import os
import json
import math
import subprocess
import threading
from typing import Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from .ffmpeg_setup import get_ffmpeg_path, probe_media_streams

TRICKPLAY_INTERVAL = 10  # 1 thumbnail every 10 seconds
THUMB_WIDTH = 160
THUMB_HEIGHT = 90
TILE_COLUMNS = 10
TILE_ROWS = 10
TILES_PER_SHEET = TILE_COLUMNS * TILE_ROWS  # 100 thumbnails per sprite sheet (1000s = ~16.6 min)


def get_trickplay_base_dir() -> str:
    """Returns the base storage directory for trickplay thumbnail sprites and manifests."""
    if os.path.exists("/config/metadata"):
        base = "/config/metadata/trickplay"
    elif os.path.exists("/config"):
        base = "/config/trickplay"
    else:
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "metadata", "trickplay"))
    os.makedirs(base, exist_ok=True)
    return base


def get_trickplay_item_dir(media_type: str, item_id: int) -> str:
    """Returns directory path for a specific media item's trickplay assets."""
    item_dir = os.path.join(get_trickplay_base_dir(), f"{media_type}_{item_id}")
    os.makedirs(item_dir, exist_ok=True)
    return item_dir


def is_trickplay_ready(media_type: str, item_id: int) -> bool:
    """Checks if trickplay sprite sheets and manifest are fully generated and ready to serve."""
    item_dir = get_trickplay_item_dir(media_type, item_id)
    manifest_file = os.path.join(item_dir, "manifest.json")
    if not os.path.exists(manifest_file) or os.path.getsize(manifest_file) == 0:
        return False
    try:
        with open(manifest_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("status") == "ready"
    except Exception:
        return False


def get_trickplay_manifest(media_type: str, item_id: int) -> Optional[Dict[str, Any]]:
    """Loads and returns the trickplay manifest for a given media item if present."""
    item_dir = get_trickplay_item_dir(media_type, item_id)
    manifest_file = os.path.join(item_dir, "manifest.json")
    if os.path.exists(manifest_file):
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _format_vtt_timestamp(seconds: float) -> str:
    """Formats seconds into standard WebVTT timestamp (HH:MM:SS.mmm)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def build_trickplay_vtt(duration: float, total_thumbnails: int, item_id: int, media_type: str) -> str:
    """Generates standard WebVTT cues mapping timestamps to sprite sheet coordinates (#xywh=x,y,w,h)."""
    lines = ["WEBVTT", ""]
    for i in range(total_thumbnails):
        start_sec = i * TRICKPLAY_INTERVAL
        end_sec = min((i + 1) * TRICKPLAY_INTERVAL, duration)
        if start_sec >= duration and i > 0:
            break

        sheet_idx = i // TILES_PER_SHEET
        pos_in_sheet = i % TILES_PER_SHEET
        col = pos_in_sheet % TILE_COLUMNS
        row = pos_in_sheet // TILE_COLUMNS
        x = col * THUMB_WIDTH
        y = row * THUMB_HEIGHT

        sheet_filename = f"sprite_{sheet_idx:03d}.jpg"
        start_str = _format_vtt_timestamp(start_sec)
        end_str = _format_vtt_timestamp(end_sec)

        lines.append(f"{start_str} --> {end_str}")
        lines.append(f"{sheet_filename}#xywh={x},{y},{THUMB_WIDTH},{THUMB_HEIGHT}")
        lines.append("")

    return "\n".join(lines)


# Dedicated low-priority single worker executor to prevent CPU thrashing
_trickplay_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trickplay_worker")
_active_jobs: Dict[str, bool] = {}
_jobs_lock = threading.Lock()


def is_trickplay_generating(media_type: str, item_id: int) -> bool:
    """Returns True if a trickplay generation task is actively running for this item."""
    key = f"{media_type}_{item_id}"
    with _jobs_lock:
        return _active_jobs.get(key, False)


def generate_trickplay_sync(file_path: str, media_type: str, item_id: int, duration: float) -> bool:
    """
    Synchronously generates sprite sheets and WebVTT manifest for a video file.
    Runs in a background thread via queue_trickplay_generation.
    """
    key = f"{media_type}_{item_id}"
    with _jobs_lock:
        if _active_jobs.get(key, False):
            return False
        _active_jobs[key] = True

    item_dir = get_trickplay_item_dir(media_type, item_id)
    tmp_manifest = os.path.join(item_dir, "manifest.json")

    # Mark as generating
    try:
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump({
                "status": "generating",
                "interval": TRICKPLAY_INTERVAL,
                "width": THUMB_WIDTH,
                "height": THUMB_HEIGHT,
                "media_type": media_type,
                "item_id": item_id
            }, f)
    except Exception:
        pass

    try:
        if not file_path or not os.path.exists(file_path):
            print(f"[Trickplay] Media file missing for {key}: {file_path}")
            return False

        if duration <= 0:
            probe = probe_media_streams(file_path)
            duration = float(probe.get("duration") or 0.0)

        if duration <= 0:
            print(f"[Trickplay] Could not determine duration for {key}")
            return False

        total_thumbnails = int(math.ceil(duration / TRICKPLAY_INTERVAL))
        total_sheets = int(math.ceil(total_thumbnails / TILES_PER_SHEET))

        # FFmpeg filter:
        # fps=1/10: sample 1 frame every 10 seconds
        # scale=160:90 with aspect decrease and pad: guarantees exact 160x90 per thumbnail
        # tile=10x10: batches 100 thumbnails into 1600x900 sprite sheet
        vf = (
            f"fps=1/{TRICKPLAY_INTERVAL},"
            f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={THUMB_WIDTH}:{THUMB_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
            f"tile={TILE_COLUMNS}x{TILE_ROWS}"
        )

        out_pattern = os.path.join(item_dir, "sprite_%03d.jpg")
        # Fast keyframe extraction (-skip_frame nokey) provides ~30-50x faster generation
        threads = str(min(os.cpu_count() or 4, 4))
        cmd = [
            get_ffmpeg_path(),
            "-v", "error",
            "-skip_frame", "nokey",
            "-threads", threads,
            "-i", file_path,
            "-an", "-sn", "-dn",
            "-vf", vf,
            "-q:v", "4",
            "-start_number", "0",
            "-y",
            out_pattern
        ]

        print(f"[Trickplay] Starting fast keyframe generation for {key} (Duration: {duration:.1f}s, ~{total_thumbnails} frames in {total_sheets} sheets)...")
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        
        # Verify produced sprite files
        produced_sheets = [
            f for f in sorted(os.listdir(item_dir))
            if f.startswith("sprite_") and f.endswith(".jpg") and os.path.getsize(os.path.join(item_dir, f)) > 0
        ]

        # Resilient fallback: if fast keyframe extraction produced no output, fallback to full decode
        if not produced_sheets:
            print(f"[Trickplay] Keyframe extraction produced no sheets for {key}. Retrying with full decode...")
            cmd_full = [
                get_ffmpeg_path(),
                "-v", "error",
                "-threads", threads,
                "-i", file_path,
                "-an", "-sn", "-dn",
                "-vf", vf,
                "-q:v", "4",
                "-start_number", "0",
                "-y",
                out_pattern
            ]
            subprocess.run(cmd_full, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            produced_sheets = [
                f for f in sorted(os.listdir(item_dir))
                if f.startswith("sprite_") and f.endswith(".jpg") and os.path.getsize(os.path.join(item_dir, f)) > 0
            ]

        if not produced_sheets:
            print(f"[Trickplay] No sprite sheets generated for {key}")
            return False

        # Generate WebVTT
        vtt_content = build_trickplay_vtt(duration, total_thumbnails, item_id, media_type)
        vtt_path = os.path.join(item_dir, "thumbnails.vtt")
        with open(vtt_path, "w", encoding="utf-8") as f:
            f.write(vtt_content)

        # Write finalized manifest
        manifest_data = {
            "status": "ready",
            "interval": TRICKPLAY_INTERVAL,
            "width": THUMB_WIDTH,
            "height": THUMB_HEIGHT,
            "columns": TILE_COLUMNS,
            "rows": TILE_ROWS,
            "duration": duration,
            "total_thumbnails": total_thumbnails,
            "total_sheets": len(produced_sheets),
            "sheets": produced_sheets,
            "vtt_url": f"/trickplay/{media_type}/{item_id}/thumbnails.vtt"
        }
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)

        print(f"[Trickplay] Generation complete for {key}: {len(produced_sheets)} sprite sheets ({THUMB_WIDTH}x{THUMB_HEIGHT} @ {TRICKPLAY_INTERVAL}s).")
        return True

    except Exception as e:
        print(f"[Trickplay] Unexpected exception generating {key}: {e}")
        return False
    finally:
        with _jobs_lock:
            _active_jobs.pop(key, None)


def queue_trickplay_generation(file_path: str, media_type: str, item_id: int, duration: Optional[float] = None) -> bool:
    """
    Enqueues trickplay sprite generation in the background worker thread.
    Returns True if scheduled, False if already ready or already generating.
    """
    if is_trickplay_ready(media_type, item_id):
        return False
    if is_trickplay_generating(media_type, item_id):
        return False

    dur_val = float(duration) if duration and duration > 0 else 0.0
    _trickplay_executor.submit(generate_trickplay_sync, file_path, media_type, item_id, dur_val)
    return True

