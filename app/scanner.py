import os
import sys
import re
import json
import time
import threading
import asyncio
import subprocess
import hashlib
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
import requests
try:
    import httpx
except ImportError:
    httpx = None

from sqlmodel import Session, select
from sqlalchemy import func
from .database import engine
from .models import Movie, TVShow, Photo, VolumePath, SystemConfig, Artist, Album, Track, Playlist, PlaylistTrack

def _get_subprocess_priority_kwargs() -> Dict[str, Any]:
    """Returns platform-specific subprocess kwargs to lower process priority for background ffmpeg/ffprobe tasks."""
    kwargs: Dict[str, Any] = {}
    if sys.platform == "win32":
        try:
            kwargs["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
        except Exception:
            pass
    elif hasattr(os, "nice"):
        kwargs["preexec_fn"] = lambda: os.nice(10)
    return kwargs

def normalize_artist_name(name: Optional[str]) -> str:
    """
    Normalizes artist names by stripping featured artist suffixes and normalizing
    stylized typography (e.g. 'KoЯn' -> 'Korn', 'Korn feat. Skrillex' -> 'Korn').
    """
    if not name:
        return "Unknown Artist"
    s = str(name).strip()
    
    # Normalize KoЯn / Koяn -> Korn
    s = re.sub(r'Ko[Яя]n', 'Korn', s, flags=re.IGNORECASE)
    # Replace any other stylized Cyrillic letters commonly used in artist names
    s = s.replace("Я", "R").replace("я", "r")
    
    # Remove parenthesized or bracketed features: (feat. ...), [ft. ...], (featuring ...)
    s = re.sub(r'\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+.*?[\]\)]', '', s, flags=re.IGNORECASE)
    
    # Remove unparenthesized featured artists: feat. ..., ft. ..., featuring ...
    s = re.sub(r'\s+(?:feat\.?|ft\.?|featuring)\s+.*$', '', s, flags=re.IGNORECASE)
    
    s = s.strip().rstrip(",-&/ ")
    return s if s else str(name).strip()

def extract_feature_info(raw_artist: str, raw_title: str) -> Tuple[str, str]:
    """
    Extracts the primary artist and any featured artist. If the track title doesn't
    already mention the featured artist, appends it nicely to the title so users still see
    who was featured on the track without splitting artists or albums.
    """
    primary = normalize_artist_name(raw_artist)
    clean_title = raw_title.strip() if raw_title else ""
    
    feature_match = re.search(r'(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+)', raw_artist, flags=re.IGNORECASE)
    if feature_match and clean_title:
        featured_who = feature_match.group(1).strip().rstrip(")] ")
        if not re.search(r'\b(?:feat\.?|ft\.?|featuring)\b', clean_title, flags=re.IGNORECASE):
            clean_title = f"{clean_title} (feat. {featured_who})"
            
    return primary, clean_title

def normalize_album_title(title: Optional[str]) -> Tuple[str, Optional[int]]:
    """
    Normalizes multi-disc album titles by stripping trailing CD/Disc notations
    (e.g., 'Issues (1)' -> 'Issues', 'See You On The Other Side (CD1)' -> 'See You On The Other Side')
    and extracting the disc number.
    """
    if not title:
        return "Unknown Album", None
    s = str(title).strip()
    disc = None

    # Pattern 1: (CD 1), [Disc 2], (Enhanced CD 01), [CD 2 - Bonus], etc.
    m = re.search(r'\s*[\(\[]\s*(?:enhanced\s+|bonus\s+)?(?:cd|disc|disk)\s*0*([1-9]\d?)[^\)\]]*[\]\)]\s*$', s, re.IGNORECASE)
    if m:
        disc = int(m.group(1))
        s = s[:m.start()].strip()
        return s or str(title).strip(), disc

    # Pattern 2: (Bonus Disc), [Bonus CD]
    m = re.search(r'\s*[\(\[]\s*bonus\s+(?:cd|disc|disk)[^\)\]]*[\]\)]\s*$', s, re.IGNORECASE)
    if m:
        disc = 2
        s = s[:m.start()].strip()
        return s or str(title).strip(), disc

    # Pattern 3: - CD 1, : Disc 2, , CD 1
    m = re.search(r'\s*[-–—:,]\s*(?:enhanced\s+|bonus\s+)?(?:cd|disc|disk)\s*0*([1-9]\d?)(?:\s+.*)?$', s, re.IGNORECASE)
    if m:
        disc = int(m.group(1))
        s = s[:m.start()].strip()
        return s or str(title).strip(), disc

    # Pattern 4: Trailing ' CD 1' or ' Disc 2'
    m = re.search(r'\s+(?:enhanced\s+|bonus\s+)?(?:cd|disc|disk)\s*0*([1-9]\d?)(?:\s+.*)?$', s, re.IGNORECASE)
    if m:
        disc = int(m.group(1))
        s = s[:m.start()].strip()
        return s or str(title).strip(), disc

    # Pattern 5: (1), (2), [1] at end of title (1-2 digits only, avoiding (1999) years)
    m = re.search(r'\s*[\(\[]\s*0*([1-9]\d?)\s*[\]\)]\s*$', s)
    if m and len(s[:m.start()].strip()) > 1:
        disc = int(m.group(1))
        s = s[:m.start()].strip()
        return s or str(title).strip(), disc

    return s, disc

def is_silence_or_filler_track(title: Optional[str], file_path: Optional[str] = "", duration: Optional[float] = 0.0) -> bool:
    """
    Detects whether an audio track is an uncredited, dummy silence or padding track
    (such as the 12 5-second silence tracks at the start of Korn's 'Follow the Leader',
    or pre-gap filler tracks found on 90s CDs).
    """
    title_str = (title or "").strip()
    title_lower = title_str.lower()
    fname = os.path.basename(file_path or "").lower()
    dur = float(duration) if duration is not None else 0.0

    # 1. Exact or bracketed metadata tags indicating silence/filler: [silence], <silence>, (silence), [blank], [empty track]
    silence_tags = (
        "[silence]", "<silence>", "(silence)", "[blank]", "[empty track]",
        "[silence track]", "[untitled silence]", "[hidden track]", "[untitled]"
    )
    if title_lower in silence_tags and dur <= 60.0:
        return True

    # 2. Track titled "silence" or "silent" (with or without brackets) and very short duration (<= 30s or 0)
    # Prevents false positives for legitimate songs titled "Silence" which are 2+ minutes long.
    if title_lower in ("silence", "silent", "[silence]", "blank", "filler") and (dur <= 30.0 or dur == 0.0):
        return True

    # 3. Filename indicates silence (e.g. "Korn - Follow the Leader - 01 - [silence].flac", "01 silence.mp3")
    # AND duration is short (<= 30s) or unmeasured
    if (re.search(r'[\[\(<_\-\s]silence[\]\)>_\-\s\.]', fname) or fname.startswith("silence.")) and (dur <= 30.0 or dur == 0.0):
        return True

    # 4. Short dummy filler with 'silence' in title: e.g. "Silence (5 seconds)" or "[Silence 01]"
    if "silence" in title_lower and dur <= 15.0:
        return True

    return False

class SafeHTTPClient:
    """Async-compatible HTTP client that uses httpx when installed, falling back to threaded requests."""
    def __init__(self, raw_client=None):
        self._client = raw_client

    async def get(self, url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0):
        if self._client is not None:
            return await self._client.get(url, params=params, timeout=timeout)
        else:
            def _sync_get():
                return requests.get(url, params=params, timeout=timeout)
            return await asyncio.to_thread(_sync_get)

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".m4v")
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")
AUDIO_EXTENSIONS = (
    ".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".oga", ".opus",
    ".wav", ".wave", ".wma", ".aiff", ".aif", ".ape", ".wv", ".m4b", ".dsf", ".dff", ".mpc"
)
METADATA_DIR = "/config/metadata"

RECYCLE_NAMES = {
    "#recycle", "@recycle", "$recycle.bin", "recycle.bin", "recycler",
    ".trash", ".trashes", "@eadir"
}

def is_recycle_name(name: str) -> bool:
    """Check if a directory or file name corresponds to recycle/trash/indexing artifacts."""
    if not name:
        return False
    lower = name.strip().lower()
    if lower in RECYCLE_NAMES:
        return True
    if "recycle" in lower:
        return True
    if lower.startswith(".trash-"):
        return True
    if lower == "@eadir" or lower.startswith("@eadir"):
        return True
    return False

def is_recycle_path(path: str) -> bool:
    """Check if any segment in the path belongs to a recycle or trash folder."""
    if not path:
        return False
    norm = path.replace("\\", "/")
    segments = [s.strip() for s in norm.split("/") if s.strip()]
    return any(is_recycle_name(s) for s in segments)

def get_album_name(root: str, photo_dir: str) -> Optional[str]:
    """
    Returns the relative album path for a photo directory preserving folder structure.
    If root is directly inside photo_dir, returns None.
    If root is a nested subfolder (e.g. Vacation/2023/Day1), returns 'Vacation/2023/Day1'.
    """
    if not root or not photo_dir:
        return None
    norm_root = root.replace("\\", "/").rstrip("/")
    norm_dir = photo_dir.replace("\\", "/").rstrip("/")
    
    if norm_root.lower() == norm_dir.lower():
        return None

    if norm_root.lower().startswith(norm_dir.lower() + "/"):
        rel_path = norm_root[len(norm_dir) + 1:]
    else:
        try:
            rel_path = os.path.relpath(root, photo_dir).replace("\\", "/")
        except ValueError:
            return None

    if rel_path == "." or not rel_path:
        return None
    parts = [p for p in rel_path.split("/") if p and p != "."]
    if not parts or any(is_recycle_name(p) for p in parts):
        return None
    return "/".join(parts)

