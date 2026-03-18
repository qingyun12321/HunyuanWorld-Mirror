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
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Condition, Lock, Thread
from typing import Any

import cv2
import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from lightning_utilities.core.rank_zero import rank_zero_only
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
LOG_LEVEL = os.getenv("KOKONI_LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [runtime] %(message)s",
)
logger = logging.getLogger("runtime")

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
# Runtime queue state
# ---------------------------------------------------------------------------
TASK_QUEUE_MAXSIZE = max(int(os.getenv("TASK_QUEUE_MAXSIZE", "8")), 1)


@dataclass
class RequestRecord:
    status: str
    created_at: float = field(default_factory=time.time)
    error: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] = field(default_factory=dict)


class ConcurrentTaskQueue:
    """Single-worker runtime queue with inspectable request status."""

    def __init__(self, *, maxsize: int = TASK_QUEUE_MAXSIZE) -> None:
        self._cond = Condition()
        self._pending: list[tuple[str, Any]] = []
        self._active_request_ids: set[str] = set()
        self._records: dict[str, RequestRecord] = {}
        self._workers: list[Thread] = []
        self._running = False
        self._maxsize = max(1, int(maxsize or 1))

    def start(self, handler: Any, *, worker_count: int = 1) -> None:
        with self._cond:
            if any(worker.is_alive() for worker in self._workers):
                return
            self._running = True
            self._workers = []
            total = max(1, int(worker_count or 1))
            for index in range(total):
                worker = Thread(
                    target=self._worker_loop,
                    args=(handler,),
                    daemon=True,
                    name=f"hunyuanworld-runtime-worker-{index}",
                )
                self._workers.append(worker)
                worker.start()

    def enqueue(
        self,
        payload: Any,
        *,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        rid = (request_id or "").strip() or uuid.uuid4().hex
        initial_meta = dict(metadata or {})

        with self._cond:
            record = self._records.get(rid)
            if record and record.status in {"pending", "processing"}:
                raise ValueError("request_id already exists in queue")
            active_count = len(self._pending) + len(self._active_request_ids)
            if active_count >= self._maxsize:
                raise OverflowError("queue is full")

            self._records[rid] = RequestRecord(status="pending", result=initial_meta)
            self._pending.append((rid, payload))
            position = self._pending_position_unlocked(rid)
            self._cond.notify()
            return rid, position

    def get_queue_status(self, request_id: str | None = None) -> dict[str, Any]:
        with self._cond:
            active_request_ids = sorted(self._active_request_ids)
            payload: dict[str, Any] = {
                "processing": bool(active_request_ids),
                "processing_count": len(active_request_ids),
                "pending": len(self._pending),
                "current_request_id": active_request_ids[0] if active_request_ids else "",
                "processing_request_ids": active_request_ids,
            }
            if request_id is not None:
                rid = request_id.strip()
                payload["status"] = self._records[rid].status if rid in self._records else "unknown"
                payload["position"] = self._position_for_request_unlocked(rid)
            else:
                payload["status"] = (
                    "processing" if active_request_ids else ("pending" if self._pending else "idle")
                )
            return payload

    def get_request_status(self, request_id: str) -> dict[str, Any] | None:
        rid = request_id.strip()
        with self._cond:
            record = self._records.get(rid)
            if not record:
                return None
            result = dict(record.result)
            payload: dict[str, Any] = {
                "request_id": rid,
                "status": record.status,
                "error": record.error,
                "created_at": record.created_at,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
                "result": result,
            }
            if record.status == "pending":
                payload["position"] = self._pending_position_unlocked(rid)
            for key, value in result.items():
                if key not in payload:
                    payload[key] = value
            return payload

    def _worker_loop(self, handler: Any) -> None:
        while True:
            with self._cond:
                while self._running and not self._pending:
                    self._cond.wait()
                if not self._running:
                    return
                request_id, payload = self._pending.pop(0)
                self._active_request_ids.add(request_id)
                record = self._records[request_id]
                record.status = "processing"
                record.error = ""
                record.started_at = time.time()

            try:
                result = handler(request_id, payload) or {}
                with self._cond:
                    record = self._records[request_id]
                    record.status = "completed"
                    record.finished_at = time.time()
                    if isinstance(result, dict):
                        merged = dict(record.result)
                        merged.update(result)
                        record.result = merged
            except Exception as exc:  # pragma: no cover - exercised via API surface
                with self._cond:
                    record = self._records[request_id]
                    record.status = "failed"
                    record.error = str(exc)
                    record.finished_at = time.time()
            finally:
                with self._cond:
                    self._active_request_ids.discard(request_id)

    def _position_for_request_unlocked(self, request_id: str) -> int:
        if not request_id:
            return -1
        if request_id in self._active_request_ids:
            return 0
        return self._pending_position_unlocked(request_id)

    def _pending_position_unlocked(self, request_id: str) -> int:
        for index, (rid, _payload) in enumerate(self._pending):
            if rid == request_id:
                return index + 1
        return -1


RUN_QUEUE = ConcurrentTaskQueue()

# ---------------------------------------------------------------------------
# Global model state
# ---------------------------------------------------------------------------
model = None
MODEL_LOAD_LOCK = Lock()
MODEL_STATUS_LOCK = Lock()
MODEL_STATUS: dict[str, str] = {
    "status": "starting",
    "message": "Runtime is initializing.",
}


def _set_model_status(status: str, message: str = "") -> None:
    with MODEL_STATUS_LOCK:
        MODEL_STATUS["status"] = status
        MODEL_STATUS["message"] = message
    logger.info("model status changed: %s (%s)", status, message or "no-message")


def _get_model_status() -> dict[str, str]:
    with MODEL_STATUS_LOCK:
        return dict(MODEL_STATUS)


def _ensure_rank_zero_initialized() -> None:
    if getattr(rank_zero_only, "rank", None) is None:
        rank_zero_only.rank = 0
        logger.info("initialized rank_zero_only.rank=0 for inference runtime")


def _ensure_model_loaded() -> None:
    global model

    from src.models.models.worldmirror import WorldMirror

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if model is None:
        with MODEL_LOAD_LOCK:
            if model is None:
                _ensure_rank_zero_initialized()
                _set_model_status("loading", "Loading model weights.")
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
                model.eval()
                _set_model_status("ready", "Model is ready.")
    else:
        model.to(device)
        model.eval()
        _set_model_status("ready", "Model is ready.")


def _startup_preload() -> None:
    try:
        logger.info("startup preload begins")
        _ensure_model_loaded()
        logger.info("startup preload finished")
    except Exception as exc:  # pragma: no cover - startup environment dependent
        _set_model_status("error", str(exc))
        logger.exception("startup preload failed")


def _health_payload() -> dict[str, Any]:
    state = _get_model_status()
    return {
        "status": "ok" if state["status"] == "ready" else state["status"],
        "model_ready": state["status"] == "ready",
        "message": state["message"],
    }


def _require_runtime_ready() -> None:
    payload = _health_payload()
    if payload["model_ready"]:
        return
    raise HTTPException(status_code=503, detail=payload["message"] or "Runtime is not ready.")

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


@app.on_event("startup")
def startup_event():
    logger.info("runtime startup event triggered")
    RUN_QUEUE.start(_execute_run_request)
    Thread(target=_startup_preload, daemon=True).start()


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

    from src.models.utils.geometry import depth_to_world_coords_points

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _ensure_model_loaded()
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
    payload = _health_payload()
    status_code = 200 if payload["model_ready"] else 503
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/ready")
def ready_check():
    payload = _health_payload()
    status_code = 200 if payload["model_ready"] else 503
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/live")
def live_check():
    return {"status": "ok"}


@app.get("/queue_status")
def queue_status(request_id: str | None = None):
    rid = request_id.strip() if request_id else None
    return RUN_QUEUE.get_queue_status(rid if rid else None)


@app.get("/request_status")
def request_status(request_id: str):
    rid = request_id.strip()
    if not rid:
        raise HTTPException(status_code=400, detail="request_id is required")
    payload = RUN_QUEUE.get_request_status(rid)
    if payload is None:
        raise HTTPException(status_code=404, detail="request_id not found")
    return payload


async def _read_upload_payloads(files: list[UploadFile]) -> list[dict[str, Any]]:
    uploads: list[dict[str, Any]] = []
    for upload in files:
        content = await upload.read()
        if not content:
            continue
        uploads.append(
            {
                "filename": upload.filename or "upload.bin",
                "content_type": upload.content_type or "application/octet-stream",
                "content": content,
            }
        )
    return uploads


def _build_queue_payload(
    upload_payloads: list[dict[str, Any]],
    *,
    time_interval: float,
    frame_selector: str,
    show_camera: bool,
    show_mesh: bool,
    filter_sky_bg: bool,
    filter_ambiguous: bool,
) -> dict[str, Any]:
    return {
        "files": upload_payloads,
        "time_interval": float(time_interval),
        "frame_selector": frame_selector,
        "show_camera": bool(show_camera),
        "show_mesh": bool(show_mesh),
        "filter_sky_bg": bool(filter_sky_bg),
        "filter_ambiguous": bool(filter_ambiguous),
    }


def _execute_run_request(request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _process_reconstruct_request(
        upload_payloads=payload.get("files") or [],
        time_interval=float(payload.get("time_interval", 1.0)),
        frame_selector=str(payload.get("frame_selector") or "All"),
        show_camera=bool(payload.get("show_camera", True)),
        show_mesh=bool(payload.get("show_mesh", True)),
        filter_sky_bg=bool(payload.get("filter_sky_bg", False)),
        filter_ambiguous=bool(payload.get("filter_ambiguous", True)),
        request_id=request_id,
    )


def _enqueue_request(
    upload_payloads: list[dict[str, Any]],
    *,
    time_interval: float,
    frame_selector: str,
    show_camera: bool,
    show_mesh: bool,
    filter_sky_bg: bool,
    filter_ambiguous: bool,
    request_id: str = "",
) -> dict[str, Any]:
    _require_runtime_ready()
    if not upload_payloads:
        raise HTTPException(status_code=400, detail="At least one file is required.")
    try:
        request_id_out, position = RUN_QUEUE.enqueue(
            _build_queue_payload(
                upload_payloads,
                time_interval=time_interval,
                frame_selector=frame_selector,
                show_camera=show_camera,
                show_mesh=show_mesh,
                filter_sky_bg=filter_sky_bg,
                filter_ambiguous=filter_ambiguous,
            ),
            request_id=request_id or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "status": "queued",
        "request_id": request_id_out,
        "position": position,
    }


@app.post("/run_with_files")
async def run_with_files(
    files: list[UploadFile] = File(...),
    time_interval: float = Form(default=1.0),
    frame_selector: str = Form(default="All"),
    show_camera: bool = Form(default=True),
    show_mesh: bool = Form(default=True),
    filter_sky_bg: bool = Form(default=False),
    filter_ambiguous: bool = Form(default=True),
    request_id: str = Form(default=""),
):
    upload_payloads = await _read_upload_payloads(files)
    return _enqueue_request(
        upload_payloads,
        time_interval=time_interval,
        frame_selector=frame_selector,
        show_camera=show_camera,
        show_mesh=show_mesh,
        filter_sky_bg=filter_sky_bg,
        filter_ambiguous=filter_ambiguous,
        request_id=request_id.strip(),
    )


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
    return await run_with_files(
        files=files,
        time_interval=time_interval,
        frame_selector=frame_selector,
        show_camera=show_camera,
        show_mesh=show_mesh,
        filter_sky_bg=filter_sky_bg,
        filter_ambiguous=filter_ambiguous,
        request_id=request_id,
    )


def _process_reconstruct_request(
    *,
    upload_payloads: list[dict[str, Any]],
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
        for upload in upload_payloads:
            ext = os.path.splitext(upload.get("filename") or "")[1] or ".png"
            name = f"{uuid.uuid4().hex}{ext}"
            path = os.path.join(upload_dir, name)
            with open(path, "wb") as fp:
                fp.write(upload["content"])
            saved_paths.append(path)

        print(f"[reconstruct] Processing {len(saved_paths)} files, session={session_id}")

        gc.collect()
        torch.cuda.empty_cache()

        image_paths = process_uploaded_files(saved_paths, target_dir, time_interval)

        if not image_paths:
            raise HTTPException(status_code=400, detail="No valid images after processing.")

        oss_input_dir = f"{OSS_PREFIX}/{session_id}/input"
        oss_upload_dir(os.path.join(target_dir, "images"), oss_input_dir + "/images/")

        print(f"[reconstruct] Running WorldMirror inference...")
        with torch.no_grad():
            outputs, processed_data = run_model(target_dir)

        print(f"[reconstruct] Generating and uploading results...")
        result = generate_and_upload_results(
            target_dir, outputs, processed_data, session_id,
            frame_selector, show_camera, show_mesh, filter_sky_bg, filter_ambiguous,
        )

        del outputs
        gc.collect()
        torch.cuda.empty_cache()

        print(f"[reconstruct] Done. session={session_id}")
        return result

    except HTTPException as exc:
        raise RuntimeError(str(exc.detail)) from exc
    except Exception as e:
        print(f"[reconstruct] Error: {e}")
        raise RuntimeError(f"Reconstruction failed: {e}") from e
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
