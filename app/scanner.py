import os
import re
import requests
from sqlmodel import Session, select
from .models import Movie, TVShow, Photo, VolumePath, SystemConfig

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".m4v")
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")
METADATA_DIR = "/config/metadata"

def download_image(image_path, prefix, tmdb_id, img_type="poster"):
    if not image_path: 
        return None
    os.makedirs(METADATA_DIR, exist_ok=True)
    filename = f"{prefix}_{tmdb_id}_{img_type}.jpg"
    filepath = os.path.join(METADATA_DIR, filename)
    
    if not os.path.exists(filepath):
        # Use w1280 for backdrops, w500 for posters
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

def fetch_credits(tmdb_id: int, media_type: str, api_key: str):
    import json
    try:
        url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/credits?api_key={api_key}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            
            cast_list = []
            for c in data.get("cast", [])[:5]:
                profile_filename = None
                if c.get("profile_path"):
                    profile_filename = download_image(c["profile_path"], "person", c["id"], "profile")
                
                cast_list.append({
                    "name": c["name"],
                    "character": c.get("character", ""),
                    "profile_filename": profile_filename
                })
            
            # Director(s)
            directors = [c["name"] for c in data.get("crew", []) if c.get("job") == "Director"]
            
            cast_str = json.dumps(cast_list) if cast_list else None
            dir_str = ", ".join(directors) if directors else None
            return cast_str, dir_str
    except Exception:
        pass
    return None, None

