import os
import subprocess
import shutil
import time
import math
import threading
import re
from typing import Optional, Dict
from .ffmpeg_setup import (
    get_ffmpeg_path, probe_media_streams, get_video_transcode_args, 
    get_working_encoder, get_keyframe_before_time
)

SEGMENT_DURATION = 6.0  # 6.0 seconds per HLS chunk

def get_hls_cache_dir() -> str:
    """Returns directory for HLS transcode segments and playlists."""
    base = "/config" if os.path.exists("/config") else os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config"))
    hls_dir = os.path.join(base, "transcodes", "hls")
    os.makedirs(hls_dir, exist_ok=True)
    return hls_dir

class HLSTranscodeWorker:
    """Manages an active FFmpeg process generating HLS segments for a specific stream."""
    def __init__(
        self,
        session_key: str,
        media_path: str,
        output_dir: str,
        res: str,
        audio_track: int,
        start_segment: int,
        hw_profile: str
    ):
        self.session_key = session_key
        self.media_path = media_path
        self.output_dir = output_dir
        self.res = res
        self.audio_track = audio_track
        self.start_segment = start_segment
        self.hw_profile = hw_profile
        self.process: Optional[subprocess.Popen] = None
        self.last_active: float = time.time()
        self.lock = threading.Lock()
        self.is_stopped: bool = False

    def start(self):
        with self.lock:
            if self.is_stopped:
                return
            os.makedirs(self.output_dir, exist_ok=True)
            start_time = self.start_segment * SEGMENT_DURATION

            probe = probe_media_streams(self.media_path)
            audio_tracks = probe.get("audio_tracks", [])
            selected_track = audio_tracks[self.audio_track] if (0 <= self.audio_track < len(audio_tracks)) else (audio_tracks[0] if audio_tracks else None)
            track_can_copy = selected_track.get("can_direct_stream", False) if selected_track else False
            can_direct_stream_video = (self.res == "original" and probe.get("can_direct_stream_video", False))

            cmd = [get_ffmpeg_path(), "-v", "error", "-y"]
            if start_time > 0:
                cmd.extend(["-ss", f"{start_time:.3f}"])
                if can_direct_stream_video:
                    cmd.append("-noaccurate_seek")

            cmd.extend(["-i", self.media_path])

            cmd.extend(["-map", "0:v:0"])
            if selected_track:
                cmd.extend(["-map", f"0:a:{self.audio_track}"])
            else:
                cmd.append("-an")
            cmd.extend(["-sn", "-map_chapters", "-1"])

            if can_direct_stream_video:
                cmd.extend(["-c:v", "copy"])
                if track_can_copy:
                    cmd.extend(["-c:a", "copy"])
                else:
                    cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-af", "aresample=async=1"])
            else:
                transcode_args = get_video_transcode_args(res=self.res, profile=self.hw_profile)
                cmd.extend(transcode_args)
                cmd.extend(["-force_key_frames", f"expr:gte(t,n_forced*{int(SEGMENT_DURATION)})"])
                if selected_track:
                    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-af", "aresample=async=1"])

            segment_pattern = os.path.join(self.output_dir, "seg_%04d.ts")
            playlist_path = os.path.join(self.output_dir, "prog.m3u8")

            cmd.extend([
                "-max_muxing_queue_size", "1024",
            ])
            if start_time > 0:
                cmd.extend(["-output_ts_offset", f"{start_time:.3f}"])
            cmd.extend([
                "-muxdelay", "0",
                "-f", "hls",
                "-hls_time", str(int(SEGMENT_DURATION)),
                "-hls_list_size", "0",
                "-start_number", str(self.start_segment),
                "-hls_segment_filename", segment_pattern,
                playlist_path
            ])

            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.last_active = time.time()

    def is_segment_ready(self, segment_idx: int) -> bool:
        """Determines if a segment file is committed to disk and closed by FFmpeg."""
        seg_file = os.path.join(self.output_dir, f"seg_{segment_idx:04d}.ts")
        if not os.path.exists(seg_file) or os.path.getsize(seg_file) == 0:
            return False

        # If next segment exists, current segment is guaranteed complete
        next_seg = os.path.join(self.output_dir, f"seg_{segment_idx + 1:04d}.ts")
        if os.path.exists(next_seg) and os.path.getsize(next_seg) > 0:
            return True

        # Or if listed in prog.m3u8
        m3u8_file = os.path.join(self.output_dir, "prog.m3u8")
        if os.path.exists(m3u8_file):
            try:
                with open(m3u8_file, "r") as f:
                    if f"seg_{segment_idx:04d}.ts" in f.read():
                        return True
            except Exception:
                pass

        # If process has already finished, check if file exists and has size
        if self.process and self.process.poll() is not None:
            return os.path.exists(seg_file) and os.path.getsize(seg_file) > 0

        return False

    def get_latest_segment_idx(self) -> int:
        """Returns the highest segment index that has been produced or is in progress."""
        if not os.path.exists(self.output_dir):
            return self.start_segment - 1
        try:
            m3u8_file = os.path.join(self.output_dir, "prog.m3u8")
            if os.path.exists(m3u8_file):
                with open(m3u8_file, "r") as f:
                    content = f.read()
                indices = [int(m) for m in re.findall(r'seg_(\d{4})\.ts', content)]
                if indices:
                    return max(indices)
            segs = [
                int(f[4:8]) for f in os.listdir(self.output_dir)
                if f.startswith("seg_") and f.endswith(".ts") and len(f) >= 8 and f[4:8].isdigit()
            ]
            return max(segs) if segs else self.start_segment - 1
        except Exception:
            return self.start_segment - 1

    def stop(self, cleanup_files: bool = False):
        """Terminates the FFmpeg transcode process and optionally cleans up transcode files."""
        with self.lock:
            self.is_stopped = True
            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=2.0)
                except Exception:
                    try:
                        self.process.kill()
                        self.process.wait(timeout=1.0)
                    except Exception:
                        pass
                self.process = None
            if cleanup_files and self.output_dir and os.path.exists(self.output_dir):
                try:
                    shutil.rmtree(self.output_dir, ignore_errors=True)
                except Exception:
                    pass

