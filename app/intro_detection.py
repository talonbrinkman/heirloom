"""
Automated Audio Fingerprinting & Intro/Outro Detection Module for Heirloom.

Cross-correlates audio spectral energy features across episodes in a season to detect
theme songs (intros) and uses audio fingerprinting / silence detection for end credits.
Runs in a low-priority background thread pool with zero impact on streaming.
"""

import os
import sys
import json
import time
import math
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Tuple, Dict, Any, Set

from sqlmodel import Session, select
from .database import engine
from .models import TVShow, Movie, SystemConfig
from .ffmpeg_setup import get_ffmpeg_path

# -----------------------------------------------------------------------------
# Configuration Constants
# -----------------------------------------------------------------------------
SAMPLE_RATE = 8000           # 8 kHz mono audio is ideal for subband energy up to 3.6 kHz
HOP_SEC = 0.2                # 200 ms per time frame (5 frames per second)
N_BANDS = 16                 # 16 log-spaced frequency bands
INTRO_SEARCH_SEC = 720.0     # Search first 12 minutes of each episode for intros
OUTRO_SEARCH_SEC = 300.0     # Search last 5 minutes of each episode for credits
MIN_INTRO_DURATION = 15.0    # Intros must be at least 15 seconds
MAX_INTRO_DURATION = 120.0   # Intros rarely exceed 2 minutes
MIN_CORRELATION_CONFIDENCE = 0.38  # Confidence threshold for consensus matches

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="intro_detector")
_active_tasks: Set[str] = set()
_tasks_lock = threading.Lock()


