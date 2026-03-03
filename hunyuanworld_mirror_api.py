"""
HunyuanWorld-Mirror GPU Backend API

Pure FastAPI server that handles 3D reconstruction requests.
All platform task management lives in suanli-task-manager; this backend
only cares about inference + OSS I/O.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import io
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

import cv2
import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CKPTS_DIR = os.path.join(PROJECT_ROOT, "ckpts")
LOCAL_MODEL_DIR = CKPTS_DIR
SKYSEG_ONNX_PATH = os.path.join(CKPTS_DIR, "skyseg.onnx")
os.makedirs(CKPTS_DIR, exist_ok=True)

sys.path.insert(0, PROJECT_ROOT)

from src.utils.inference_utils import load_and_preprocess_images
from src.utils.geometry import depth_edge, normals_edge
from src.utils.visual_util import (
    convert_predictions_to_glb_scene,
    download_file_from_url,
    segment_sky,
)
from src.utils.save_utils import save_camera_params, save_gs_ply
from src.utils.render_utils import render_interpolated_video
import onnxruntime

# ---------------------------------------------------------------------------
# OSS helpers
# ---------------------------------------------------------------------------
OSS_BUCKET = "kokokoni"
OSS_PREFIX = "docker-input&output/hunyuanworld-mirror"


def oss_upload(local_path: str, oss_key: str) -> None:
    oss_url = f"oss://{OSS_BUCKET}/{oss_key}"
    r = subprocess.run(["ossutil", "cp", local_path, oss_url, "-f"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[oss] upload failed: {r.stderr.strip()}")
        raise RuntimeError(f"ossutil cp failed: {r.stderr.strip()}")


def oss_upload_dir(local_dir: str, oss_key_dir: str) -> None:
    oss_url = f"oss://{OSS_BUCKET}/{oss_key_dir}"
    r = subprocess.run(["ossutil", "cp", local_dir, oss_url, "-rf"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[oss] upload dir failed: {r.stderr.strip()}")
        raise RuntimeError(f"ossutil cp -r failed: {r.stderr.strip()}")


def oss_sign_url(oss_key: str, expires: str = "1h") -> str:
    """Generate a presigned download URL using ossutil presign."""
    oss_url = f"oss://{OSS_BUCKET}/{oss_key}"
    result = subprocess.run(
        ["ossutil", "presign", oss_url, "--expires-duration", expires],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[oss] presign failed: {result.stderr.strip()}")
        raise RuntimeError(f"ossutil presign failed: {result.stderr.strip()}")
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("https://") or line.startswith("http://"):
            return line
    return result.stdout.strip().splitlines()[0].strip()


# ---------------------------------------------------------------------------
# Reconstruction queue (concurrency=1, async FIFO)
# ---------------------------------------------------------------------------
class ReconstructionQueue:
    """Async single-concurrency queue with status tracking."""

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(1)
        self._lock = Lock()
        self._processing_id: str | None = None
        self._pending: OrderedDict[str, int] = OrderedDict()
        self._position_counter = 0

    def register(self, request_id: str) -> None:
        with self._lock:
            self._position_counter += 1
            self._pending[request_id] = self._position_counter

    def set_processing(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)
            self._processing_id = request_id

    def finish(self, request_id: str) -> None:
        with self._lock:
            if self._processing_id == request_id:
                self._processing_id = None
            self._pending.pop(request_id, None)

    def status(self, request_id: str | None = None) -> dict:
        with self._lock:
            pending_count = len(self._pending)
            processing = self._processing_id is not None

            if request_id:
                if self._processing_id == request_id:
                    return {"processing": True, "pending": pending_count,
                            "status": "processing", "position": 0}
                if request_id in self._pending:
                    pos = list(self._pending.keys()).index(request_id) + 1
                    return {"processing": processing, "pending": pending_count,
                            "status": "pending", "position": pos}
                return {"processing": processing, "pending": pending_count,
                        "status": "idle", "position": -1}

            return {"processing": processing, "pending": pending_count,
                    "status": "processing" if processing else ("pending" if pending_count else "idle"),
                    "position": 0}


QUEUE = ReconstructionQueue()

# ---------------------------------------------------------------------------
# Global model state
# ---------------------------------------------------------------------------
model = None

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="HunyuanWorld-Mirror API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# File processing (adapted from app.py process_uploaded_files)
# ---------------------------------------------------------------------------
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp"}


def process_uploaded_files(
    file_paths: list[str],
    target_dir: str,
    time_interval: float = 1.0,
) -> list[str]:
    """Process uploaded files: extract video frames / convert HEIC / copy images."""
    images_dir = os.path.join(target_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    image_paths: list[str] = []

    for src_path in file_paths:
        ext = os.path.splitext(src_path)[1].lower()
        base_name = os.path.splitext(os.path.basename(src_path))[0]

        if ext in VIDEO_EXTS:
            cap = cv2.VideoCapture(src_path)
            fps = cap.get(cv2.CAP_PROP_FPS)
            interval = max(1, int(fps * time_interval))
            frame_count = 0
            saved_count = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                frame_count += 1
                if frame_count % interval == 0:
                    dst = os.path.join(images_dir, f"{base_name}_{saved_count:06d}.png")
                    cv2.imwrite(dst, frame)
                    image_paths.append(dst)
                    saved_count += 1
            cap.release()
            print(f"Extracted {saved_count} frames from {os.path.basename(src_path)}")

        elif ext in (".heic", ".heif"):
            try:
                with Image.open(src_path) as img:
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    dst = os.path.join(images_dir, f"{base_name}.jpg")
                    img.save(dst, "JPEG", quality=95)
                    image_paths.append(dst)
            except Exception as e:
                print(f"HEIC conversion failed for {src_path}: {e}")
                dst = os.path.join(images_dir, os.path.basename(src_path))
                shutil.copy(src_path, dst)
                image_paths.append(dst)
        else:
            dst = os.path.join(images_dir, os.path.basename(src_path))
            shutil.copy(src_path, dst)
            image_paths.append(dst)

    image_paths.sort()
    return image_paths


# ---------------------------------------------------------------------------
# Model inference (adapted from app.py run_model)
# ---------------------------------------------------------------------------
def run_model(
    target_dir: str,
    confidence_percentile: float = 10,
    edge_normal_threshold: float = 5.0,
    edge_depth_threshold: float = 0.03,
    apply_confidence_mask: bool = True,
    apply_edge_mask: bool = True,
) -> tuple[dict, dict]:
    """Run WorldMirror model and return (outputs, processed_data)."""
    global model

    from src.models.models.worldmirror import WorldMirror
    from src.models.utils.geometry import depth_to_world_coords_points

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if model is None:
        required = ["config.json", "model.safetensors"]
        missing = [f for f in required if not os.path.exists(os.path.join(LOCAL_MODEL_DIR, f))]
        if missing:
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id="tencent/HunyuanWorld-Mirror",
                local_dir=LOCAL_MODEL_DIR,
                local_dir_use_symlinks=False,
                allow_patterns=required,
            )
        model = WorldMirror.from_pretrained(LOCAL_MODEL_DIR).to(device)
    else:
        model.to(device)
    model.eval()

    image_folder = os.path.join(target_dir, "images")
    image_files = sorted(os.listdir(image_folder))
    image_paths = [os.path.join(image_folder, f) for f in image_files]
    img = load_and_preprocess_images(image_paths).to(device)

    if img.shape[1] == 0:
        raise ValueError("No images found.")

    inputs = {"img": img}
    use_amp = torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    amp_dtype = torch.bfloat16 if use_amp else torch.float32
    with torch.amp.autocast("cuda", enabled=bool(use_amp), dtype=amp_dtype):
        predictions = model(inputs)

    imgs = inputs["img"].permute(0, 1, 3, 4, 2)[0].detach().cpu().numpy()
    depth_preds = predictions["depth"][0].detach().cpu().numpy()
    depth_conf = predictions["depth_conf"][0].detach().cpu().numpy()
    normal_preds = predictions["normals"][0].detach().cpu().numpy()
    camera_poses = predictions["camera_poses"][0].detach().cpu().numpy()
    camera_intrs = predictions["camera_intrs"][0].detach().cpu().numpy()

    pts3d_preds = depth_to_world_coords_points(
        predictions["depth"][0, ..., 0],
        predictions["camera_poses"][0],
        predictions["camera_intrs"][0],
    )[0].detach().cpu().numpy()
    pts3d_conf = depth_conf

    if not os.path.exists(SKYSEG_ONNX_PATH):
        download_file_from_url(
            "https://huggingface.co/JianyuanWang/skyseg/resolve/main/skyseg.onnx",
            SKYSEG_ONNX_PATH,
        )
    skyseg_session = onnxruntime.InferenceSession(SKYSEG_ONNX_PATH)
    sky_mask_list = []
    for img_path in image_paths:
        sm = segment_sky(img_path, skyseg_session)
        if sm.shape[0] != imgs.shape[1] or sm.shape[1] != imgs.shape[2]:
            sm = cv2.resize(sm, (imgs.shape[2], imgs.shape[1]))
        sky_mask_list.append(sm)
    sky_mask = np.stack(sky_mask_list, axis=0) > 0

    final_mask_list = []
    for i in range(inputs["img"].shape[1]):
        final_mask = None
        if apply_confidence_mask:
            conf = pts3d_conf[i]
            thr = np.quantile(conf, confidence_percentile / 100.0)
            conf_mask = conf >= thr
            final_mask = conf_mask if final_mask is None else (final_mask & conf_mask)
        if apply_edge_mask:
            normal_edges = normals_edge(normal_preds[i], tol=edge_normal_threshold, mask=final_mask)
            depth_edges = depth_edge(depth_preds[i, :, :, 0], rtol=edge_depth_threshold, mask=final_mask)
            edge_mask = ~(depth_edges & normal_edges)
            final_mask = edge_mask if final_mask is None else (final_mask & edge_mask)
        final_mask_list.append(final_mask)

    if final_mask_list[0] is not None:
        final_mask_arr = np.stack(final_mask_list, axis=0)
    else:
        final_mask_arr = np.ones(pts3d_conf.shape[:3], dtype=bool)

    outputs: dict[str, Any] = {
        "images": imgs,
        "world_points": pts3d_preds,
        "depth": depth_preds,
        "normal": normal_preds,
        "final_mask": final_mask_arr,
        "sky_mask": sky_mask,
        "camera_poses": camera_poses,
        "camera_intrs": camera_intrs,
    }

    if "splats" in predictions:
        splats: dict[str, Any] = {}
        for k in ("means", "scales", "quats", "opacities", "sh", "colors"):
            if k in predictions["splats"]:
                splats[k] = predictions["splats"][k]
        outputs["splats"] = splats

    processed_data: dict[int, dict] = {}
    nviews = inputs["img"].shape[1]
    for idx in range(nviews):
        rgb = inputs["img"][0, idx].detach().cpu().numpy()
        processed_data[idx] = {
            "image": rgb,
            "points3d": pts3d_preds[idx],
            "depth": depth_preds[idx].squeeze(),
            "normal": normal_preds[idx],
            "mask": final_mask_arr[idx].copy(),
        }

    torch.cuda.empty_cache()
    return outputs, processed_data


# ---------------------------------------------------------------------------
# Visualization helpers
# ---------------------------------------------------------------------------
def render_depth_png(depth_map: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    import matplotlib.pyplot as plt

    depth_copy = depth_map.copy()
    pos = depth_copy > 0
    if mask is not None:
        pos = pos & mask
    if pos.sum() > 0:
        vals = depth_copy[pos]
        lo, hi = np.percentile(vals, 5), np.percentile(vals, 95)
        depth_copy[pos] = (depth_copy[pos] - lo) / (hi - lo + 1e-8)
    rgb = (plt.cm.turbo_r(depth_copy)[:, :, :3] * 255).astype(np.uint8)
    rgb[~pos] = [255, 255, 255]
    return rgb


def render_normal_png(normal_map: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    n = normal_map.copy()
    if mask is not None:
        n[~mask] = [0, 0, 0]
    return ((n + 1.0) / 2.0 * 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Output generation + OSS upload (adapted from app.py gradio_demo)
# ---------------------------------------------------------------------------
def generate_and_upload_results(
    target_dir: str,
    outputs: dict,
    processed_data: dict,
    session_id: str,
    frame_selector: str = "All",
    show_camera: bool = True,
    show_mesh: bool = True,
    filter_sky_bg: bool = False,
    filter_ambiguous: bool = True,
) -> dict:
    """Generate all output files, upload to OSS, return signed URLs."""

    oss_out = f"{OSS_PREFIX}/{session_id}/output"
    result: dict[str, Any] = {"session_id": session_id}

    image_folder = os.path.join(target_dir, "images")
    all_files = sorted(os.listdir(image_folder)) if os.path.isdir(image_folder) else []
    frame_choices = ["All"] + [f"{i}: {f}" for i, f in enumerate(all_files)]
    result["frame_choices"] = frame_choices
    result["num_views"] = len(all_files)

    # --- GLB scene ---
    glb_path = os.path.join(target_dir, "scene.glb")
    glbscene = convert_predictions_to_glb_scene(
        outputs,
        filter_by_frames=frame_selector,
        show_camera=show_camera,
        mask_sky_bg=filter_sky_bg,
        as_mesh=show_mesh,
        mask_ambiguous=filter_ambiguous,
    )
    glbscene.export(file_obj=glb_path)
    oss_key = f"{oss_out}/scene.glb"
    oss_upload(glb_path, oss_key)
    result["glb_url"] = oss_sign_url(oss_key)

    # --- Camera params ---
    cam_file = save_camera_params(outputs["camera_poses"], outputs["camera_intrs"], target_dir)
    if cam_file and os.path.exists(cam_file):
        cam_key = f"{oss_out}/{os.path.basename(cam_file)}"
        oss_upload(cam_file, cam_key)
        result["camera_params_url"] = oss_sign_url(cam_key)
    else:
        result["camera_params_url"] = None

    # --- Gaussian PLY ---
    gs_url = None
    if "splats" in outputs:
        means = outputs["splats"]["means"][0].reshape(-1, 3)
        scales = outputs["splats"]["scales"][0].reshape(-1, 3)
        quats = outputs["splats"]["quats"][0].reshape(-1, 4)
        colors_key = "sh" if "sh" in outputs["splats"] else "colors"
        colors = outputs["splats"][colors_key][0].reshape(-1, 3)
        opacities = outputs["splats"]["opacities"][0].reshape(-1)

        def _to_tensor(x):
            return x if isinstance(x, torch.Tensor) else torch.from_numpy(x)

        means = _to_tensor(means)
        scales = _to_tensor(scales)
        quats = _to_tensor(quats)
        colors = _to_tensor(colors)
        opacities = _to_tensor(opacities)

        ply_path = os.path.join(target_dir, "gaussians.ply")
        save_gs_ply(ply_path, means, scales, quats, colors, opacities)
        ply_key = f"{oss_out}/gaussians.ply"
        oss_upload(ply_path, ply_key)
        gs_url = oss_sign_url(ply_key)
    result["ply_url"] = gs_url

    # --- Depth / Normal PNGs ---
    depth_urls: list[str] = []
    normal_urls: list[str] = []
    for idx, vd in processed_data.items():
        if vd["depth"] is not None:
            d_img = render_depth_png(vd["depth"], mask=vd.get("mask"))
            d_path = os.path.join(target_dir, f"depth_{idx}.png")
            Image.fromarray(d_img).save(d_path)
            d_key = f"{oss_out}/depth_{idx}.png"
            oss_upload(d_path, d_key)
            depth_urls.append(oss_sign_url(d_key))
        if vd["normal"] is not None:
            n_img = render_normal_png(vd["normal"], mask=vd.get("mask"))
            n_path = os.path.join(target_dir, f"normal_{idx}.png")
            Image.fromarray(n_img).save(n_path)
            n_key = f"{oss_out}/normal_{idx}.png"
            oss_upload(n_path, n_key)
            normal_urls.append(oss_sign_url(n_key))
    result["depth_urls"] = depth_urls
    result["normal_urls"] = normal_urls

    # --- Rendered videos ---
    rgb_video_url = None
    depth_video_url = None
    if "splats" in outputs:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        cam_poses_t = torch.tensor(outputs["camera_poses"]).unsqueeze(0).to(device)
        cam_intrs_t = torch.tensor(outputs["camera_intrs"]).unsqueeze(0).to(device)
        H, W = outputs["images"].shape[1], outputs["images"].shape[2]
        out_stem = Path(target_dir) / "rendered_video"
        render_interpolated_video(
            model.gs_renderer,
            outputs["splats"],
            cam_poses_t,
            cam_intrs_t,
            (H, W),
            out_stem,
            interp_per_pair=15,
            loop_reverse=True,
            save_mode="split",
        )
        rgb_vid = str(out_stem) + "_rgb.mp4"
        depth_vid = str(out_stem) + "_depth.mp4"
        if os.path.exists(rgb_vid):
            k = f"{oss_out}/rendered_rgb.mp4"
            oss_upload(rgb_vid, k)
            rgb_video_url = oss_sign_url(k)
        if os.path.exists(depth_vid):
            k = f"{oss_out}/rendered_depth.mp4"
            oss_upload(depth_vid, k)
            depth_video_url = oss_sign_url(k)
    result["rgb_video_url"] = rgb_video_url
    result["depth_video_url"] = depth_video_url

    return result


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/queue_status")
def queue_status(request_id: str | None = None):
    return QUEUE.status(request_id)


@app.post("/reconstruct")
async def reconstruct(
    files: list[UploadFile] = File(...),
    time_interval: float = Form(default=1.0),
    frame_selector: str = Form(default="All"),
    show_camera: bool = Form(default=True),
    show_mesh: bool = Form(default=True),
    filter_sky_bg: bool = Form(default=False),
    filter_ambiguous: bool = Form(default=True),
    request_id: str = Form(default=""),
):
    request_id = request_id.strip() or uuid.uuid4().hex
    QUEUE.register(request_id)

    try:
        async with QUEUE._sem:
            QUEUE.set_processing(request_id)
            return await _do_reconstruct(
                files, time_interval, frame_selector,
                show_camera, show_mesh, filter_sky_bg, filter_ambiguous,
                request_id,
            )
    finally:
        QUEUE.finish(request_id)


async def _do_reconstruct(
    files: list[UploadFile],
    time_interval: float,
    frame_selector: str,
    show_camera: bool,
    show_mesh: bool,
    filter_sky_bg: bool,
    filter_ambiguous: bool,
    request_id: str,
) -> dict:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = f"{timestamp}_{request_id[:8]}"
    target_dir = os.path.join(PROJECT_ROOT, f"workspace_{session_id}")

    try:
        os.makedirs(target_dir, exist_ok=True)
        upload_dir = os.path.join(target_dir, "uploads")
        os.makedirs(upload_dir, exist_ok=True)

        saved_paths: list[str] = []
        for f in files:
            ext = os.path.splitext(f.filename or "")[1] or ".png"
            name = f"{uuid.uuid4().hex}{ext}"
            path = os.path.join(upload_dir, name)
            content = await f.read()
            with open(path, "wb") as fp:
                fp.write(content)
            saved_paths.append(path)

        print(f"[reconstruct] Processing {len(saved_paths)} files, session={session_id}")

        gc.collect()
        torch.cuda.empty_cache()

        image_paths = await asyncio.get_event_loop().run_in_executor(
            None, process_uploaded_files, saved_paths, target_dir, time_interval,
        )

        if not image_paths:
            raise HTTPException(status_code=400, detail="No valid images after processing.")

        oss_input_dir = f"{OSS_PREFIX}/{session_id}/input"
        await asyncio.get_event_loop().run_in_executor(
            None, oss_upload_dir, os.path.join(target_dir, "images"), oss_input_dir + "/images/",
        )

        print(f"[reconstruct] Running WorldMirror inference...")
        with torch.no_grad():
            outputs, processed_data = await asyncio.get_event_loop().run_in_executor(
                None, run_model, target_dir,
            )

        print(f"[reconstruct] Generating and uploading results...")
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            generate_and_upload_results,
            target_dir, outputs, processed_data, session_id,
            frame_selector, show_camera, show_mesh, filter_sky_bg, filter_ambiguous,
        )

        del outputs
        gc.collect()
        torch.cuda.empty_cache()

        print(f"[reconstruct] Done. session={session_id}")
        return result

    except HTTPException:
        raise
    except Exception as e:
        print(f"[reconstruct] Error: {e}")
        raise HTTPException(status_code=500, detail=f"Reconstruction failed: {e}")
    finally:
        if os.path.isdir(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HunyuanWorld-Mirror API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10085)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
