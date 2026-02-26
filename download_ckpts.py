#!/usr/bin/env python3
"""Download all runtime model assets into ckpts/ for app.py."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

WORLDMIRROR_REPO = "tencent/HunyuanWorld-Mirror"
SKYSEG_REPO = "JianyuanWang/skyseg"
SKYSEG_FILENAME = "skyseg.onnx"
WORLDMIRROR_REQUIRED_FILES = ("config.json", "model.safetensors")


def ensure_worldmirror_weights(ckpts_dir: Path, force: bool = False) -> None:
    if force:
        for filename in WORLDMIRROR_REQUIRED_FILES:
            target = ckpts_dir / filename
            if target.exists():
                target.unlink()

    print(f"[1/2] Syncing {WORLDMIRROR_REPO} -> {ckpts_dir}")
    snapshot_download(
        repo_id=WORLDMIRROR_REPO,
        local_dir=str(ckpts_dir),
        allow_patterns=list(WORLDMIRROR_REQUIRED_FILES),
    )

    missing = [
        filename
        for filename in WORLDMIRROR_REQUIRED_FILES
        if not (ckpts_dir / filename).exists()
    ]
    if missing:
        raise RuntimeError(
            "WorldMirror weights download incomplete. Missing files: "
            + ", ".join(missing)
        )


def ensure_skyseg_onnx(ckpts_dir: Path, force: bool = False) -> None:
    skyseg_path = ckpts_dir / SKYSEG_FILENAME
    if force and skyseg_path.exists():
        skyseg_path.unlink()

    if skyseg_path.exists():
        print(f"[2/2] {SKYSEG_FILENAME} already exists at {skyseg_path}")
        return

    print(f"[2/2] Downloading {SKYSEG_FILENAME} -> {skyseg_path}")
    hf_hub_download(
        repo_id=SKYSEG_REPO,
        filename=SKYSEG_FILENAME,
        local_dir=str(ckpts_dir),
    )


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    default_ckpts_dir = script_dir / "ckpts"

    parser = argparse.ArgumentParser(
        description=(
            "Pre-download all model assets required by app.py into ckpts/. "
            "This avoids first-run downloads inside the Gradio app."
        )
    )
    parser.add_argument(
        "--ckpts-dir",
        type=Path,
        default=default_ckpts_dir,
        help=f"Target ckpts directory (default: {default_ckpts_dir})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download files even if they already exist.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ckpts_dir = args.ckpts_dir.resolve()
    ckpts_dir.mkdir(parents=True, exist_ok=True)

    try:
        ensure_worldmirror_weights(ckpts_dir, force=args.force)
        ensure_skyseg_onnx(ckpts_dir, force=args.force)
    except Exception as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1

    print("\nDone. Local assets ready:")
    for filename in (*WORLDMIRROR_REQUIRED_FILES, SKYSEG_FILENAME):
        file_path = ckpts_dir / filename
        if file_path.exists():
            size_mb = file_path.stat().st_size / (1024 * 1024)
            print(f"  - {file_path} ({size_mb:.2f} MB)")
        else:
            print(f"  - {file_path} (missing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