def _get_subprocess_priority_kwargs() -> dict:
    """Returns platform-specific subprocess kwargs to lower process priority."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    else:
        kwargs["preexec_fn"] = lambda: os.nice(10) if hasattr(os, "nice") else None
    return kwargs


def is_auto_intro_detection_enabled(session: Optional[Session] = None) -> bool:
    """Checks SystemConfig to see if automated intro/outro detection is enabled. Defaults to True (On)."""
    close_session = False
    if session is None:
        session = Session(engine)
        close_session = True

    try:
        config = session.exec(select(SystemConfig).where(SystemConfig.key == "auto_intro_detection")).first()
        if not config:
            return True
        val = str(config.value).strip().lower()
        return val in ("true", "1", "on", "yes", "enabled")
    except Exception:
        return True
    finally:
        if close_session:
            session.close()


# -----------------------------------------------------------------------------
# Audio Feature Extraction
# -----------------------------------------------------------------------------

def extract_audio_pcm(file_path: str, start_sec: float, duration_sec: float, sample_rate: int = SAMPLE_RATE) -> Optional[Any]:
    """
    Extracts a mono PCM audio slice from a video file into a numpy float32 array.
    """
    try:
        import numpy as np
    except ImportError:
        print("[IntroDetection] NumPy is required for audio fingerprinting.")
        return None

    if not os.path.exists(file_path):
        return None

    ffmpeg_bin = get_ffmpeg_path()
    cmd = [
        ffmpeg_bin,
        "-v", "error",
        "-ss", f"{max(0.0, start_sec):.2f}",
        "-t", f"{duration_sec:.2f}",
        "-i", file_path,
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "s16le",
        "pipe:1"
    ]

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=40,
            **_get_subprocess_priority_kwargs()
        )
        if res.returncode != 0 or not res.stdout:
            return None

        raw = np.frombuffer(res.stdout, dtype=np.int16)
        if len(raw) < sample_rate * 2:  # Less than 2 seconds of audio
            return None
        return raw.astype(np.float32) / 32768.0
    except Exception as e:
        print(f"[IntroDetection] Error extracting audio PCM from {file_path}: {e}")
        return None


def compute_spectral_fingerprint(audio: Any, sr: int = SAMPLE_RATE, hop_sec: float = HOP_SEC, n_bands: int = N_BANDS) -> Optional[Any]:
    """
    Computes a 2D log-energy spectrogram feature matrix normalized per frequency band.
    Shape: (n_frames, n_bands).
    """
    try:
        import numpy as np
    except ImportError:
        return None

    hop_samples = int(sr * hop_sec)
    n_frames = len(audio) // hop_samples
    if n_frames < 20:
        return None

    frames = audio[:n_frames * hop_samples].reshape(n_frames, hop_samples)
    window = np.hanning(hop_samples)
    windowed = frames * window

    fft_mag = np.abs(np.fft.rfft(windowed, axis=1))
    fft_freqs = np.fft.rfftfreq(hop_samples, d=1.0 / sr)

    band_edges = np.geomspace(80, min(3600, sr / 2 - 100), n_bands + 1)
    features = np.zeros((n_frames, n_bands), dtype=np.float32)

    for b in range(n_bands):
        mask = (fft_freqs >= band_edges[b]) & (fft_freqs < band_edges[b + 1])
        if np.any(mask):
            features[:, b] = np.log1p(np.mean(fft_mag[:, mask], axis=1))

    # Standardize each band over time (zero mean, unit variance)
    mean = np.mean(features, axis=0, keepdims=True)
    std = np.std(features, axis=0, keepdims=True) + 1e-6
    return (features - mean) / std


# -----------------------------------------------------------------------------
# Cross-Episode Correlation & Segment Detection
# -----------------------------------------------------------------------------

def correlate_pair(f1: Any, f2: Any, hop_sec: float = HOP_SEC) -> Optional[Tuple[float, float, float, float, float]]:
    """
    Cross-correlates two feature matrices and detects matching recurring audio segments.
    Returns: (start_time_1, end_time_1, start_time_2, end_time_2, confidence)
    """
    try:
        import numpy as np
    except ImportError:
        return None

    len1, len2 = len(f1), len(f2)
    if len1 < 50 or len2 < 50:
        return None

    n = len1 + len2 - 1
    fft1 = np.fft.rfft(f1, n, axis=0)
    fft2 = np.fft.rfft(f2[::-1], n, axis=0)
    corr_bands = np.fft.irfft(fft1 * fft2, n, axis=0)
    total_corr = np.sum(corr_bands, axis=1)

    best_lag_idx = int(np.argmax(total_corr))
    best_lag = best_lag_idx - len2 + 1

    # Align frames at best lag
    if best_lag >= 0:
        t_1 = np.arange(best_lag, min(len1, len2 + best_lag))
        t_2 = t_1 - best_lag
    else:
        t_2 = np.arange(-best_lag, min(len2, len1 - best_lag))
        t_1 = t_2 + best_lag

    if len(t_1) < int(MIN_INTRO_DURATION / hop_sec):
        return None

    sim = np.sum(f1[t_1] * f2[t_2], axis=1) / f1.shape[1]
    
    # Moving average filter of ~5 seconds
    kernel_size = max(5, int(5.0 / hop_sec))
    kernel = np.ones(kernel_size) / kernel_size
    smoothed = np.convolve(sim, kernel, mode="same")

    threshold = max(0.35, float(np.mean(smoothed) + 1.2 * np.std(smoothed)))
    above = smoothed > threshold

    runs = []
    cur_start = None
    for i, val in enumerate(above):
        if val and cur_start is None:
            cur_start = i
        elif not val and cur_start is not None:
            runs.append((cur_start, i))
            cur_start = None
    if cur_start is not None:
        runs.append((cur_start, len(above)))

    min_frames = int(MIN_INTRO_DURATION / hop_sec)
    max_frames = int(MAX_INTRO_DURATION / hop_sec)
    valid_runs = [r for r in runs if min_frames <= (r[1] - r[0]) <= max_frames]

    if not valid_runs:
        return None

    # Pick run with highest total similarity
    best_run = max(valid_runs, key=lambda r: np.sum(smoothed[r[0]:r[1]]))
    confidence = float(np.mean(smoothed[best_run[0]:best_run[1]]))

    if confidence < MIN_CORRELATION_CONFIDENCE:
        return None

    s1 = float(t_1[best_run[0]] * hop_sec)
    e1 = float(t_1[best_run[1] - 1] * hop_sec)
    s2 = float(t_2[best_run[0]] * hop_sec)
    e2 = float(t_2[best_run[1] - 1] * hop_sec)

    return s1, e1, s2, e2, confidence


# -----------------------------------------------------------------------------
# Silence & Credit Detection
# -----------------------------------------------------------------------------

def detect_silence_credits(file_path: str, duration: float, search_window: float = OUTRO_SEARCH_SEC) -> Optional[float]:
    """
    Uses FFmpeg silencedetect over the last few minutes to find transition to rolling credits.
    """
    if not duration or duration < 120:
        return None

    search_start = max(0.0, duration - search_window)
    ffmpeg_bin = get_ffmpeg_path()
    cmd = [
        ffmpeg_bin,
        "-v", "info",
        "-ss", f"{search_start:.2f}",
        "-t", f"{search_window:.2f}",
        "-i", file_path,
        "-af", "silencedetect=noise=-32dB:d=0.7",
        "-f", "null",
        "-"
    ]

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            **_get_subprocess_priority_kwargs()
        )
        output = res.stderr or ""
        
        # Parse silence events
        silence_ends = []
        for match in re.finditer(r"silence_end:\s*([0-9.]+)", output):
            s_end = float(match.group(1)) + search_start
            silence_ends.append(s_end)

        # Look for silence transition between 25s and 260s before end of file
        candidates = [t for t in silence_ends if 25.0 <= (duration - t) <= 260.0]
        if candidates:
            # Pick the transition closest to the end (typically between last scene & credits)
            return round(candidates[-1], 2)
    except Exception as e:
        print(f"[IntroDetection] Error detecting silence credits for {file_path}: {e}")

    return None


# -----------------------------------------------------------------------------
# Season Cross-Episode Fingerprinting Pipeline
# -----------------------------------------------------------------------------

def process_season_intros(show_title: str, season_num: int) -> bool:
    """
    Processes all episodes of a TV season: extracts fingerprints, correlates them to find
    the intro theme song, detects end credits, and updates the database.
    """
    with Session(engine) as session:
        if not is_auto_intro_detection_enabled(session):
            print(f"[IntroDetection] Auto intro detection is disabled in settings. Skipping {show_title} S{season_num}.")
            return False

        episodes = session.exec(
            select(TVShow)
            .where(TVShow.title == show_title, TVShow.season == season_num)
            .order_by(TVShow.episode)
        ).all()

        if not episodes or len(episodes) < 2:
            # If only 1 episode exists, we can still detect credits via silence
            if len(episodes) == 1:
                ep = episodes[0]
                if ep.credits_start is None and os.path.exists(ep.file_path):
                    from .main import get_video_duration_seconds
                    dur = get_video_duration_seconds(ep.file_path) or 0.0
                    cred_s = detect_silence_credits(ep.file_path, dur)
                    if cred_s:
                        ep.credits_start = cred_s
                        session.add(ep)
                        session.commit()
                        print(f"[IntroDetection] Silence credits detected for {show_title} S{season_num}E{ep.episode}: {cred_s:.1f}s")
            return False

    print(f"[IntroDetection] Starting automated intro/outro detection for '{show_title}' Season {season_num} ({len(episodes)} episodes)...")

    # Step 1: Extract intro audio fingerprints (first 12 minutes)
    fingerprints = {}
    durations = {}

    for ep in episodes:
        if not os.path.exists(ep.file_path):
            continue
        from .main import get_video_duration_seconds
        dur = get_video_duration_seconds(ep.file_path) or 0.0
        durations[ep.id] = dur

        # If intro already detected via chapters, skip audio fingerprinting
        if ep.intro_start is not None and ep.intro_end is not None:
            continue

        pcm = extract_audio_pcm(ep.file_path, start_sec=0.0, duration_sec=INTRO_SEARCH_SEC)
        if pcm is not None:
            fp = compute_spectral_fingerprint(pcm)
            if fp is not None:
                fingerprints[ep.id] = fp

    # Step 2: Cross-correlate pairs of episodes to find common intro theme
    matches = {}  # ep_id -> list of (start, end)
    ep_ids = list(fingerprints.keys())

    # Test up to first 5 episodes against each other for fast high-confidence convergence
    sample_ids = ep_ids[:5]
    for i in range(len(sample_ids)):
        for j in range(i + 1, len(sample_ids)):
            id1, id2 = sample_ids[i], sample_ids[j]
            res = correlate_pair(fingerprints[id1], fingerprints[id2])
            if res:
                s1, e1, s2, e2, conf = res
                matches.setdefault(id1, []).append((s1, e1))
                matches.setdefault(id2, []).append((s2, e2))

    # Calculate consensus intro duration and update matching episodes
    detected_intros = {}
    for ep_id, windows in matches.items():
        if len(windows) >= 1:
            # Average the detected start and end times for this episode
            avg_start = round(sum(w[0] for w in windows) / len(windows), 2)
            avg_end = round(sum(w[1] for w in windows) / len(windows), 2)
            if (avg_end - avg_start) >= MIN_INTRO_DURATION:
                detected_intros[ep_id] = (avg_start, avg_end)

    # For any remaining episodes in the season, correlate against the confirmed theme from episode 1
    if detected_intros and sample_ids:
        ref_id = sample_ids[0]
        if ref_id in detected_intros:
            ref_fp = fingerprints.get(ref_id)
            if ref_fp is not None:
                for rem_id in ep_ids:
                    if rem_id not in detected_intros and rem_id in fingerprints:
                        res = correlate_pair(ref_fp, fingerprints[rem_id])
                        if res:
                            _, _, s_rem, e_rem, _ = res
                            if (e_rem - s_rem) >= MIN_INTRO_DURATION:
                                detected_intros[rem_id] = (round(s_rem, 2), round(e_rem, 2))

    # Step 3: Outro / Credits Detection
    detected_credits = {}
    for ep in episodes:
        if ep.credits_start is not None:
            continue
        dur = durations.get(ep.id, 0.0)
        if dur > 120 and os.path.exists(ep.file_path):
            cred_s = detect_silence_credits(ep.file_path, dur)
            if cred_s:
                detected_credits[ep.id] = cred_s

    # Step 4: Commit results to database
    updated_count = 0
    with Session(engine) as session:
        for ep in episodes:
            updated = False
            if ep.id in detected_intros and ep.intro_start is None:
                ep_db = session.get(TVShow, ep.id)
                if ep_db:
                    ep_db.intro_start, ep_db.intro_end = detected_intros[ep.id]
                    session.add(ep_db)
                    updated = True

            if ep.id in detected_credits and ep.credits_start is None:
                ep_db = session.get(TVShow, ep.id)
                if ep_db:
                    ep_db.credits_start = detected_credits[ep.id]
                    session.add(ep_db)
                    updated = True

            if updated:
                updated_count += 1

        try:
            session.commit()
            print(f"[IntroDetection] Complete for '{show_title}' S{season_num}: Updated {updated_count} episodes with intro/outro markers.")
        except Exception as e:
            session.rollback()
            print(f"[IntroDetection] Error saving intro markers: {e}")

    return True


# -----------------------------------------------------------------------------
# Background Task Queueing
# -----------------------------------------------------------------------------

def queue_season_intro_detection(show_title: str, season_num: int) -> bool:
    """Enqueues automated intro detection for a given TV show season in the background worker."""
    task_key = f"tv_{show_title}_s{season_num}"
    with _tasks_lock:
        if task_key in _active_tasks:
            return False
        _active_tasks.add(task_key)

    def _worker():
        try:
            process_season_intros(show_title, season_num)
        except Exception as e:
            print(f"[IntroDetection] Worker exception for {task_key}: {e}")
        finally:
            with _tasks_lock:
                _active_tasks.discard(task_key)

    _executor.submit(_worker)
    return True


def queue_media_intro_detection(media_type: str, item_id: int, file_path: str, duration: float) -> bool:
    """
    On-demand background hook called when an item is played.
    If TV episode, queues season detection. If Movie, runs silence credits detection.
    """
    if not is_auto_intro_detection_enabled():
        return False

    if media_type == "tv":
        with Session(engine) as session:
            show = session.get(TVShow, item_id)
            if show and show.title and show.season is not None:
                return queue_season_intro_detection(show.title, show.season)
        return False

    elif media_type == "movie":
        task_key = f"movie_{item_id}"
        with _tasks_lock:
            if task_key in _active_tasks:
                return False
            _active_tasks.add(task_key)

        def _worker():
            try:
                with Session(engine) as session:
                    mov = session.get(Movie, item_id)
                    if mov and mov.credits_start is None and duration > 120 and os.path.exists(file_path):
                        cred_s = detect_silence_credits(file_path, duration)
                        if cred_s:
                            mov.credits_start = cred_s
                            session.add(mov)
                            session.commit()
                            print(f"[IntroDetection] Detected movie credits for '{mov.title}': {cred_s:.1f}s")
            except Exception as e:
                print(f"[IntroDetection] Worker error for movie {item_id}: {e}")
            finally:
                with _tasks_lock:
                    _active_tasks.discard(task_key)

        _executor.submit(_worker)
        return True

    return False