def extract_date_taken(filepath: str) -> Optional[datetime]:
    """
    Extract the date and time the photo or video was taken.
    Checks:
    1. EXIF metadata (DateTimeOriginal, DateTimeDigitized, DateTime) via Pillow.
    2. Video creation_time tag via ffprobe.
    3. Filename patterns containing YYYYMMDD or YYYY-MM-DD.
    4. File filesystem birthtime / mtime.
    """
    if not filepath or not os.path.exists(filepath):
        return None

    ext = os.path.splitext(filepath)[1].lower()

    # 1. EXIF metadata for images
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".tiff"):
        try:
            from PIL import Image
            with Image.open(filepath) as img:
                exif = img.getexif()
                if exif:
                    date_str = None
                    try:
                        exif_ifd = exif.get_ifd(0x8769)
                        if exif_ifd:
                            date_str = exif_ifd.get(36867) or exif_ifd.get(36868)
                    except Exception:
                        pass
                    if not date_str:
                        date_str = exif.get(36867) or exif.get(36868) or exif.get(306)

                    if date_str and isinstance(date_str, str):
                        clean_str = date_str.strip().split("\x00")[0]
                        parts = clean_str.split(" ")
                        if len(parts) >= 1:
                            date_part = parts[0].replace(":", "-")
                            time_part = parts[1] if len(parts) > 1 else "00:00:00"
                            time_part_clean = time_part.split("+")[0].split("-")[0].replace("Z", "")
                            dt = datetime.fromisoformat(f"{date_part}T{time_part_clean}")
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            return dt
        except Exception:
            pass

    # 2. Video creation_time via ffprobe
    if ext in (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"):
        try:
            from .ffmpeg_setup import get_ffprobe_path
            cmd = [
                get_ffprobe_path(), "-v", "quiet",
                "-print_format", "json",
                "-show_entries", "format_tags=creation_time:stream_tags=creation_time",
                filepath
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, **_get_subprocess_priority_kwargs())
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                c_time = None
                if "format" in data and "tags" in data["format"]:
                    c_time = data["format"]["tags"].get("creation_time")
                if not c_time and "streams" in data:
                    for s in data["streams"]:
                        if "tags" in s and "creation_time" in s["tags"]:
                            c_time = s["tags"]["creation_time"]
                            break
                if c_time:
                    clean_iso = c_time.replace("Z", "").split(".")[0]
                    dt = datetime.fromisoformat(clean_iso)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
        except Exception:
            pass

    # 3. Filename timestamp patterns (e.g. 20230814_123456 or 2023-08-14)
    try:
        fname = os.path.basename(filepath)
        match = re.search(r'(20\d\d)[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])', fname)
        if match:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            return datetime(year, month, day, tzinfo=timezone.utc)
    except Exception:
        pass

    # 4. Filesystem timestamp
    try:
        st = os.stat(filepath)
        file_time = st.st_mtime
        if hasattr(st, "st_birthtime") and st.st_birthtime > 0:
            file_time = min(st.st_mtime, st.st_birthtime)
        return datetime.fromtimestamp(file_time, tz=timezone.utc)
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Synchronous Helpers (Preserved for compatibility)
# -----------------------------------------------------------------------------

def download_image(image_path: Optional[str], prefix: str, tmdb_id: int, img_type: str = "poster") -> Optional[str]:
    """Synchronous image downloader (legacy/fallback)."""
    if not image_path:
        return None
    os.makedirs(METADATA_DIR, exist_ok=True)
    filename = f"{prefix}_{tmdb_id}_{img_type}.jpg"
    filepath = os.path.join(METADATA_DIR, filename)
    
    if not os.path.exists(filepath):
        resolution = "w1280" if img_type == "backdrop" else "w500"
        url = f"https://image.tmdb.org/t/p/{resolution}{image_path}"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                with open(filepath, 'wb') as f:
                    f.write(response.content)
        except Exception:
            return None
    return filename

def fetch_additional_details(tmdb_id: int, media_type: str, api_key: str):
    """Synchronous TMDB additional details fetcher (legacy/fallback)."""
    try:
        if media_type == "movie":
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={api_key}&append_to_response=credits,release_dates"
        else:
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&append_to_response=credits,content_ratings"
            
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            credits_data = data.get("credits", {})
            cast_list = []
            for c in credits_data.get("cast", [])[:5]:
                profile_filename = None
                if c.get("profile_path"):
                    profile_filename = download_image(c["profile_path"], "person", c["id"], "profile")
                
                cast_list.append({
                    "name": c["name"],
                    "character": c.get("character", ""),
                    "profile_filename": profile_filename
                })
            
            directors = [c["name"] for c in credits_data.get("crew", []) if c.get("job") == "Director"]
            cast_str = json.dumps(cast_list) if cast_list else None
            dir_str = ", ".join(directors) if directors else None
            
            genres = data.get("genres", [])
            genres_str = ", ".join([g.get("name") for g in genres if g.get("name")]) if genres else None
            
            content_rating = None
            if media_type == "movie":
                release_dates = data.get("release_dates", {}).get("results", [])
                for result in release_dates:
                    if result.get("iso_3166_1") == "US":
                        for date in result.get("release_dates", []):
                            if date.get("certification"):
                                content_rating = date.get("certification")
                                break
                        break
            else:
                content_ratings = data.get("content_ratings", {}).get("results", [])
                for result in content_ratings:
                    if result.get("iso_3166_1") == "US":
                        if result.get("rating"):
                            content_rating = result.get("rating")
                            break
                            
            return cast_str, dir_str, content_rating, genres_str
    except Exception:
        pass
    return None, None, None, None

def extract_chapter_markers(file_path: str):
    """Synchronous chapter marker extraction (used by on-demand playback probe)."""
    from .ffmpeg_setup import get_ffprobe_path
    try:
        cmd = [
            get_ffprobe_path(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_chapters",
            file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, **_get_subprocess_priority_kwargs())
        if res.returncode != 0:
            return None, None, None, None
        
        data = json.loads(res.stdout)
        chapters = data.get("chapters", [])
        intro_start, intro_end = None, None
        credits_start, credits_end = None, None

        for ch in chapters:
            tags = ch.get("tags", {})
            title = tags.get("title", tags.get("TITLE", "")).strip().lower()
            start = float(ch.get("start_time", 0))
            end = float(ch.get("end_time", 0))
            
            if re.search(r"\b(intro|opening|op|theme|title sequence)\b", title):
                intro_start = start
                intro_end = end
            elif re.search(r"\b(credits?|end credits?|closing credits?|ending|ed|outro)\b", title):
                credits_start = start
                credits_end = end

        return intro_start, intro_end, credits_start, credits_end
    except Exception:
        return None, None, None, None

# -----------------------------------------------------------------------------
# Async Helpers for Non-Blocking Scanner
# -----------------------------------------------------------------------------

async def async_download_image(client: Any, image_path: Optional[str], prefix: str, tmdb_id: int, img_type: str = "poster") -> Optional[str]:
    """Asynchronously download TMDB poster/backdrop/profile image without blocking."""
    if not image_path:
        return None
    os.makedirs(METADATA_DIR, exist_ok=True)
    filename = f"{prefix}_{tmdb_id}_{img_type}.jpg"
    filepath = os.path.join(METADATA_DIR, filename)
    
    if not os.path.exists(filepath):
        resolution = "w1280" if img_type == "backdrop" else "w500"
        url = f"https://image.tmdb.org/t/p/{resolution}{image_path}"
        try:
            res = await client.get(url, timeout=10.0)
            if res.status_code == 200:
                def _write_file():
                    with open(filepath, 'wb') as f:
                        f.write(res.content)
                await asyncio.to_thread(_write_file)
        except Exception:
            return None
    return filename

def find_local_trailer(file_path: str) -> Optional[str]:
    """
    Locates a local trailer file for a movie based on standard Plex/Jellyfin/Emby conventions:
    1. Exact movie stem + -trailer / .trailer in the same directory:
       e.g. 'Movie (2020)-trailer.mp4', 'Movie.trailer.mkv'
    2. 'trailer.mp4' / 'trailer.mkv' in the same directory
    3. Any file ending in '-trailer.*' in the same directory matching movie stem
    4. Subdirectory 'Trailers/' or 'trailers/' or 'trailer/' containing video files
    """
    if not file_path:
        return None
    try:
        norm_path = os.path.normpath(file_path)
        if not os.path.exists(norm_path):
            return None
        parent_dir = os.path.dirname(norm_path)
        if not os.path.isdir(parent_dir):
            return None

        base_name, _ = os.path.splitext(os.path.basename(norm_path))
        video_exts = {".mp4", ".mkv", ".webm", ".m4v", ".mov", ".avi", ".ts"}

        # 1. Exact priority checks in the same directory:
        exact_stems = [
            f"{base_name}-trailer",
            f"{base_name}.trailer",
            f"{base_name}_trailer",
            f"{base_name} - trailer",
            "trailer",
        ]
        for stem in exact_stems:
            for ext in video_exts:
                candidate = os.path.join(parent_dir, f"{stem}{ext}")
                if os.path.isfile(candidate) and os.path.normcase(candidate) != os.path.normcase(norm_path):
                    return candidate

        # 2. Case-insensitive scan in movie's directory
        lower_base = base_name.lower()
        with os.scandir(parent_dir) as it:
            for entry in it:
                if entry.is_file():
                    if os.path.normcase(entry.path) == os.path.normcase(norm_path):
                        continue
                    e_name, e_ext = os.path.splitext(entry.name)
                    if e_ext.lower() in video_exts:
                        e_low = e_name.lower()
                        if e_low in ("trailer", f"{lower_base}-trailer", f"{lower_base}.trailer", f"{lower_base}_trailer", f"{lower_base} - trailer"):
                            return entry.path
                        if ("-trailer" in e_low or ".trailer" in e_low or "_trailer" in e_low) and lower_base in e_low:
                            return entry.path
                elif entry.is_dir() and entry.name.lower() in ("trailers", "trailer"):
                    with os.scandir(entry.path) as sub_it:
                        for sub_entry in sub_it:
                            if sub_entry.is_file():
                                _, s_ext = os.path.splitext(sub_entry.name)
                                if s_ext.lower() in video_exts:
                                    return sub_entry.path
    except Exception as e:
        print(f"[Scanner] Error checking local trailer for {file_path}: {e}")
    return None

def extract_tmdb_trailer_key(videos: list) -> Optional[str]:
    """Selects the best YouTube trailer video key from TMDB video results."""
    if not videos or not isinstance(videos, list):
        return None
    yt_trailers = [
        v for v in videos
        if isinstance(v, dict) and v.get("site") == "YouTube" and v.get("key") and v.get("type") == "Trailer"
    ]
    if not yt_trailers:
        yt_trailers = [
            v for v in videos
            if isinstance(v, dict) and v.get("site") == "YouTube" and v.get("key") and v.get("type") in ("Teaser", "Clip")
        ]
    if not yt_trailers:
        return None

    # Priority 1: Official trailer in English
    official_en = [t for t in yt_trailers if t.get("official") and t.get("iso_639_1") == "en"]
    if official_en:
        return official_en[0].get("key")

    # Priority 2: Any English trailer
    en = [t for t in yt_trailers if t.get("iso_639_1") == "en"]
    if en:
        return en[0].get("key")

    # Priority 3: Any official trailer
    official = [t for t in yt_trailers if t.get("official")]
    if official:
        return official[0].get("key")

    return yt_trailers[0].get("key")

async def async_fetch_additional_details(client: Any, tmdb_id: int, media_type: str, api_key: str):
    """Asynchronously fetches credits, ratings, genres, and trailers from TMDB."""
    try:
        if media_type == "movie":
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={api_key}&append_to_response=credits,release_dates,videos"
        else:
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&append_to_response=credits,content_ratings,videos"
            
        res = await client.get(url, timeout=10.0)
        if res.status_code == 200:
            data = res.json()
            credits_data = data.get("credits", {})
            cast_list = []
            
            cast_items = credits_data.get("cast", [])[:5]
            for c in cast_items:
                profile_filename = None
                if c.get("profile_path"):
                    profile_filename = await async_download_image(client, c["profile_path"], "person", c["id"], "profile")
                
                cast_list.append({
                    "name": c["name"],
                    "character": c.get("character", ""),
                    "profile_filename": profile_filename
                })
            
            directors = [c["name"] for c in credits_data.get("crew", []) if c.get("job") == "Director"]
            cast_str = json.dumps(cast_list) if cast_list else None
            dir_str = ", ".join(directors) if directors else None
            
            genres = data.get("genres", [])
            genres_str = ", ".join([g.get("name") for g in genres if g.get("name")]) if genres else None
            
            content_rating = None
            if media_type == "movie":
                release_dates = data.get("release_dates", {}).get("results", [])
                for result in release_dates:
                    if result.get("iso_3166_1") == "US":
                        for date in result.get("release_dates", []):
                            if date.get("certification"):
                                content_rating = date.get("certification")
                                break
                        break
            else:
                content_ratings = data.get("content_ratings", {}).get("results", [])
                for result in content_ratings:
                    if result.get("iso_3166_1") == "US":
                        if result.get("rating"):
                            content_rating = result.get("rating")
                            break

            videos = data.get("videos", {}).get("results", [])
            trailer_key = extract_tmdb_trailer_key(videos)
                            
            return cast_str, dir_str, content_rating, genres_str, trailer_key
    except Exception:
        pass
    return None, None, None, None, None

async def async_extract_chapter_markers(file_path: str):
    """Non-blocking chapter marker extraction using asyncio.create_subprocess_exec with polite process priority."""
    from .ffmpeg_setup import get_ffprobe_path
    try:
        preexec = (lambda: os.nice(10)) if hasattr(os, "nice") and sys.platform != "win32" else None
        proc = await asyncio.create_subprocess_exec(
            get_ffprobe_path(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_chapters",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=preexec
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return None, None, None, None

        if proc.returncode != 0:
            return None, None, None, None

        data = json.loads(stdout.decode('utf-8', errors='ignore'))
        chapters = data.get("chapters", [])
        intro_start, intro_end = None, None
        credits_start, credits_end = None, None

        for ch in chapters:
            tags = ch.get("tags", {})
            title = tags.get("title", tags.get("TITLE", "")).strip().lower()
            start = float(ch.get("start_time", 0))
            end = float(ch.get("end_time", 0))
            
            if re.search(r"\b(intro|opening|op|theme|title sequence)\b", title):
                intro_start = start
                intro_end = end
            elif re.search(r"\b(credits?|end credits?|closing credits?|ending|ed|outro)\b", title):
                credits_start = start
                credits_end = end

        return intro_start, intro_end, credits_start, credits_end
    except Exception:
        return None, None, None, None

def _generate_photo_thumbnail(full_path: str, thumb_path: str, is_video: bool) -> bool:
    """Thread-offloaded thumbnail generator for photos and home videos with low-overhead draft scaling."""
    try:
        os.makedirs(METADATA_DIR, exist_ok=True)
        if is_video:
            from .ffmpeg_setup import get_ffmpeg_path
            ffmpeg_cmd = [
                get_ffmpeg_path(), "-y",
                "-i", full_path,
                "-ss", "00:00:00.000",
                "-vframes", "1",
                thumb_path
            ]
            subprocess.run(ffmpeg_cmd, capture_output=True, check=True, timeout=10, **_get_subprocess_priority_kwargs())
        else:
            from PIL import Image
            with Image.open(full_path) as img:
                # Fast DCT downsampling for JPEG/WEBP images before full bitmap decode
                if hasattr(img, "draft"):
                    try:
                        img.draft("RGB", (400, 400))
                    except Exception:
                        pass
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.thumbnail((400, 400))
                img.save(thumb_path, "JPEG", quality=80)
        return True
    except Exception as e:
        print(f"[Scanner] Error generating thumbnail for {full_path}: {e}")
        return False

# -----------------------------------------------------------------------------
# Audio & Music Extraction Helpers (File-First with TheAudioDB Enrichment)
# -----------------------------------------------------------------------------

def extract_audio_metadata(full_path: str) -> Dict[str, Any]:
    """
    Extracts tags (title, artist, album, track number, disc number, year, genre, duration, bitrate, codec)
    and embedded cover artwork directly from an audio file.
    Falls back gracefully to ffprobe and intelligent folder/filename heuristics when mutagen or tags are missing.
    """
    mutagen = None
    APIC = None
    try:
        import mutagen
        from mutagen.id3 import APIC
    except ImportError:
        pass

    raw_filename = os.path.basename(full_path)
    base_name, ext = os.path.splitext(raw_filename)
    ext_lower = ext.lower()

    # Default fallback values derived from filename and folder structure
    parts = os.path.normpath(full_path).replace("\\", "/").split("/")
    
    # Check if immediate parent directory is a disc subfolder (e.g. "CD 01", "CD 2", "Enhanced CD 01", "Disc 1")
    folder_disc_number = 1
    folder_album = "Unknown Album"
    folder_artist = "Unknown Artist"

    if len(parts) >= 2:
        parent_dir = parts[-2]
        disc_match = re.search(r'^(?:enhanced\s+|bonus\s+)?(?:cd|disc|disk)\s*0*([1-9]\d?)(?:[^\w\d].*)?$', parent_dir.strip(), re.IGNORECASE)
        if disc_match:
            folder_disc_number = int(disc_match.group(1))
            folder_album = parts[-3] if len(parts) >= 3 else "Unknown Album"
            folder_artist = parts[-4] if len(parts) >= 4 else "Unknown Artist"
        elif re.search(r'^bonus\s+(?:cd|disc|disk)$', parent_dir.strip(), re.IGNORECASE):
            folder_disc_number = 2
            folder_album = parts[-3] if len(parts) >= 3 else "Unknown Album"
            folder_artist = parts[-4] if len(parts) >= 4 else "Unknown Artist"
        else:
            folder_album = parts[-2]
            folder_artist = parts[-3] if len(parts) >= 3 else "Unknown Artist"

    # Strip year suffix from folder_album if present, e.g. "Issues (1999)" -> "Issues"
    folder_year = None
    y_match = re.search(r'\s*\((19\d\d|20\d\d)\)$', folder_album)
    if y_match:
        folder_year = y_match.group(1)
        folder_album = folder_album[:y_match.start()].strip()

    # Clean multi-disc indicators from folder_album name if present
    clean_f_alb, f_disc = normalize_album_title(folder_album)
    folder_album = clean_f_alb
    if f_disc and folder_disc_number == 1:
        folder_disc_number = f_disc

    clean_title = re.sub(r'^\d+[\s\.\-_]+', '', base_name).strip() or base_name
    track_num_match = re.match(r'^(\d+)', base_name)
    fallback_track = int(track_num_match.group(1)) if track_num_match else 1

    meta = {
        "title": clean_title,
        "artist": folder_artist if folder_artist != "media" and not folder_artist.lower().startswith("music") else "Unknown Artist",
        "album_artist": None,
        "album": folder_album if folder_album != "media" and not folder_album.lower().startswith("music") else "Unknown Album",
        "track_number": fallback_track,
        "disc_number": folder_disc_number,
        "year": folder_year,
        "genre": None,
        "duration": 0.0,
        "bitrate": None,
        "codec": ext_lower.lstrip("."),
        "cover_filename": None
    }

    embedded_image_bytes = None
    embedded_image_ext = ".jpg"

    if mutagen is not None:
        try:
            audio = mutagen.File(full_path)
            if audio is not None:
                # Duration & Bitrate
                if hasattr(audio, 'info') and audio.info:
                    if hasattr(audio.info, 'length') and audio.info.length:
                        meta["duration"] = round(float(audio.info.length), 2)
                    if hasattr(audio.info, 'bitrate') and audio.info.bitrate:
                        meta["bitrate"] = int(audio.info.bitrate)

                # 1. MP3 / ID3 tags
                if hasattr(audio, 'tags') and audio.tags:
                    tags = audio.tags

                    # ID3 style tags (TIT2, TPE1, TALB, etc.)
                    if hasattr(tags, 'getall') or 'TIT2' in tags:
                        if 'TIT2' in tags:
                            meta["title"] = str(tags['TIT2']).strip() or meta["title"]
                        if 'TPE1' in tags:
                            meta["artist"] = str(tags['TPE1']).strip() or meta["artist"]
                        if 'TPE2' in tags:
                            meta["album_artist"] = str(tags['TPE2']).strip()
                        if 'TALB' in tags:
                            meta["album"] = str(tags['TALB']).strip() or meta["album"]
                        if 'TCON' in tags:
                            meta["genre"] = str(tags['TCON']).strip()
                        if 'TDRC' in tags:
                            meta["year"] = str(tags['TDRC'])[:4]
                        elif 'TYER' in tags:
                            meta["year"] = str(tags['TYER'])[:4]
                        if 'TRCK' in tags:
                            tr_val = str(tags['TRCK']).split('/')[0].strip()
                            if tr_val.isdigit(): meta["track_number"] = int(tr_val)
                        if 'TPOS' in tags:
                            disc_val = str(tags['TPOS']).split('/')[0].strip()
                            if disc_val.isdigit(): meta["disc_number"] = int(disc_val)

                        # Extract APIC picture
                        for tag in tags.values():
                            if (APIC and isinstance(tag, APIC)) or (hasattr(tag, 'data') and hasattr(tag, 'mime') and 'image' in getattr(tag, 'mime', '')):
                                embedded_image_bytes = tag.data
                                if 'png' in getattr(tag, 'mime', '').lower():
                                    embedded_image_ext = ".png"
                                break

                    # Vorbis / FLAC / OGG tags
                    elif hasattr(audio, 'pictures') or hasattr(tags, 'items'):
                        def get_vorbis(keys):
                            for k in keys:
                                for tag_k, tag_v in tags.items():
                                    if tag_k.lower() == k.lower():
                                        val = tag_v[0] if isinstance(tag_v, (list, tuple)) else tag_v
                                        if val: return str(val).strip()
                            return None

                        t_val = get_vorbis(['title'])
                        if t_val: meta["title"] = t_val
                        a_val = get_vorbis(['artist'])
                        if a_val: meta["artist"] = a_val
                        aa_val = get_vorbis(['albumartist', 'album_artist'])
                        if aa_val: meta["album_artist"] = aa_val
                        alb_val = get_vorbis(['album'])
                        if alb_val: meta["album"] = alb_val
                        gen_val = get_vorbis(['genre'])
                        if gen_val: meta["genre"] = gen_val
                        date_val = get_vorbis(['date', 'year'])
                        if date_val: meta["year"] = str(date_val)[:4]
                        track_val = get_vorbis(['tracknumber', 'track'])
                        if track_val:
                            t_clean = str(track_val).split('/')[0].strip()
                            if t_clean.isdigit(): meta["track_number"] = int(t_clean)
                        disc_val = get_vorbis(['discnumber', 'disc'])
                        if disc_val:
                            d_clean = str(disc_val).split('/')[0].strip()
                            if d_clean.isdigit(): meta["disc_number"] = int(d_clean)

                        # FLAC pictures
                        if hasattr(audio, 'pictures') and audio.pictures:
                            pic = audio.pictures[0]
                            embedded_image_bytes = pic.data
                            if 'png' in getattr(pic, 'mime', '').lower():
                                embedded_image_ext = ".png"

                    # MP4 / M4A / ALAC tags
                    if '\xa9nam' in tags or 'covr' in tags or 'aART' in tags:
                        if '\xa9nam' in tags and tags['\xa9nam']:
                            meta["title"] = str(tags['\xa9nam'][0]).strip()
                        if '\xa9ART' in tags and tags['\xa9ART']:
                            meta["artist"] = str(tags['\xa9ART'][0]).strip()
                        if 'aART' in tags and tags['aART']:
                            meta["album_artist"] = str(tags['aART'][0]).strip()
                        if '\xa9alb' in tags and tags['\xa9alb']:
                            meta["album"] = str(tags['\xa9alb'][0]).strip()
                        if '\xa9gen' in tags and tags['\xa9gen']:
                            meta["genre"] = str(tags['\xa9gen'][0]).strip()
                        if '\xa9day' in tags and tags['\xa9day']:
                            meta["year"] = str(tags['\xa9day'][0])[:4]
                        if 'trkn' in tags and tags['trkn'] and tags['trkn'][0]:
                            try:
                                meta["track_number"] = int(tags['trkn'][0][0])
                            except (ValueError, TypeError):
                                pass
                        if 'disk' in tags and tags['disk'] and tags['disk'][0]:
                            try:
                                meta["disc_number"] = int(tags['disk'][0][0])
                            except (ValueError, TypeError):
                                pass
                        if 'covr' in tags and tags['covr']:
                            c_data = tags['covr'][0]
                            embedded_image_bytes = bytes(c_data)

        except Exception as e:
            print(f"[Scanner] Warning reading tags for {full_path} via mutagen: {e}")

    # Fallback to ffprobe if mutagen was missing or duration/tags were incomplete
    if mutagen is None or meta["duration"] <= 0.0 or meta["artist"] == "Unknown Artist":
        try:
            from .ffmpeg_setup import get_ffprobe_path
            probe_cmd = [
                get_ffprobe_path(), "-v", "quiet", "-print_format", "json",
                "-show_format", full_path
            ]
            probe_out = subprocess.check_output(probe_cmd, stderr=subprocess.DEVNULL, timeout=5).decode('utf-8', errors='ignore')
            probe_json = json.loads(probe_out)
            probe_fmt = probe_json.get("format", {})
            probe_tags = probe_fmt.get("tags", {})
            for k, v in probe_tags.items():
                k_l = k.lower()
                if k_l == "title" and v and (meta["title"] == clean_title or not meta["title"]):
                    meta["title"] = str(v).strip()
                elif k_l in ("artist", "performer") and v and meta["artist"] == "Unknown Artist":
                    meta["artist"] = str(v).strip()
                elif k_l in ("album_artist", "albumartist") and v and not meta["album_artist"]:
                    meta["album_artist"] = str(v).strip()
                elif k_l == "album" and v and meta["album"] == "Unknown Album":
                    meta["album"] = str(v).strip()
                elif k_l in ("date", "year") and v and not meta["year"]:
                    meta["year"] = str(v)[:4]
                elif k_l == "genre" and v and not meta["genre"]:
                    meta["genre"] = str(v).strip()
                elif k_l in ("track", "tracknumber") and v:
                    tr = str(v).split('/')[0].strip()
                    if tr.isdigit(): meta["track_number"] = int(tr)
            if meta["duration"] <= 0.0 and "duration" in probe_fmt:
                meta["duration"] = round(float(probe_fmt["duration"]), 2)
            if not meta["bitrate"] and "bit_rate" in probe_fmt:
                try: meta["bitrate"] = int(probe_fmt["bit_rate"])
                except Exception: pass
        except Exception:
            pass

    # Save extracted embedded artwork or look for local folder artwork
    os.makedirs(METADATA_DIR, exist_ok=True)
    if embedded_image_bytes:
        h = hashlib.md5(embedded_image_bytes).hexdigest()[:16]
        cover_fn = f"cover_{h}{embedded_image_ext}"
        cover_dst = os.path.join(METADATA_DIR, cover_fn)
        if not os.path.exists(cover_dst):
            try:
                with open(cover_dst, "wb") as f:
                    f.write(embedded_image_bytes)
            except Exception as e:
                print(f"[Scanner] Error saving embedded cover: {e}")
        meta["cover_filename"] = cover_fn
    else:
        # Check local folder for cover art
        parent_dir = os.path.dirname(full_path)
        common_art_names = (
            "cover.jpg", "cover.jpeg", "cover.png",
            "folder.jpg", "folder.jpeg", "folder.png",
            "album.jpg", "album.jpeg", "album.png",
            "front.jpg", "front.jpeg", "front.png"
        )
        for art_name in common_art_names:
            candidate = os.path.join(parent_dir, art_name)
            if os.path.isfile(candidate):
                try:
                    with open(candidate, "rb") as f:
                        c_bytes = f.read()
                    if c_bytes:
                        c_ext = os.path.splitext(art_name)[1].lower() or ".jpg"
                        h = hashlib.md5(c_bytes).hexdigest()[:16]
                        cover_fn = f"cover_{h}{c_ext}"
                        cover_dst = os.path.join(METADATA_DIR, cover_fn)
                        if not os.path.exists(cover_dst):
                            with open(cover_dst, "wb") as df:
                                df.write(c_bytes)
                        meta["cover_filename"] = cover_fn
                        break
                except Exception:
                    pass

    # Normalize artist names & ensure featured artists are captured in track title without creating split artists
    raw_artist = meta.get("artist") or "Unknown Artist"
    raw_title = meta.get("title") or clean_title
    primary_artist, enriched_title = extract_feature_info(raw_artist, raw_title)
    meta["artist"] = primary_artist
    meta["title"] = enriched_title
    if meta.get("album_artist"):
        meta["album_artist"] = normalize_artist_name(meta["album_artist"])
    if meta.get("album"):
        clean_alb, disc_from_title = normalize_album_title(meta["album"])
        meta["album"] = clean_alb
        if disc_from_title and (not meta.get("disc_number") or meta.get("disc_number") == 1):
            meta["disc_number"] = disc_from_title
    if folder_disc_number > 1 and (not meta.get("disc_number") or meta.get("disc_number") == 1):
        meta["disc_number"] = folder_disc_number

    return meta

async def async_download_custom_image(client: SafeHTTPClient, url: str, filename: str) -> Optional[str]:
    """Helper to download image from direct URL to METADATA_DIR."""
    if not url: return None
    os.makedirs(METADATA_DIR, exist_ok=True)
    filepath = os.path.join(METADATA_DIR, filename)
    if not os.path.exists(filepath):
        try:
            res = await client.get(url, timeout=10.0)
            if res.status_code == 200:
                def _write():
                    with open(filepath, 'wb') as f:
                        f.write(res.content)
                await asyncio.to_thread(_write)
        except Exception:
            return None
    return filename

async def async_fetch_theaudiodb_artist(client: SafeHTTPClient, artist_name: str, api_key: str) -> Optional[Dict[str, Any]]:
    """Fetches artist bio and artwork from TheAudioDB v1 JSON API."""
    if not artist_name or artist_name in ("Unknown Artist", "Various Artists") or not api_key:
        return None
    url = f"https://www.theaudiodb.com/api/v1/json/{api_key}/search.php"
    try:
        res = await client.get(url, params={"s": artist_name}, timeout=10.0)
        if res.status_code == 200:
            data = res.json()
            artists = data.get("artists")
            if artists and len(artists) > 0:
                art = artists[0]
                t_id = art.get("idArtist")
                bio = art.get("strBiographyEN") or ""
                thumb_url = art.get("strArtistThumb")
                banner_url = art.get("strArtistBanner") or art.get("strArtistFanart")
                
                thumb_fn = None
                banner_fn = None
                if thumb_url and t_id:
                    thumb_fn = await async_download_custom_image(client, thumb_url, f"artist_{t_id}_thumb.jpg")
                if banner_url and t_id:
                    banner_fn = await async_download_custom_image(client, banner_url, f"artist_{t_id}_banner.jpg")

                return {
                    "theaudiodb_id": int(t_id) if t_id and str(t_id).isdigit() else None,
                    "bio": bio,
                    "thumbnail_filename": thumb_fn,
                    "banner_filename": banner_fn
                }
    except Exception as e:
        print(f"[Scanner] TheAudioDB artist lookup failed for {artist_name}: {e}")
    return None

async def async_fetch_theaudiodb_album(client: SafeHTTPClient, artist_name: str, album_title: str, api_key: str) -> Optional[Dict[str, Any]]:
    """Fetches album details & artwork fallback from TheAudioDB."""
    if not artist_name or not album_title or not api_key:
        return None
    url = f"https://www.theaudiodb.com/api/v1/json/{api_key}/searchalbum.php"
    try:
        res = await client.get(url, params={"s": artist_name, "a": album_title}, timeout=10.0)
        if res.status_code == 200:
            data = res.json()
            albums = data.get("album")
            if albums and len(albums) > 0:
                alb = albums[0]
                alb_id = alb.get("idAlbum")
                thumb_url = alb.get("strAlbumThumb")
                genre = alb.get("strGenre")
                year = alb.get("intYearReleased")
                
                cover_fn = None
                if thumb_url and alb_id:
                    cover_fn = await async_download_custom_image(client, thumb_url, f"album_{alb_id}_cover.jpg")
                return {
                    "theaudiodb_id": int(alb_id) if alb_id and str(alb_id).isdigit() else None,
                    "cover_filename": cover_fn,
                    "genre": genre,
                    "year": str(year) if year else None
                }
    except Exception as e:
        print(f"[Scanner] TheAudioDB album lookup failed for {album_title}: {e}")
    return None

# -----------------------------------------------------------------------------
# Database Reconciliation & Music Consolidation Helpers
# -----------------------------------------------------------------------------

def consolidate_music_artists_and_albums(session: Session):
    """
    Consolidates split featured artists and fragmented albums into their canonical primary artist.
    Heals existing databases that have multiple artist records for featured artists (e.g. 'Korn feat. Skrillex', 'KoЯn').
    """
    try:
        # 0. Automatically purge any uncredited silence / dummy filler tracks (e.g. 5-second [silence] tracks)
        existing_tracks = session.exec(select(Track)).all()
        for trk in existing_tracks:
            if is_silence_or_filler_track(trk.title, trk.file_path, trk.duration or 0.0):
                for pt in session.exec(select(PlaylistTrack).where(PlaylistTrack.track_id == trk.id)).all():
                    session.delete(pt)
                session.delete(trk)
        session.commit()

        # 1. Consolidate Artists
        artist_tuples = [(a.id, a.name) for a in session.exec(select(Artist)).all()]
        for art_id, art_name in artist_tuples:
            target_name = normalize_artist_name(art_name)
            if target_name != art_name:
                art = session.get(Artist, art_id)
                if not art:
                    continue
                canonical = session.exec(
                    select(Artist).where(func.lower(Artist.name) == target_name.lower())
                ).first()
                if canonical and canonical.id != art.id:
                    for tr in session.exec(select(Track).where(Track.artist_id == art.id)).all():
                        tr.artist_id = canonical.id
                        tr.artist_name = canonical.name
                        session.add(tr)
                    for alb in session.exec(select(Album).where(Album.artist_id == art.id)).all():
                        alb.artist_id = canonical.id
                        alb.artist_name = canonical.name
                        session.add(alb)
                    session.delete(art)
                    session.commit()
                else:
                    art.name = target_name
                    session.add(art)
                    for tr in session.exec(select(Track).where(Track.artist_id == art.id)).all():
                        tr.artist_name = target_name
                        session.add(tr)
                    for alb in session.exec(select(Album).where(Album.artist_id == art.id)).all():
                        alb.artist_name = target_name
                        session.add(alb)
                    session.commit()

        # 2. Normalize multi-disc album titles & consolidate tracks
        albums = session.exec(select(Album)).all()
        for alb in albums:
            clean_title, disc_num = normalize_album_title(alb.title)
            if clean_title != alb.title:
                alb.title = clean_title
                session.add(alb)
                tracks = session.exec(select(Track).where(Track.album_id == alb.id)).all()
                for tr in tracks:
                    tr.album_title = clean_title
                    if disc_num and (tr.disc_number is None or tr.disc_number <= 1):
                        tr.disc_number = disc_num
                    session.add(tr)
        session.commit()

        # 3. Merge Fragmented Albums for the same artist & title
        albums = session.exec(select(Album)).all()
        groups: Dict[tuple, list] = {}
        for alb in albums:
            key = (alb.artist_id, alb.title.strip().lower())
            groups.setdefault(key, []).append(alb)

        for key, albs in groups.items():
            if len(albs) > 1:
                master = next((a for a in albs if a.cover_filename), albs[0])
                for a in albs:
                    if a.id != master.id:
                        for tr in session.exec(select(Track).where(Track.album_id == a.id)).all():
                            tr.album_id = master.id
                            tr.album_title = master.title
                            session.add(tr)
                        if not master.cover_filename and a.cover_filename:
                            master.cover_filename = a.cover_filename
                        if not master.year and a.year:
                            master.year = a.year
                        if not master.genre and a.genre:
                            master.genre = a.genre
                        session.delete(a)
                session.add(master)
                session.commit()

        # 3. Clean up empty artists & albums
        for alb in session.exec(select(Album)).all():
            if not session.exec(select(Track.id).where(Track.album_id == alb.id)).first():
                session.delete(alb)
        for a in session.exec(select(Artist)).all():
            if not session.exec(select(Track.id).where(Track.artist_id == a.id)).first():
                session.delete(a)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"[Scanner] Error during artist/album consolidation: {e}")

# -----------------------------------------------------------------------------
# Scanner Manager (Stateful, Asynchronous, Non-Blocking)
# -----------------------------------------------------------------------------

class ScannerManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.is_scanning: bool = False
        self.phase: str = "idle"  # "idle", "discovering", "cleanup", "movies", "tv", "photos", "complete"
        self.action: str = "Idle"
        self.current_item: str = ""
        self.items_scanned: int = 0
        self.total_items: int = 0
        self.start_time: float = 0.0
        self.last_completed_time: float = 0.0
        self.last_summary: str = ""
        self._thread: Optional[threading.Thread] = None
        self._abort_event = threading.Event()
        self.scanned_volumes: set[str] = set()
        self.scanned_types: set[str] = set()
        self.last_validity_check: float = 0.0

    def check_scan_necessary(self) -> bool:
        """
        Checks whether the currently running scan is still needed based on active volumes in the database.
        If the scan is no longer necessary (e.g. volumes were removed, changed, or none exist),
        it signals the scan to abort immediately and cleans up orphaned media.
        Returns True if the scan is still needed/valid, False if it was cancelled or not scanning.
        """
        with self._lock:
            if not self.is_scanning or self._abort_event.is_set():
                return False
            now = time.time()
            if now - self.last_validity_check < 3.0:
                return True
            self.last_validity_check = now
            if not self.scanned_volumes:
                return True

        try:
            with Session(engine) as session:
                active_vols = session.exec(select(VolumePath)).all()
                active_paths = {v.path.replace("\\", "/").rstrip("/").lower() for v in active_vols if v.path}
                active_types = {v.media_type for v in active_vols if v.media_type}

            with self._lock:
                if not self.is_scanning or self._abort_event.is_set():
                    return False

                # 1. No volume paths configured in database
                if not active_paths:
                    print("[Scanner] No active volume paths configured in database. Aborting scan.")
                    self._abort_event.set()
                    self.action = "Scan cancelled (no volumes configured)"
                    self.last_summary = "Scan was cancelled because all media libraries were removed."
                    return False

                # 2. Check if any volume currently being scanned was removed from DB
                norm_scanned = {p.replace("\\", "/").rstrip("/").lower() for p in self.scanned_volumes if p}
                removed_vols = norm_scanned - active_paths
                if removed_vols:
                    print(f"[Scanner] Scanned volume(s) removed from configuration ({removed_vols}). Aborting obsolete scan.")
                    self._abort_event.set()
                    self.action = "Scan cancelled (volume removed)"
                    self.last_summary = "Scan was cancelled because a library volume was removed."
                    return False

                # 3. Check if current phase media type has been removed
                if self.phase in ("movies", "tv", "photos", "music"):
                    type_map = {"movies": "movie", "tv": "tv", "photos": "photo", "music": "music"}
                    curr_type = type_map.get(self.phase)
                    if curr_type and curr_type not in active_types:
                        print(f"[Scanner] No active volumes for phase '{self.phase}'. Aborting obsolete scan.")
                        self._abort_event.set()
                        self.action = f"Scan cancelled ({curr_type} library removed)"
                        self.last_summary = f"Scan was cancelled because {curr_type} library was removed."
                        return False

            return True
        except Exception as e:
            print(f"[Scanner] Error during scan necessity check: {e}")
            return True

    def abort_scan(self, reason: str = "Scan cancelled"):
        """Requests immediate cancellation of any active background scan."""
        with self._lock:
            if not self.is_scanning:
                return
            self._abort_event.set()
            self.action = reason
            self.last_summary = reason
            print(f"[Scanner] Abort requested: {reason}")

    def _cleanup_orphans(self):
        """Purge media items from the database that no longer belong to any active volume."""
        with Session(engine) as session:
            volume_paths = session.exec(select(VolumePath)).all()
            movie_volume_paths = [v.path for v in volume_paths if v.media_type == "movie"]
            tv_volume_paths = [v.path for v in volume_paths if v.media_type == "tv"]
            photo_volume_paths = [v.path for v in volume_paths if v.media_type == "photo"]
            music_volume_paths = [v.path for v in volume_paths if v.media_type == "music"]

            raw_movies = session.exec(select(Movie.id, Movie.file_path)).all()
            raw_shows = session.exec(select(TVShow.id, TVShow.file_path)).all()
            raw_photos = session.exec(select(Photo.id, Photo.file_path)).all()
            raw_tracks = session.exec(select(Track.id, Track.file_path)).all()

        def belongs_to_any_volume(filepath, volumes):
            if not filepath:
                return False
            norm_filepath = filepath.replace("\\", "/").rstrip("/").lower()
            for v in volumes:
                norm_v = v.replace("\\", "/").rstrip("/").lower()
                if norm_filepath == norm_v or norm_filepath.startswith(norm_v + "/"):
                    return True
                try:
                    rel = os.path.relpath(filepath, v)
                    if not rel.startswith(".."):
                        return True
                except ValueError:
                    continue
            return False

        del_m_ids = [m_id for m_id, fpath in raw_movies if fpath and not fpath.startswith("remote_") and not belongs_to_any_volume(fpath, movie_volume_paths)]
        del_t_ids = [t_id for t_id, fpath in raw_shows if fpath and not fpath.startswith("remote_") and not belongs_to_any_volume(fpath, tv_volume_paths)]
        del_p_ids = [p_id for p_id, fpath in raw_photos if not fpath or not belongs_to_any_volume(fpath, photo_volume_paths)]
        del_tr_ids = [tr_id for tr_id, fpath in raw_tracks if not fpath or not belongs_to_any_volume(fpath, music_volume_paths)]

        total_to_delete = len(del_m_ids) + len(del_t_ids) + len(del_p_ids) + len(del_tr_ids)
        if total_to_delete > 0:
            all_deletions = (
                [("movie", m_id) for m_id in del_m_ids] +
                [("tv", t_id) for t_id in del_t_ids] +
                [("photo", p_id) for p_id in del_p_ids] +
                [("track", tr_id) for tr_id in del_tr_ids]
            )
            # Batch delete in chunks of 50 to avoid holding SQLite locks
            for i in range(0, len(all_deletions), 50):
                chunk = all_deletions[i:i + 50]
                with Session(engine) as session:
                    for m_type, item_id in chunk:
                        if m_type == "movie":
                            item = session.get(Movie, item_id)
                        elif m_type == "tv":
                            item = session.get(TVShow, item_id)
                        elif m_type == "photo":
                            item = session.get(Photo, item_id)
                        else:
                            item = session.get(Track, item_id)
                        if item:
                            session.delete(item)
                    try:
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"[Scanner] Error during orphan batch deletion: {e}")
                time.sleep(0.02)

            # Reconcile empty albums and artists and consolidate music
            with Session(engine) as session:
                consolidate_music_artists_and_albums(session)

            print(f"[Scanner] Cleaned up {total_to_delete} orphaned media item(s).")

    def get_status(self) -> Dict[str, Any]:
        """Returns JSON-serializable snapshot of current scanning progress (100% in-memory)."""
        with self._lock:
            now = time.time()
            short_status = "Idle"
            if self.is_scanning:
                if self.total_items > 0:
                    short_status = f"Scanning ({self.items_scanned}/{self.total_items})"
                elif self.phase == "discovering":
                    short_status = "Discovering..."
                elif self.phase == "cleanup":
                    short_status = "Cleaning..."
                else:
                    short_status = "Scanning..."
            elif "cancelled" in self.action.lower():
                short_status = "Cancelled"

            progress_pct = int((self.items_scanned / self.total_items) * 100) if (self.total_items > 0) else 0

            return {
                "is_scanning": self.is_scanning,
                "phase": self.phase,
                "action": self.action,
                "current_item": self.current_item,
                "items_scanned": self.items_scanned,
                "total_items": self.total_items,
                "progress_percent": progress_pct,
                "short_status": short_status,
                "recently_completed": False,
                "last_summary": self.last_summary,
                "elapsed_seconds": int(now - self.start_time) if self.is_scanning else 0
            }

    def start_scan(self) -> bool:
        """Triggers non-blocking background async library scan if not already running."""
        with self._lock:
            if self.is_scanning:
                # If a scan is already running and not aborting, ignore duplicate triggers
                if not self._abort_event.is_set():
                    return False
                print("[Scanner] Current scan is aborting, waiting briefly for worker to reset.")

        if self._thread and self._thread.is_alive() and self._abort_event.is_set():
            self._thread.join(timeout=2.0)

        with self._lock:
            if self.is_scanning:
                return False
            self.is_scanning = True
            self.phase = "discovering"
            self.action = "Discovering media files..."
            self.current_item = ""
            self.items_scanned = 0
            self.total_items = 0
            self.start_time = time.time()
            self._abort_event.clear()

        self._thread = threading.Thread(target=self._run_worker, daemon=True)
        self._thread.start()
        return True

    def _run_worker(self):
        """Worker thread entry point that executes the asyncio event loop."""
        was_aborted = False
        try:
            asyncio.run(self._async_scan())
        except Exception as e:
            print(f"[Scanner] Error during async scan: {e}")
        finally:
            with self._lock:
                self.is_scanning = False
                self.last_completed_time = time.time()
                was_aborted = self._abort_event.is_set()
                if was_aborted:
                    self.phase = "complete"
                    if "cancelled" not in self.action.lower():
                        self.action = "Scan cancelled"
                else:
                    self.phase = "complete"
                    self.action = "Scan complete"
                self._abort_event.clear()

            if was_aborted:
                try:
                    self._cleanup_orphans()
                except Exception as e:
                    print(f"[Scanner] Error cleaning up orphans: {e}")

    async def _async_scan(self):
        """Main async scanning pipeline."""
        # 1. Fetch volume paths and configurations
        with Session(engine) as session:
            volume_paths = session.exec(select(VolumePath)).all()
            movie_volume_paths = [v.path for v in volume_paths if v.media_type == "movie"]
            tv_volume_paths = [v.path for v in volume_paths if v.media_type == "tv"]
            photo_volume_paths = [v.path for v in volume_paths if v.media_type == "photo"]
            music_volume_paths = [v.path for v in volume_paths if v.media_type == "music"]

            config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
            tmdb_api_key = config.value if config else os.getenv("TMDB_API_KEY")

            adb_config = session.exec(select(SystemConfig).where(SystemConfig.key == "theaudiodb_api_key")).first()
            theaudiodb_api_key = (adb_config.value.strip() if adb_config and adb_config.value and adb_config.value.strip() else "") or os.getenv("THEAUDIODB_API_KEY", "123")

            existing_movie_paths = set(session.exec(select(Movie.file_path)).all())
            existing_tv_paths = set(session.exec(select(TVShow.file_path)).all())
            existing_photos = {p.file_path: p.poster_filename for p in session.exec(select(Photo)).all()}
            existing_tracks = set(session.exec(select(Track.file_path)).all())

        with self._lock:
            self.scanned_volumes = set(movie_volume_paths + tv_volume_paths + photo_volume_paths + music_volume_paths)
            self.scanned_types = {v.media_type for v in volume_paths if v.media_type}
            self._abort_event.clear()

        if not self.scanned_volumes:
            with self._lock:
                self.action = "No volumes configured"
                self.last_summary = "No library volumes are configured to scan."
            return

        def is_valid_path(filepath, volumes):
            if not filepath or not os.path.exists(filepath):
                return False
            if is_recycle_path(filepath):
                return False
            norm_filepath = filepath.replace("\\", "/").rstrip("/")
            for v in volumes:
                norm_v = v.replace("\\", "/").rstrip("/")
                if norm_filepath.lower() == norm_v.lower() or norm_filepath.lower().startswith(norm_v.lower() + "/"):
                    return True
                try:
                    rel = os.path.relpath(filepath, v)
                    if not rel.startswith(".."):
                        return True
                except ValueError:
                    continue
            return False

        # 2. Cleanup missing files from database and reconcile albums
        with self._lock:
            self.phase = "cleanup"
            self.action = "Cleaning up removed files..."

        with Session(engine) as session:
            movie_records = session.exec(select(Movie.id, Movie.file_path)).all()
            tv_records = session.exec(select(TVShow.id, TVShow.file_path)).all()
            photo_records = session.exec(select(Photo.id, Photo.file_path, Photo.album, Photo.date_taken)).all()
            track_records = session.exec(select(Track.id, Track.file_path)).all()

        del_movies = [m_id for m_id, fpath in movie_records if fpath and not fpath.startswith("remote_") and not is_valid_path(fpath, movie_volume_paths)]
        del_tv = [t_id for t_id, fpath in tv_records if fpath and not fpath.startswith("remote_") and not is_valid_path(fpath, tv_volume_paths)]
        del_tracks = [tr_id for tr_id, fpath in track_records if fpath and not is_valid_path(fpath, music_volume_paths)]
        def _reconcile_photos():
            del_p = []
            updates = []
            date_backfill_count = 0
            for p_id, p_fpath, p_alb, p_dt in photo_records:
                if not p_fpath or not is_valid_path(p_fpath, photo_volume_paths):
                    del_p.append(p_id)
                else:
                    target_album = None
                    file_dir = os.path.dirname(p_fpath)
                    for v in photo_volume_paths:
                        norm_v = v.replace("\\", "/").rstrip("/").lower()
                        norm_f = file_dir.replace("\\", "/").rstrip("/").lower()
                        if norm_f == norm_v or norm_f.startswith(norm_v + "/"):
                            new_alb = get_album_name(file_dir, v)
                            if p_alb != new_alb:
                                target_album = new_alb
                            break
                    
                    new_dt = None
                    if p_dt is None and date_backfill_count < 100 and os.path.exists(p_fpath):
                        extracted = extract_date_taken(p_fpath)
                        if extracted:
                            new_dt = extracted
                            date_backfill_count += 1
                    
                    if target_album is not None or new_dt is not None:
                        updates.append((p_id, target_album, new_dt))
            return del_p, updates

        del_photos, photo_updates = await asyncio.to_thread(_reconcile_photos)

        if del_movies or del_tv or del_photos or del_tracks:
            with Session(engine) as session:
                for m_id in del_movies:
                    m = session.get(Movie, m_id)
                    if m: session.delete(m)
                for t_id in del_tv:
                    t = session.get(TVShow, t_id)
                    if t: session.delete(t)
                for p_id in del_photos:
                    p = session.get(Photo, p_id)
                    if p: session.delete(p)
                for tr_id in del_tracks:
                    tr = session.get(Track, tr_id)
                    if tr: session.delete(tr)
                
                # Cleanup empty albums & artists
                all_albs = session.exec(select(Album)).all()
                for alb in all_albs:
                    if len(session.exec(select(Track.id).where(Track.album_id == alb.id)).all()) == 0:
                        session.delete(alb)

                all_arts = session.exec(select(Artist)).all()
                for art in all_arts:
                    if len(session.exec(select(Track.id).where(Track.artist_id == art.id)).all()) == 0:
                        session.delete(art)

                try:
                    session.commit()
                except Exception as e:
                    session.rollback()
                    print(f"[Scanner] Error committing cleanup deletions: {e}")
            await asyncio.sleep(0.05)

        if photo_updates:
            for i in range(0, len(photo_updates), 50):
                chunk = photo_updates[i:i + 50]
                with Session(engine) as session:
                    for p_id, target_alb, new_dt in chunk:
                        p = session.get(Photo, p_id)
                        if p:
                            if target_alb is not None:
                                p.album = target_alb
                            if new_dt is not None:
                                p.date_taken = new_dt
                            session.add(p)
                    try:
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"[Scanner] Error committing photo updates batch: {e}")
                await asyncio.sleep(0.05)

        # 3. Quick Discovery Phase (Thread-offloaded os.walk)
        with self._lock:
            self.phase = "discovering"
            self.action = "Discovering new media files..."

        def _discover_candidates():
            if self._abort_event.is_set():
                return [], [], [], []
            new_movies = []
            for movies_dir in movie_volume_paths:
                if self._abort_event.is_set():
                    return [], [], [], []
                if os.path.exists(movies_dir):
                    for root, dirs, files in os.walk(movies_dir):
                        dirs[:] = [d for d in dirs if not is_recycle_name(d)]
                        if is_recycle_path(root):
                            continue
                        for file in files:
                            if is_recycle_name(file):
                                continue
                            if file.lower().endswith(VIDEO_EXTENSIONS):
                                full_path = os.path.join(root, file)
                                if is_recycle_path(full_path):
                                    continue
                                if full_path not in existing_movie_paths:
                                    new_movies.append((full_path, file))

            new_shows = []
            for tv_dir in tv_volume_paths:
                if self._abort_event.is_set():
                    return [], [], [], []
                if os.path.exists(tv_dir):
                    for root, dirs, files in os.walk(tv_dir):
                        dirs[:] = [d for d in dirs if not is_recycle_name(d)]
                        if is_recycle_path(root):
                            continue
                        for file in files:
                            if is_recycle_name(file):
                                continue
                            if file.lower().endswith(VIDEO_EXTENSIONS):
                                full_path = os.path.join(root, file)
                                if is_recycle_path(full_path):
                                    continue
                                if full_path not in existing_tv_paths:
                                    new_shows.append((full_path, file, root, tv_dir))

            new_photos = []
            for photo_dir in photo_volume_paths:
                if self._abort_event.is_set():
                    return [], [], [], []
                if os.path.exists(photo_dir):
                    for root, dirs, files in os.walk(photo_dir):
                        dirs[:] = [d for d in dirs if not is_recycle_name(d)]
                        if is_recycle_path(root):
                            continue
                        for file in files:
                            if is_recycle_name(file):
                                continue
                            if file.lower().endswith(PHOTO_EXTENSIONS):
                                full_path = os.path.join(root, file)
                                if is_recycle_path(full_path):
                                    continue
                                if full_path not in existing_photos:
                                    new_photos.append((full_path, file, root, photo_dir))

            new_tracks = []
            for music_dir in music_volume_paths:
                if self._abort_event.is_set():
                    return [], [], [], []
                if not os.path.exists(music_dir):
                    print(f"[Scanner] Warning: Configured music volume path does not exist on disk: {music_dir}")
                    continue
                dir_tracks_count = 0
                for root, dirs, files in os.walk(music_dir):
                    dirs[:] = [d for d in dirs if not is_recycle_name(d)]
                    if is_recycle_path(root):
                        continue
                    for file in files:
                        if is_recycle_name(file):
                            continue
                        if file.lower().endswith(AUDIO_EXTENSIONS):
                            full_path = os.path.join(root, file)
                            if is_recycle_path(full_path):
                                continue
                            if is_silence_or_filler_track(title=None, file_path=full_path, duration=0.0):
                                continue
                            if full_path not in existing_tracks:
                                new_tracks.append((full_path, file, root, music_dir))
                                dir_tracks_count += 1
                print(f"[Scanner] Music directory {music_dir}: discovered {dir_tracks_count} new candidate track(s).")

            return new_movies, new_shows, new_photos, new_tracks

        new_movies, new_shows, new_photos, new_tracks = await asyncio.to_thread(_discover_candidates)
        if self._abort_event.is_set() or not self.check_scan_necessary():
            return
        total_items = len(new_movies) + len(new_shows) + len(new_photos) + len(new_tracks)

        with self._lock:
            self.total_items = total_items
            self.items_scanned = 0

        if total_items == 0:
            with self._lock:
                self.action = "Library up to date"
                self.last_summary = "All media libraries are up to date."
            return

        # 4. Async processing with HTTP connection pool and concurrency semaphore
        semaphore = asyncio.Semaphore(4)
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def get_http_client():
            if httpx is not None:
                async with httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)) as raw_client:
                    yield SafeHTTPClient(raw_client)
            else:
                yield SafeHTTPClient(None)

        async with get_http_client() as client:

            # 4A. Scan Movies
            batch_movies = []
            for full_path, file in new_movies:
                if self._abort_event.is_set() or not self.check_scan_necessary():
                    print("[Scanner] Aborting movie processing loop.")
                    break
                raw_title = os.path.splitext(file)[0]
                with self._lock:
                    self.phase = "movies"
                    self.action = f"Scanning Movies ({self.items_scanned + 1}/{self.total_items})"
                    self.current_item = raw_title

                match = re.match(r"(.*?)\s*\((\d{4})\)", raw_title)
                search_title = match.group(1) if match else raw_title
                year = match.group(2) if match else ""

                plot, poster_filename, backdrop_filename = None, None, None
                cast, director = None, None
                rating, content_rating, genres_str = None, None, None
                tmdb_id, trailer_key = None, None

                if tmdb_api_key:
                    params = {"query": search_title, "api_key": tmdb_api_key}
                    if year:
                        params["year"] = year
                    try:
                        async with semaphore:
                            res = await client.get("https://api.themoviedb.org/3/search/movie", params=params)
                        if res.status_code == 200 and res.json().get("results"):
                            results = res.json()["results"]
                            clean_search = re.sub(r'[^a-zA-Z0-9]', '', search_title).lower()
                            data = next((r for r in results if re.sub(r'[^a-zA-Z0-9]', '', r.get("title", "")).lower() == clean_search), results[0])
                            tmdb_id = data.get("id")
                            plot = data.get("overview")
                            poster_filename = await async_download_image(client, data.get("poster_path"), "movie", tmdb_id, "poster")
                            backdrop_filename = await async_download_image(client, data.get("backdrop_path"), "movie", tmdb_id, "backdrop")
                            rating = data.get("vote_average")
                            search_title = data.get("title", search_title)
                            year = data.get("release_date", "")[:4]
                            
                            cast, director, content_rating, genres_str, trailer_key = await async_fetch_additional_details(client, tmdb_id, "movie", tmdb_api_key)
                    except Exception as e:
                        print(f"[Scanner] Movie TMDB lookup failed for {search_title}: {e}")

                intro_s, intro_e, cred_s, cred_e = await async_extract_chapter_markers(full_path)

                batch_movies.append(Movie(
                    title=search_title, file_path=full_path, year=year,
                    plot=plot, poster_filename=poster_filename,
                    backdrop_filename=backdrop_filename, cast=cast, director=director, rating=rating,
                    content_rating=content_rating, genres=genres_str,
                    intro_start=intro_s, intro_end=intro_e, credits_start=cred_s, credits_end=cred_e,
                    trailer_key=trailer_key, tmdb_id=tmdb_id
                ))

                with self._lock:
                    self.items_scanned += 1

                # Polite yield between movies
                await asyncio.sleep(0.02)

                if len(batch_movies) >= 10:
                    with Session(engine) as session:
                        for m in batch_movies:
                            if not session.exec(select(Movie.id).where(Movie.file_path == m.file_path)).first():
                                session.add(m)
                        try:
                            session.commit()
                        except Exception as e:
                            session.rollback()
                            print(f"[Scanner] Error committing movie batch: {e}")
                    batch_movies.clear()
                    await asyncio.sleep(0.1)

            if batch_movies:
                with Session(engine) as session:
                    for m in batch_movies:
                        if not session.exec(select(Movie.id).where(Movie.file_path == m.file_path)).first():
                            session.add(m)
                    try:
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"[Scanner] Error committing final movie batch: {e}")
                batch_movies.clear()
                await asyncio.sleep(0.1)

            # 4B. Scan TV Shows
            batch_shows = []
            for full_path, file, root, tv_dir in new_shows:
                if self._abort_event.is_set() or not self.check_scan_necessary():
                    print("[Scanner] Aborting TV processing loop.")
                    break
                raw_title = os.path.splitext(file)[0]
                with self._lock:
                    self.phase = "tv"
                    self.action = f"Scanning TV ({self.items_scanned + 1}/{self.total_items})"
                    self.current_item = raw_title

                title_match = re.search(r'^(.*?)\s*[-_]?\s*(?:[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)|(\d+)x(\d+))', raw_title, re.IGNORECASE)
                season, episode = None, None
                
                if title_match:
                    search_title = title_match.group(1).strip()
                    if title_match.group(2) and title_match.group(3):
                        season = int(title_match.group(2))
                        episode = int(title_match.group(3))
                    elif title_match.group(4) and title_match.group(5):
                        season = int(title_match.group(4))
                        episode = int(title_match.group(5))
                else:
                    search_title = raw_title.split(" - ")[0]

                rel_root = os.path.relpath(root, tv_dir)
                if rel_root and rel_root != ".":
                    folder_parts = [p for p in rel_root.split(os.sep) if p and not re.match(r'^season\s*\d+', p, re.IGNORECASE)]
                    if folder_parts:
                        folder_title = folder_parts[0].strip()
                        if not search_title or len(search_title) < 2:
                            search_title = folder_title

                year_in_title = re.search(r'\s*\((\d{4})\)$', search_title)
                year = year_in_title.group(1) if year_in_title else ""
                search_title = re.sub(r'\s*\(\d{4}\)$', '', search_title).strip()

                plot, poster_filename, backdrop_filename = None, None, None
                cast, director = None, None
                rating, content_rating, genres_str = None, None, None
                season_poster_filename = None

                if tmdb_api_key:
                    params = {"query": search_title, "api_key": tmdb_api_key}
                    if year:
                        params["first_air_date_year"] = year
                    try:
                        async with semaphore:
                            res = await client.get("https://api.themoviedb.org/3/search/tv", params=params)
                        if res.status_code == 200 and res.json().get("results"):
                            results = res.json()["results"]
                            clean_search = re.sub(r'[^a-zA-Z0-9]', '', search_title).lower()
                            data = next((r for r in results if re.sub(r'[^a-zA-Z0-9]', '', r.get("name", "")).lower() == clean_search), results[0])
                            tmdb_id = data.get("id")
                            plot = data.get("overview")
                            poster_filename = await async_download_image(client, data.get("poster_path"), "tv", tmdb_id, "poster")
                            backdrop_filename = await async_download_image(client, data.get("backdrop_path"), "tv", tmdb_id, "backdrop")
                            rating = data.get("vote_average")
                            search_title = data.get("name", search_title)
                            
                            if not year:
                                year = data.get("first_air_date", "")[:4]
                                
                            cast, director, content_rating, genres_str, _ = await async_fetch_additional_details(client, tmdb_id, "tv", tmdb_api_key)
                            
                            if season is not None:
                                s_res = await client.get(
                                    f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}",
                                    params={"api_key": tmdb_api_key}
                                )
                                if s_res.status_code == 200:
                                    s_data = s_res.json()
                                    if s_data.get("poster_path"):
                                        season_poster_filename = await async_download_image(client, s_data["poster_path"], f"tv_s{season}", tmdb_id, "poster")
                    except Exception as e:
                        print(f"[Scanner] TV TMDB lookup failed for {search_title}: {e}")

                intro_s, intro_e, cred_s, cred_e = await async_extract_chapter_markers(full_path)

                batch_shows.append(TVShow(
                    title=search_title, file_path=full_path, year=year,
                    plot=plot, poster_filename=poster_filename,
                    backdrop_filename=backdrop_filename, cast=cast, director=director, rating=rating,
                    season=season, episode=episode, season_poster_filename=season_poster_filename,
                    content_rating=content_rating, genres=genres_str,
                    intro_start=intro_s, intro_end=intro_e, credits_start=cred_s, credits_end=cred_e
                ))

                with self._lock:
                    self.items_scanned += 1

                # Polite yield between TV episodes
                await asyncio.sleep(0.02)

                if len(batch_shows) >= 20:
                    with Session(engine) as session:
                        for s in batch_shows:
                            if not session.exec(select(TVShow.id).where(TVShow.file_path == s.file_path)).first():
                                session.add(s)
                        try:
                            session.commit()
                        except Exception as e:
                            session.rollback()
                            print(f"[Scanner] Error committing TV batch: {e}")
                    batch_shows.clear()
                    await asyncio.sleep(0.1)

            if batch_shows:
                with Session(engine) as session:
                    for s in batch_shows:
                        if not session.exec(select(TVShow.id).where(TVShow.file_path == s.file_path)).first():
                            session.add(s)
                    try:
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"[Scanner] Error committing final TV batch: {e}")
                batch_shows.clear()
                await asyncio.sleep(0.1)

            # 4C. Scan Photos & Home Videos
            batch_photos = []
            for full_path, file, root, photo_dir in new_photos:
                if self._abort_event.is_set() or not self.check_scan_necessary():
                    print("[Scanner] Aborting photo processing loop.")
                    break
                raw_title = os.path.splitext(file)[0]
                with self._lock:
                    self.phase = "photos"
                    self.action = f"Scanning Photos ({self.items_scanned + 1}/{self.total_items})"
                    self.current_item = raw_title

                if is_recycle_path(full_path):
                    continue

                album = get_album_name(root, photo_dir)
                date_taken = await asyncio.to_thread(extract_date_taken, full_path)

                path_hash = hashlib.md5(full_path.encode('utf-8')).hexdigest()[:16]
                thumb_filename = f"photo_{path_hash}_poster.jpg"
                thumb_path = os.path.join(METADATA_DIR, thumb_filename)
                ext = os.path.splitext(file)[1].lower()
                is_video = ext in (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm")

                if not os.path.exists(thumb_path):
                    success = await asyncio.to_thread(_generate_photo_thumbnail, full_path, thumb_path, is_video)
                    poster_fn = thumb_filename if success else None
                else:
                    poster_fn = thumb_filename

                batch_photos.append({
                    "title": raw_title,
                    "file_path": full_path,
                    "album": album,
                    "date_taken": date_taken,
                    "poster_filename": poster_fn
                })

                with self._lock:
                    self.items_scanned += 1

                # Polite yield between photos / home videos
                await asyncio.sleep(0.03)

                if len(batch_photos) >= 30:
                    with Session(engine) as session:
                        for p_data in batch_photos:
                            existing = session.exec(select(Photo).where(Photo.file_path == p_data["file_path"])).first()
                            if not existing:
                                session.add(Photo(
                                    title=p_data["title"],
                                    file_path=p_data["file_path"],
                                    album=p_data["album"],
                                    date_taken=p_data["date_taken"],
                                    poster_filename=p_data["poster_filename"]
                                ))
                            else:
                                dirty = False
                                if existing.album != p_data["album"]:
                                    existing.album = p_data["album"]
                                    dirty = True
                                if p_data["date_taken"] and existing.date_taken != p_data["date_taken"]:
                                    existing.date_taken = p_data["date_taken"]
                                    dirty = True
                                if p_data["poster_filename"] and existing.poster_filename != p_data["poster_filename"]:
                                    existing.poster_filename = p_data["poster_filename"]
                                    dirty = True
                                if dirty:
                                    session.add(existing)
                        try:
                            session.commit()
                        except Exception as e:
                            session.rollback()
                            print(f"[Scanner] Error committing photo batch: {e}")
                    batch_photos.clear()
                    await asyncio.sleep(0.1)

            if batch_photos:
                with Session(engine) as session:
                    for p_data in batch_photos:
                        existing = session.exec(select(Photo).where(Photo.file_path == p_data["file_path"])).first()
                        if not existing:
                            session.add(Photo(
                                title=p_data["title"],
                                file_path=p_data["file_path"],
                                album=p_data["album"],
                                date_taken=p_data["date_taken"],
                                poster_filename=p_data["poster_filename"]
                            ))
                        else:
                            dirty = False
                            if existing.album != p_data["album"]:
                                existing.album = p_data["album"]
                                dirty = True
                            if p_data["date_taken"] and existing.date_taken != p_data["date_taken"]:
                                existing.date_taken = p_data["date_taken"]
                                dirty = True
                            if p_data["poster_filename"] and existing.poster_filename != p_data["poster_filename"]:
                                existing.poster_filename = p_data["poster_filename"]
                                dirty = True
                            if dirty:
                                session.add(existing)
                    try:
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"[Scanner] Error committing final photo batch: {e}")
                batch_photos.clear()
                await asyncio.sleep(0.1)

            # 4D. Scan Music Library (File-First with TheAudioDB enrichment)
            batch_tracks = []
            artists_to_enrich = set()
            albums_to_enrich = set()

            for full_path, file, root, music_dir in new_tracks:
                if self._abort_event.is_set() or not self.check_scan_necessary():
                    print("[Scanner] Aborting music processing loop.")
                    break

                with self._lock:
                    self.phase = "music"
                    self.action = f"Scanning Music ({self.items_scanned + 1}/{self.total_items})"
                    self.current_item = file

                if is_recycle_path(full_path):
                    continue

                try:
                    track_meta = await asyncio.to_thread(extract_audio_metadata, full_path)
                    if is_silence_or_filler_track(track_meta.get("title"), full_path, track_meta.get("duration", 0.0)):
                        print(f"[Scanner] Automatically skipping silence/filler track: {full_path} ({track_meta.get('title')})")
                        continue
                    batch_tracks.append({
                        "full_path": full_path,
                        "meta": track_meta
                    })
                except Exception as e:
                    print(f"[Scanner] Error extracting audio metadata for {full_path}: {e}")
                    continue

                with self._lock:
                    self.items_scanned += 1

                # Polite yield between audio tracks
                await asyncio.sleep(0.02)

                if len(batch_tracks) >= 25:
                    await self._commit_music_batch(batch_tracks, artists_to_enrich, albums_to_enrich)
                    batch_tracks.clear()
                    await asyncio.sleep(0.1)

            if batch_tracks:
                await self._commit_music_batch(batch_tracks, artists_to_enrich, albums_to_enrich)
                batch_tracks.clear()
                await asyncio.sleep(0.1)

            # Optional Online Enrichment via TheAudioDB (Throttled for 30 req/min free-tier limit)
            if theaudiodb_api_key and theaudiodb_api_key != "disabled":
                for artist_id, artist_name in artists_to_enrich:
                    if self._abort_event.is_set(): break
                    try:
                        art_info = await async_fetch_theaudiodb_artist(client, artist_name, theaudiodb_api_key)
                        if art_info:
                            with Session(engine) as session:
                                art_rec = session.get(Artist, artist_id)
                                if art_rec:
                                    if art_info.get("bio") and not art_rec.bio:
                                        art_rec.bio = art_info["bio"]
                                    if art_info.get("thumbnail_filename") and not art_rec.thumbnail_filename:
                                        art_rec.thumbnail_filename = art_info["thumbnail_filename"]
                                    if art_info.get("banner_filename") and not art_rec.banner_filename:
                                        art_rec.banner_filename = art_info["banner_filename"]
                                    if art_info.get("theaudiodb_id"):
                                        art_rec.theaudiodb_id = art_info["theaudiodb_id"]
                                    session.add(art_rec)
                                    session.commit()
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        print(f"[Scanner] Error enriching artist {artist_name}: {e}")

                for album_id, artist_name, album_title in albums_to_enrich:
                    if self._abort_event.is_set(): break
                    try:
                        alb_info = await async_fetch_theaudiodb_album(client, artist_name, album_title, theaudiodb_api_key)
                        if alb_info:
                            with Session(engine) as session:
                                alb_rec = session.get(Album, album_id)
                                if alb_rec:
                                    if alb_info.get("cover_filename") and not alb_rec.cover_filename:
                                        alb_rec.cover_filename = alb_info["cover_filename"]
                                    if alb_info.get("genre") and not alb_rec.genre:
                                        alb_rec.genre = alb_info["genre"]
                                    if alb_info.get("year") and not alb_rec.year:
                                        alb_rec.year = alb_info["year"]
                                    if alb_info.get("theaudiodb_id"):
                                        alb_rec.theaudiodb_id = alb_info["theaudiodb_id"]
                                    session.add(alb_rec)
                                    session.commit()
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        print(f"[Scanner] Error enriching album {album_title}: {e}")

            # Reconcile & consolidate any split featured artists or fragmented albums
            def _post_scan_consolidation():
                with Session(engine) as session:
                    consolidate_music_artists_and_albums(session)
            await asyncio.to_thread(_post_scan_consolidation)

        with self._lock:
            self.action = "Scan complete"
            self.last_summary = f"Scanned {self.items_scanned} media item(s) successfully."

    async def _commit_music_batch(self, batch_tracks, artists_to_enrich, albums_to_enrich):
        def _commit():
            with Session(engine) as session:
                for item in batch_tracks:
                    meta = item["meta"]
                    fpath = item["full_path"]

                    try:
                        if is_silence_or_filler_track(meta.get("title"), fpath, meta.get("duration", 0.0)):
                            continue

                        # 1. Match or Create Primary Artist (always normalized, case-insensitive match)
                        raw_artist_name = meta.get("album_artist") or meta.get("artist") or "Unknown Artist"
                        primary_artist_name = normalize_artist_name(raw_artist_name)

                        # Match case-insensitively to prevent duplicates like 'Korn' vs 'KoRn'
                        artist = session.exec(
                            select(Artist).where(func.lower(Artist.name) == primary_artist_name.lower())
                        ).first()
                        if not artist:
                            artist = Artist(name=primary_artist_name)
                            session.add(artist)
                            session.commit()
                            session.refresh(artist)
                            artists_to_enrich.add((artist.id, artist.name))
                        elif not artist.bio or not artist.thumbnail_filename:
                            artists_to_enrich.add((artist.id, artist.name))

                        # 2. Match or Create Album (case-insensitive title under primary artist)
                        raw_album_title = (meta.get("album") or "Unknown Album").strip()
                        album_title, disc_from_title = normalize_album_title(raw_album_title)
                        if disc_from_title and (not meta.get("disc_number") or meta.get("disc_number") == 1):
                            meta["disc_number"] = disc_from_title
                        album = session.exec(
                            select(Album).where(
                                func.lower(Album.title) == album_title.lower(),
                                Album.artist_id == artist.id
                            )
                        ).first()
                        if not album:
                            album = Album(
                                title=album_title,
                                artist_id=artist.id,
                                artist_name=artist.name,
                                year=meta.get("year"),
                                genre=meta.get("genre"),
                                cover_filename=meta.get("cover_filename")
                            )
                            session.add(album)
                            session.commit()
                            session.refresh(album)
                            if not album.cover_filename:
                                albums_to_enrich.add((album.id, artist.name, album.title))
                        else:
                            dirty_alb = False
                            if not album.cover_filename and meta.get("cover_filename"):
                                album.cover_filename = meta["cover_filename"]
                                dirty_alb = True
                            if not album.year and meta.get("year"):
                                album.year = meta["year"]
                                dirty_alb = True
                            if not album.genre and meta.get("genre"):
                                album.genre = meta["genre"]
                                dirty_alb = True
                            if dirty_alb:
                                session.add(album)
                                session.commit()
                            elif not album.cover_filename:
                                albums_to_enrich.add((album.id, artist.name, album.title))

                        # 3. Add or Update Track
                        existing_trk = session.exec(select(Track).where(Track.file_path == fpath)).first()
                        cover_art = meta.get("cover_filename") or album.cover_filename
                        trk_format = meta.get("codec") or "mp3"
                        track_title = meta.get("title") or os.path.splitext(os.path.basename(fpath))[0]
                        if not existing_trk:
                            track = Track(
                                title=track_title,
                                file_path=fpath,
                                artist_id=artist.id,
                                artist_name=artist.name,
                                album_id=album.id,
                                album_title=album.title,
                                track_number=meta.get("track_number", 1),
                                disc_number=meta.get("disc_number", 1),
                                duration=meta.get("duration", 0.0),
                                bitrate=meta.get("bitrate"),
                                codec=meta.get("codec"),
                                format=trk_format,
                                year=meta.get("year") or album.year,
                                genre=meta.get("genre") or album.genre,
                                cover_filename=cover_art,
                                date_added=datetime.now(timezone.utc)
                            )
                            session.add(track)
                        else:
                            existing_trk.title = track_title
                            existing_trk.artist_id = artist.id
                            existing_trk.artist_name = artist.name
                            existing_trk.album_id = album.id
                            existing_trk.album_title = album.title
                            existing_trk.track_number = meta.get("track_number", 1)
                            existing_trk.disc_number = meta.get("disc_number", 1)
                            existing_trk.duration = meta.get("duration", 0.0)
                            if trk_format and not getattr(existing_trk, 'format', None):
                                existing_trk.format = trk_format
                            if cover_art and not existing_trk.cover_filename:
                                existing_trk.cover_filename = cover_art
                            session.add(existing_trk)

                        session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"[Scanner] Error committing track {fpath}: {e}")

        await asyncio.to_thread(_commit)

# -----------------------------------------------------------------------------
# Global Singleton and Entry Points
# -----------------------------------------------------------------------------

scanner_manager = ScannerManager()

def scan_media_library(session: Optional[Session] = None):
    """
    Non-blocking entry point for library scanning.
    Kicks off the asynchronous scanner background worker.
    """
    scanner_manager.start_scan()