import os
import re
import json
import time
import threading
import asyncio
import subprocess
from typing import Optional, Dict, Any, List
import requests
try:
    import httpx
except ImportError:
    httpx = None

from sqlmodel import Session, select
from .database import engine
from .models import Movie, TVShow, Photo, VolumePath, SystemConfig

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
METADATA_DIR = "/config/metadata"

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
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
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

async def async_fetch_additional_details(client: Any, tmdb_id: int, media_type: str, api_key: str):
    """Asynchronously fetches credits, ratings, and genres from TMDB."""
    try:
        if media_type == "movie":
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={api_key}&append_to_response=credits,release_dates"
        else:
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&append_to_response=credits,content_ratings"
            
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
                            
            return cast_str, dir_str, content_rating, genres_str
    except Exception:
        pass
    return None, None, None, None

async def async_extract_chapter_markers(file_path: str):
    """Non-blocking chapter marker extraction using asyncio.create_subprocess_exec."""
    from .ffmpeg_setup import get_ffprobe_path
    try:
        proc = await asyncio.create_subprocess_exec(
            get_ffprobe_path(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_chapters",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
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
    """Thread-offloaded thumbnail generator for photos and home videos."""
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
            subprocess.run(ffmpeg_cmd, capture_output=True, check=True, timeout=10)
        else:
            from PIL import Image
            with Image.open(full_path) as img:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.thumbnail((400, 400))
                img.save(thumb_path, "JPEG", quality=85)
        return True
    except Exception as e:
        print(f"[Scanner] Error generating thumbnail for {full_path}: {e}")
        return False

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

    def get_status(self) -> Dict[str, Any]:
        """Returns JSON-serializable snapshot of current scanning progress."""
        with self._lock:
            now = time.time()
            recently_completed = (not self.is_scanning) and (now - self.last_completed_time < 5.0) and self.last_completed_time > 0

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
            elif recently_completed:
                short_status = "Scan Complete"

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
                "recently_completed": recently_completed,
                "last_summary": self.last_summary,
                "elapsed_seconds": int(now - self.start_time) if self.is_scanning else 0
            }

    def start_scan(self) -> bool:
        """Triggers non-blocking background async library scan if not already running."""
        with self._lock:
            if self.is_scanning:
                print("[Scanner] A scan is already running, skipping duplicate trigger.")
                return False
            self.is_scanning = True
            self.phase = "discovering"
            self.action = "Discovering media files..."
            self.current_item = ""
            self.items_scanned = 0
            self.total_items = 0
            self.start_time = time.time()

        self._thread = threading.Thread(target=self._run_worker, daemon=True)
        self._thread.start()
        return True

    def _run_worker(self):
        """Worker thread entry point that executes the asyncio event loop."""
        try:
            asyncio.run(self._async_scan())
        except Exception as e:
            print(f"[Scanner] Error during async scan: {e}")
        finally:
            with self._lock:
                self.is_scanning = False
                self.last_completed_time = time.time()
                self.phase = "complete"
                self.action = "Scan complete"

    async def _async_scan(self):
        """Main async scanning pipeline."""
        # 1. Fetch volume paths and configurations
        with Session(engine) as session:
            volume_paths = session.exec(select(VolumePath)).all()
            movie_volume_paths = [v.path for v in volume_paths if v.media_type == "movie"]
            tv_volume_paths = [v.path for v in volume_paths if v.media_type == "tv"]
            photo_volume_paths = [v.path for v in volume_paths if v.media_type == "photo"]

            config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
            tmdb_api_key = config.value if config else os.getenv("TMDB_API_KEY")

            existing_movie_paths = set(session.exec(select(Movie.file_path)).all())
            existing_tv_paths = set(session.exec(select(TVShow.file_path)).all())
            existing_photos = {p.file_path: p.poster_filename for p in session.exec(select(Photo)).all()}

        def is_valid_path(filepath, volumes):
            if not os.path.exists(filepath):
                return False
            for v in volumes:
                if filepath.startswith(v.rstrip('/') + '/'):
                    return True
            return False

        # 2. Cleanup missing files from database
        with self._lock:
            self.phase = "cleanup"
            self.action = "Cleaning up removed files..."

        with Session(engine) as session:
            for m in session.exec(select(Movie)).all():
                if m.file_path and not m.file_path.startswith("remote_") and not is_valid_path(m.file_path, movie_volume_paths):
                    session.delete(m)
            for t in session.exec(select(TVShow)).all():
                if t.file_path and not t.file_path.startswith("remote_") and not is_valid_path(t.file_path, tv_volume_paths):
                    session.delete(t)
            for p in session.exec(select(Photo)).all():
                if p.file_path and not is_valid_path(p.file_path, photo_volume_paths):
                    session.delete(p)
            try:
                session.commit()
            except Exception:
                session.rollback()

        # 3. Quick Discovery Phase (Thread-offloaded os.walk)
        with self._lock:
            self.phase = "discovering"
            self.action = "Discovering new media files..."

        def _discover_candidates():
            new_movies = []
            for movies_dir in movie_volume_paths:
                if os.path.exists(movies_dir):
                    for root, dirs, files in os.walk(movies_dir):
                        for file in files:
                            if file.lower().endswith(VIDEO_EXTENSIONS):
                                full_path = os.path.join(root, file)
                                if full_path not in existing_movie_paths:
                                    new_movies.append((full_path, file))

            new_shows = []
            for tv_dir in tv_volume_paths:
                if os.path.exists(tv_dir):
                    for root, dirs, files in os.walk(tv_dir):
                        for file in files:
                            if file.lower().endswith(VIDEO_EXTENSIONS):
                                full_path = os.path.join(root, file)
                                if full_path not in existing_tv_paths:
                                    new_shows.append((full_path, file, root, tv_dir))

            new_photos = []
            for photo_dir in photo_volume_paths:
                if os.path.exists(photo_dir):
                    for root, dirs, files in os.walk(photo_dir):
                        for file in files:
                            if file.lower().endswith(PHOTO_EXTENSIONS):
                                full_path = os.path.join(root, file)
                                if full_path not in existing_photos or not existing_photos.get(full_path):
                                    new_photos.append((full_path, file, root, photo_dir))

            return new_movies, new_shows, new_photos

        new_movies, new_shows, new_photos = await asyncio.to_thread(_discover_candidates)
        total_items = len(new_movies) + len(new_shows) + len(new_photos)

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
            for full_path, file in new_movies:
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
                            
                            cast, director, content_rating, genres_str = await async_fetch_additional_details(client, tmdb_id, "movie", tmdb_api_key)
                    except Exception as e:
                        print(f"[Scanner] Movie TMDB lookup failed for {search_title}: {e}")

                intro_s, intro_e, cred_s, cred_e = await async_extract_chapter_markers(full_path)

                with Session(engine) as session:
                    if not session.exec(select(Movie).where(Movie.file_path == full_path)).first():
                        new_movie = Movie(
                            title=search_title, file_path=full_path, year=year,
                            plot=plot, poster_filename=poster_filename,
                            backdrop_filename=backdrop_filename, cast=cast, director=director, rating=rating,
                            content_rating=content_rating, genres=genres_str,
                            intro_start=intro_s, intro_end=intro_e, credits_start=cred_s, credits_end=cred_e
                        )
                        session.add(new_movie)
                        try:
                            session.commit()
                        except Exception as e:
                            session.rollback()
                            print(f"[Scanner] Skipping duplicate or failed movie {full_path}: {e}")

                with self._lock:
                    self.items_scanned += 1
                await asyncio.sleep(0.01)

            # 4B. Scan TV Shows
            for full_path, file, root, tv_dir in new_shows:
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
                                
                            cast, director, content_rating, genres_str = await async_fetch_additional_details(client, tmdb_id, "tv", tmdb_api_key)
                            
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

                with Session(engine) as session:
                    if not session.exec(select(TVShow).where(TVShow.file_path == full_path)).first():
                        new_show = TVShow(
                            title=search_title, file_path=full_path, year=year,
                            plot=plot, poster_filename=poster_filename,
                            backdrop_filename=backdrop_filename, cast=cast, director=director, rating=rating,
                            season=season, episode=episode, season_poster_filename=season_poster_filename,
                            content_rating=content_rating, genres=genres_str,
                            intro_start=intro_s, intro_end=intro_e, credits_start=cred_s, credits_end=cred_e
                        )
                        session.add(new_show)
                        try:
                            session.commit()
                        except Exception as e:
                            session.rollback()
                            print(f"[Scanner] Skipping duplicate or failed TV show {full_path}: {e}")

                with self._lock:
                    self.items_scanned += 1
                await asyncio.sleep(0.01)

            # 4C. Scan Photos & Home Videos
            for full_path, file, root, photo_dir in new_photos:
                raw_title = os.path.splitext(file)[0]
                with self._lock:
                    self.phase = "photos"
                    self.action = f"Scanning Photos ({self.items_scanned + 1}/{self.total_items})"
                    self.current_item = raw_title

                rel_path = os.path.relpath(root, photo_dir)
                album = rel_path if rel_path != "." else None

                with Session(engine) as session:
                    existing = session.exec(select(Photo).where(Photo.file_path == full_path)).first()
                    if not existing:
                        photo_obj = Photo(title=raw_title, file_path=full_path, album=album)
                        session.add(photo_obj)
                        try:
                            session.commit()
                            session.refresh(photo_obj)
                        except Exception as e:
                            session.rollback()
                            print(f"[Scanner] Skipping duplicate photo record {full_path}: {e}")
                            with self._lock:
                                self.items_scanned += 1
                            continue
                    else:
                        photo_obj = existing
                        photo_obj.album = album

                    photo_id = photo_obj.id

                thumb_filename = f"photo_{photo_id}_poster.jpg"
                thumb_path = os.path.join(METADATA_DIR, thumb_filename)
                ext = os.path.splitext(file)[1].lower()
                is_video = ext in (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm")

                success = await asyncio.to_thread(_generate_photo_thumbnail, full_path, thumb_path, is_video)
                if success:
                    with Session(engine) as session:
                        p = session.get(Photo, photo_id)
                        if p:
                            p.poster_filename = thumb_filename
                            session.add(p)
                            try:
                                session.commit()
                            except Exception:
                                session.rollback()

                with self._lock:
                    self.items_scanned += 1
                await asyncio.sleep(0.01)

        with self._lock:
            self.action = "Scan complete"
            self.last_summary = f"Scanned {self.items_scanned} media item(s) successfully."

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