def scan_media_library(session: Session):
    volume_paths = session.exec(select(VolumePath)).all()
    movie_volume_paths = [v.path for v in volume_paths if v.media_type == "movie"]
    tv_volume_paths = [v.path for v in volume_paths if v.media_type == "tv"]
    photo_volume_paths = [v.path for v in volume_paths if v.media_type == "photo"]
    
    config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
    tmdb_api_key = config.value if config else os.getenv("TMDB_API_KEY")

    def is_valid_path(filepath, volumes):
        if not os.path.exists(filepath):
            return False
        for v in volumes:
            if filepath.startswith(v.rstrip('/') + '/'):
                return True
        return False

    # 0. Clean up missing media from the database
    for m in session.exec(select(Movie)).all():
        if m.file_path and not m.file_path.startswith("remote_") and not is_valid_path(m.file_path, movie_volume_paths):
            session.delete(m)
    for t in session.exec(select(TVShow)).all():
        if t.file_path and not t.file_path.startswith("remote_") and not is_valid_path(t.file_path, tv_volume_paths):
            session.delete(t)
    for p in session.exec(select(Photo)).all():
        if p.file_path and not is_valid_path(p.file_path, photo_volume_paths):
            session.delete(p)
    session.commit()

    # 1. Scan Movies
    for movies_dir in movie_volume_paths:
        if os.path.exists(movies_dir):
            for root, dirs, files in os.walk(movies_dir):
                for file in files:
                    if file.lower().endswith(VIDEO_EXTENSIONS):
                        full_path = os.path.join(root, file)
                        raw_title = os.path.splitext(file)[0]
                        
                        if not session.exec(select(Movie).where(Movie.file_path == full_path)).first():
                            match = re.match(r"(.*?)\s*\((\d{4})\)", raw_title)
                            search_title = match.group(1) if match else raw_title
                            year = match.group(2) if match else ""
                            
                            plot, poster_filename, backdrop_filename = None, None, None
                            rating = None
                            cast, director = None, None
                            
                            if tmdb_api_key:
                                url = f"https://api.themoviedb.org/3/search/movie?query={search_title}&year={year}&api_key={tmdb_api_key}"
                                try:
                                    res = requests.get(url, timeout=10)
                                    if res.status_code == 200 and res.json().get("results"):
                                        data = res.json()["results"][0]
                                        tmdb_id = data.get("id")
                                        plot = data.get("overview")
                                        poster_filename = download_image(data.get("poster_path"), "movie", tmdb_id, "poster")
                                        backdrop_filename = download_image(data.get("backdrop_path"), "movie", tmdb_id, "backdrop")
                                        rating = data.get("vote_average")
                                        search_title = data.get("title", search_title)
                                        year = data.get("release_date", "")[:4]
                                        
                                        cast, director = fetch_credits(tmdb_id, "movie", tmdb_api_key)
                                except Exception:
                                    pass

                            new_movie = Movie(
                                title=search_title, file_path=full_path, year=year, 
                                plot=plot, poster_filename=poster_filename,
                                backdrop_filename=backdrop_filename, cast=cast, director=director, rating=rating
                            )
                            session.add(new_movie)
                            session.commit()

    # 2. Scan TV Shows
    for tv_dir in tv_volume_paths:
        if os.path.exists(tv_dir):
            for root, dirs, files in os.walk(tv_dir):
                for file in files:
                    if file.lower().endswith(VIDEO_EXTENSIONS):
                        full_path = os.path.join(root, file)
                        raw_title = os.path.splitext(file)[0]
                        
                        if not session.exec(select(TVShow).where(TVShow.file_path == full_path)).first():
                            title_match = re.search(r'^(.*?)\s*[-_]?\s*(?:[Ss]\d+\s*[-_]?\s*[Ee]?\d+|\d+x\d+)', raw_title, re.IGNORECASE)
                            if title_match:
                                search_title = title_match.group(1).strip()
                            else:
                                search_title = raw_title.split(" - ")[0]
                            
                            # Strip trailing year in parentheses, e.g. " (2021)"
                            search_title = re.sub(r'\s*\(\d{4}\)$', '', search_title).strip()
                            
                            plot, poster_filename, backdrop_filename = None, None, None
                            rating = None
                            cast, director = None, None
                            
                            if tmdb_api_key:
                                url = f"https://api.themoviedb.org/3/search/tv?query={search_title}&api_key={tmdb_api_key}"
                                try:
                                    res = requests.get(url, timeout=10)
                                    if res.status_code == 200 and res.json().get("results"):
                                        data = res.json()["results"][0]
                                        tmdb_id = data.get("id")
                                        plot = data.get("overview")
                                        poster_filename = download_image(data.get("poster_path"), "tv", tmdb_id, "poster")
                                        backdrop_filename = download_image(data.get("backdrop_path"), "tv", tmdb_id, "backdrop")
                                        search_title = data.get("name", search_title)
                                        rating = data.get("vote_average")
                                        
                                        if season_number is not None:
                                            try:
                                                s_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key={tmdb_api_key}"
                                                s_res = requests.get(s_url, timeout=10)
                                                if s_res.status_code == 200:
                                                    s_data = s_res.json()
                                                    season_poster_filename = download_image(s_data.get("poster_path"), "tv_season", f"{tmdb_id}_{season_number}", "poster")
                                            except Exception:
                                                pass
                                        
                                        cast, director = fetch_credits(tmdb_id, "tv", tmdb_api_key)
                                except Exception:
                                    pass

                            new_show = TVShow(
                                title=search_title, file_path=full_path, year="", 
                                plot=plot, poster_filename=poster_filename,
                                backdrop_filename=backdrop_filename, cast=cast, director=director, rating=rating
                            )
                            session.add(new_show)
                            session.commit()

    # 3. Scan Photos
    for photo_dir in photo_volume_paths:
        if os.path.exists(photo_dir):
            for root, dirs, files in os.walk(photo_dir):
                for file in files:
                    if file.lower().endswith(PHOTO_EXTENSIONS):
                        full_path = os.path.join(root, file)
                        raw_title = os.path.splitext(file)[0]
                        rel_path = os.path.relpath(root, photo_dir)
                        album = rel_path if rel_path != "." else None
                        
                        existing = session.exec(select(Photo).where(Photo.file_path == full_path)).first()
                        if not existing or not existing.poster_filename:
                            if not existing:
                                new_photo = Photo(title=raw_title, file_path=full_path, album=album)
                                session.add(new_photo)
                                session.commit()
                                session.refresh(new_photo)
                            else:
                                new_photo = existing
                                new_photo.album = album
                            
                            thumb_filename = f"photo_{new_photo.id}_poster.jpg"
                            thumb_path = os.path.join(METADATA_DIR, thumb_filename)
                            
                            ext = os.path.splitext(file)[1].lower()
                            is_video = ext in (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm")
                            
                            try:
                                if is_video:
                                    import subprocess
                                    from .ffmpeg_setup import get_ffmpeg_path
                                    ffmpeg_cmd = [
                                        get_ffmpeg_path(), "-y",
                                        "-i", full_path,
                                        "-ss", "00:00:00.000",
                                        "-vframes", "1",
                                        thumb_path
                                    ]
                                    subprocess.run(ffmpeg_cmd, capture_output=True, check=True)
                                else:
                                    from PIL import Image
                                    with Image.open(full_path) as img:
                                        if img.mode != 'RGB':
                                            img = img.convert('RGB')
                                        img.thumbnail((400, 400))
                                        img.save(thumb_path, "JPEG", quality=85)
                                        
                                new_photo.poster_filename = thumb_filename
                                session.add(new_photo)
                                session.commit()
                            except Exception as e:
                                print(f"Error generating thumbnail for {full_path}: {e}")

    session.commit()