class HLSManager:
    """Coordinates HLS playlists, segment generation, worker lifecycles, and cleanup."""
    def __init__(self):
        self.workers: Dict[str, HLSTranscodeWorker] = {}
        self.lock = threading.Lock()
        self._start_watchdog()

    def cleanup_all_transcodes(self):
        """Cleans up all cached transcode directories."""
        cache_base = get_hls_cache_dir()
        if os.path.exists(cache_base):
            try:
                for entry in os.listdir(cache_base):
                    entry_path = os.path.join(cache_base, entry)
                    if os.path.isdir(entry_path):
                        shutil.rmtree(entry_path, ignore_errors=True)
            except Exception:
                pass

    def cleanup_stale_transcodes(self, max_age_seconds: float = 120.0):
        """
        Removes transcode session directories that do not belong to active workers
        and have not been modified within max_age_seconds.
        """
        cache_base = get_hls_cache_dir()
        if not os.path.exists(cache_base):
            return

        with self.lock:
            active_dirs = {os.path.abspath(w.output_dir) for w in self.workers.values()}

        now = time.time()
        try:
            for entry in os.listdir(cache_base):
                entry_path = os.path.abspath(os.path.join(cache_base, entry))
                if not os.path.isdir(entry_path):
                    continue
                if entry_path in active_dirs:
                    continue
                try:
                    mtime = os.path.getmtime(entry_path)
                    if now - mtime > max_age_seconds:
                        shutil.rmtree(entry_path, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _start_watchdog(self):
        def watchdog_loop():
            while True:
                time.sleep(15.0)
                now = time.time()
                with self.lock:
                    to_remove = []
                    for key, worker in list(self.workers.items()):
                        # Inactivity timeout: 60 seconds without any segment requests
                        if now - worker.last_active > 60.0:
                            worker.stop(cleanup_files=True)
                            to_remove.append(key)
                    for key in to_remove:
                        self.workers.pop(key, None)
                self.cleanup_stale_transcodes(max_age_seconds=300.0)

        t = threading.Thread(target=watchdog_loop, daemon=True)
        t.start()

    def build_master_playlist(
        self,
        media_type: str,
        item_id: int,
        file_path: str,
        audio_track: int = 0,
        token: Optional[str] = None
    ) -> str:
        """Constructs an HLS Master Playlist listing all available quality variants for ABR."""
        probe = probe_media_streams(file_path)
        source_w = probe.get("width") or 1920
        source_h = probe.get("height") or 1080
        source_bitrate = probe.get("bitrate") or 4000000

        # Define candidate resolution tiers
        tiers = [
            ("original", source_w, source_h, source_bitrate, "Original"),
            ("1080", 1920, 1080, min(source_bitrate, 6000000), "1080p"),
            ("720", 1280, 720, min(source_bitrate, 3000000), "720p"),
            ("480", 854, 480, min(source_bitrate, 1500000), "480p"),
            ("360", 640, 360, min(source_bitrate, 800000), "360p")
        ]

        # Filter tiers so we don't offer higher resolution transcode than original
        active_tiers = []
        for res_key, w, h, br, name in tiers:
            if res_key == "original":
                active_tiers.append((res_key, w, h, br, name))
            elif h < source_h or (h == source_h and res_key != "1080" and source_h != 1080):
                active_tiers.append((res_key, w, h, br, name))
            elif h <= source_h:
                active_tiers.append((res_key, w, h, br, name))

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            ""
        ]

        token_param = f"&token={token}" if token else ""

        for res_key, w, h, br, name in active_tiers:
            codec_str = 'avc1.640028,mp4a.40.2' if h >= 1080 else 'avc1.4d401f,mp4a.40.2'
            lines.append(
                f'#EXT-X-STREAM-INF:BANDWIDTH={int(br * 1.15)},AVERAGE-BANDWIDTH={int(br)},'
                f'RESOLUTION={w}x{h},CODECS="{codec_str}",NAME="{name}"'
            )
            lines.append(f"variant_{res_key}.m3u8?audio_track={audio_track}{token_param}")
            lines.append("")

        return "\n".join(lines)

    def build_variant_playlist(
        self,
        media_type: str,
        item_id: int,
        res: str,
        duration: float,
        audio_track: int = 0,
        token: Optional[str] = None
    ) -> str:
        """Constructs an HLS VOD Media Playlist mapping all segments across total duration."""
        if duration <= 0:
            duration = 10.0

        num_segments = int(math.ceil(duration / SEGMENT_DURATION))
        target_dur = int(math.ceil(SEGMENT_DURATION)) + 1

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target_dur}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            ""
        ]

        token_param = f"&token={token}" if token else ""

        for i in range(num_segments):
            seg_start = i * SEGMENT_DURATION
            seg_len = min(SEGMENT_DURATION, duration - seg_start)
            lines.append(f"#EXTINF:{seg_len:.6f},")
            lines.append(f"segment_{res}_{i}.ts?audio_track={audio_track}{token_param}")

        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines)

    def get_segment(
        self,
        media_type: str,
        item_id: int,
        file_path: str,
        res: str,
        audio_track: int,
        segment_idx: int,
        hw_profile: str = "auto"
    ) -> Optional[str]:
        """
        Retrieves a completed .ts segment, spawning or advancing the transcode worker as needed.
        """
        cache_base = get_hls_cache_dir()
        session_folder = f"{media_type}_{item_id}_{res}_a{audio_track}"
        output_dir = os.path.join(cache_base, session_folder)
        target_seg = os.path.join(output_dir, f"seg_{segment_idx:04d}.ts")

        # 1. If segment already on disk and finalized, return immediately
        worker_key = f"{media_type}_{item_id}_{res}_{audio_track}"
        with self.lock:
            existing_worker = self.workers.get(worker_key)
            if existing_worker:
                existing_worker.last_active = time.time()
                if existing_worker.is_segment_ready(segment_idx):
                    return target_seg

        if os.path.exists(target_seg) and os.path.getsize(target_seg) > 0:
            m3u8_file = os.path.join(output_dir, "prog.m3u8")
            if os.path.exists(m3u8_file):
                try:
                    with open(m3u8_file, "r") as f:
                        if f"seg_{segment_idx:04d}.ts" in f.read():
                            return target_seg
                except Exception:
                    pass
            next_seg = os.path.join(output_dir, f"seg_{segment_idx + 1:04d}.ts")
            if os.path.exists(next_seg) and os.path.getsize(next_seg) > 0:
                return target_seg

        # 2. Worker coordination
        worker_to_start = None
        with self.lock:
            worker = self.workers.get(worker_key)
            latest_idx = worker.get_latest_segment_idx() if worker else -1
            # Check if current worker is producing towards segment_idx
            # The worker is kept only if currently running and segment_idx is within its immediate queue:
            # between worker.start_segment and (latest_idx + 2).
            # If segment_idx is beyond latest_idx + 2, the user has sought forward;
            # if segment_idx < worker.start_segment, the user has sought backward.
            if (
                worker and 
                not worker.is_stopped and
                worker.process and 
                worker.process.poll() is None and
                worker.start_segment <= segment_idx <= (latest_idx + 2)
            ):
                worker.last_active = time.time()
            else:
                # Stop existing worker (seek occurred or brand new session)
                if worker:
                    worker.stop()
                # Create and start new worker from target segment
                worker = HLSTranscodeWorker(
                    session_key=worker_key,
                    media_path=file_path,
                    output_dir=output_dir,
                    res=res,
                    audio_track=audio_track,
                    start_segment=segment_idx,
                    hw_profile=hw_profile
                )
                self.workers[worker_key] = worker
                worker_to_start = worker

        if worker_to_start:
            worker_to_start.start()

        # 3. Wait for segment to be finalized (up to 45s timeout for heavily loaded hosts)
        wait_start = time.time()
        while time.time() - wait_start < 45.0:
            if worker.is_segment_ready(segment_idx):
                return target_seg
            if worker.process and worker.process.poll() is not None:
                # Process exited; check if file was produced
                if os.path.exists(target_seg) and os.path.getsize(target_seg) > 0:
                    return target_seg
                print(f"[HLSManager] Worker process for {worker.session_key} exited with code {worker.process.returncode} before segment {segment_idx} was ready.")
                break
            time.sleep(0.05)

        return target_seg if (os.path.exists(target_seg) and os.path.getsize(target_seg) > 0) else None

    def stop_session(self, media_type: str, item_id: int):
        """Stops any active HLS transcode workers for the given media item and cleans up files."""
        with self.lock:
            prefix = f"{media_type}_{item_id}_"
            to_remove = []
            for key, worker in self.workers.items():
                if key.startswith(prefix):
                    worker.stop(cleanup_files=True)
                    to_remove.append(key)
            for k in to_remove:
                self.workers.pop(k, None)

        cache_base = get_hls_cache_dir()
        if os.path.exists(cache_base):
            try:
                prefix = f"{media_type}_{item_id}_"
                for entry in os.listdir(cache_base):
                    if entry.startswith(prefix):
                        shutil.rmtree(os.path.join(cache_base, entry), ignore_errors=True)
            except Exception:
                pass

    def shutdown(self):
        """Kills all active workers and cleans up transcode files on server shutdown."""
        with self.lock:
            for worker in self.workers.values():
                worker.stop(cleanup_files=True)
            self.workers.clear()
        self.cleanup_all_transcodes()

hls_manager = HLSManager()
