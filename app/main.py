from fastapi import FastAPI, Depends, Form, Request, HTTPException, Response, WebSocket, WebSocketDisconnect, Query, BackgroundTasks
import threading
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Optional, Union
from sqlalchemy import text
from sqlmodel import Session, select, func
import bcrypt
import os
import uuid
import json
import time
import re
import zipfile
import hmac
import hashlib
import urllib.parse
import random
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from .database import create_db_and_tables, get_session, engine
from .models import User, Movie, TVShow, InviteCode, Watchlist, WatchHistory, VolumePath, SystemConfig, Photo, WatchSession, WatchInvite, PasswordResetRequest, Profile, Artist, Album, Track, Playlist, PlaylistTrack
from .scanner import METADATA_DIR, scan_media_library, scanner_manager, is_recycle_name, is_recycle_path, download_image, async_download_image, async_fetch_additional_details, SafeHTTPClient, httpx, find_local_trailer, extract_tmdb_trailer_key
from .watcher import start_watcher, stop_watcher, scanner_debouncer
from .ffmpeg_setup import ensure_ffmpeg, get_ffmpeg_path, get_ffprobe_path, get_video_transcode_args, get_working_encoder, probe_media_streams, extract_subtitle_to_vtt, get_subtitle_cache_dir, shift_vtt_timestamps, get_keyframe_before_time, sanitize_vtt_content, get_video_transcode_pipeline, parse_burn_sub_index, get_smart_audio_pipeline, extract_video_frame
from .hls_manager import hls_manager
from .trickplay import (
    queue_trickplay_generation,
    is_trickplay_ready,
    is_trickplay_generating,
    get_trickplay_manifest,
    get_trickplay_item_dir,
)
import subprocess
import shutil
try:
    import psutil
except ImportError:
    psutil = None

app = FastAPI(title="Heirloom")

# Add CORS middleware to allow Flutter Web testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}
        self.session_clients: dict[str, dict[WebSocket, dict]] = {}
        self.session_playback: dict[str, dict] = {}

    async def connect(self, websocket: WebSocket, session_id: str, user_id: int = None, username: str = None, is_host: bool = False):
        await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
            self.session_clients[session_id] = {}
            if session_id not in self.session_playback:
                self.session_playback[session_id] = {"current_time": 0.0, "is_paused": True, "updated_at": time.time()}
        self.active_connections[session_id].append(websocket)
        self.session_clients[session_id][websocket] = {
            "user_id": user_id,
            "username": username or "Guest",
            "is_host": is_host,
            "is_ready": False,
            "time": 0.0
        }

    def disconnect(self, websocket: WebSocket, session_id: str):
        was_host = False
        if session_id in self.session_clients and websocket in self.session_clients[session_id]:
            was_host = self.session_clients[session_id][websocket].get("is_host", False)
            del self.session_clients[session_id][websocket]

        if session_id in self.active_connections:
            if websocket in self.active_connections[session_id]:
                self.active_connections[session_id].remove(websocket)
            if not self.active_connections[session_id]:
                del self.active_connections[session_id]
                if session_id in self.session_clients:
                    del self.session_clients[session_id]

        remaining = len(self.active_connections.get(session_id, []))
        return was_host, remaining

    async def broadcast(self, message: str, session_id: str, sender: WebSocket = None):
        if session_id in self.active_connections:
            for connection in list(self.active_connections[session_id]):
                if connection != sender:
                    try:
                        await connection.send_text(message)
                    except Exception:
                        pass

    def check_all_ready(self, session_id: str) -> bool:
        clients = self.session_clients.get(session_id, {})
        if not clients:
            return True
        return all(c.get("is_ready", False) for c in clients.values())

    def get_waiting_users(self, session_id: str) -> list[str]:
        clients = self.session_clients.get(session_id, {})
        return [c.get("username", "User") for c in clients.values() if not c.get("is_ready", False)]

    def get_session_time(self, session_id: str) -> float:
        return self.session_playback.get(session_id, {}).get("current_time", 0.0)

manager = ConnectionManager()

SERVER_START_TIME = time.time()

def format_seconds(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return "0:00"
    sec = int(round(seconds))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

class ActiveStreamManager:
    def __init__(self):
        self.streams: dict[str, dict] = {}

    def register_or_update(
        self,
        session_key: str,
        user_id: int,
        username: str,
        media_type: str,
        item_id: int,
        title: str,
        poster_filename: Optional[str],
        progress: float,
        current_time: float = 0.0,
        duration: float = 0.0,
        play_type: str = "Direct Play",
        season_episode: str = "",
        ip: str = ""
    ):
        cur_sec_int = int(round(current_time or 0.0))
        dur_sec_int = int(round(duration or 0.0))
        time_left = max(0, dur_sec_int - cur_sec_int)
        time_display = f"{format_seconds(cur_sec_int)} / {format_seconds(dur_sec_int)} -{format_seconds(time_left)}" if dur_sec_int > 0 else f"{round(progress, 1)}%"

        self.streams[session_key] = {
            "session_key": session_key,
            "user_id": user_id,
            "username": username,
            "media_type": media_type,
            "item_id": item_id,
            "title": title,
            "poster_filename": poster_filename,
            "progress": round(progress, 1),
            "current_time": round(current_time, 1),
            "duration": round(duration, 1),
            "time_display": time_display,
            "play_type": play_type,
            "season_episode": season_episode,
            "ip": ip,
            "last_seen": time.time(),
            "started_at": self.streams.get(session_key, {}).get("started_at", time.time())
        }

    def stop_stream(self, session_key: str):
        if session_key in self.streams:
            del self.streams[session_key]

    def get_active_streams(self, timeout: float = 20.0) -> list[dict]:
        now = time.time()
        expired = [k for k, v in self.streams.items() if now - v.get("last_seen", 0) > timeout]
        for k in expired:
            del self.streams[k]
        return list(self.streams.values())

active_stream_manager = ActiveStreamManager()

os.makedirs("/config/metadata", exist_ok=True)
app.mount("/metadata", StaticFiles(directory="/config/metadata"), name="metadata")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    with Session(engine) as session:
        # Schema Migrations
        columns = [
            ("movie", "backdrop_filename", "VARCHAR"),
            ("movie", "cast", "VARCHAR"),
            ("movie", "director", "VARCHAR"),
            ("tvshow", "backdrop_filename", "VARCHAR"),
            ("tvshow", "cast", "VARCHAR"),
            ("tvshow", "director", "VARCHAR"),
            ("tvshow", "logo_filename", "VARCHAR"),
            ("tvshow", "thumbnail_filename", "VARCHAR"),
            ("movie", "logo_filename", "VARCHAR"),
            ("user", "photos_access", "BOOLEAN DEFAULT 1"),
            ("user", "downloads_access", "BOOLEAN DEFAULT 1"),
            ("user", "watch_together_access", "BOOLEAN DEFAULT 1"),
            ("photo", "album", "VARCHAR"),
            ("photo", "poster_filename", "VARCHAR"),
            ("movie", "runtime", "INTEGER"),
            ("tvshow", "runtime", "INTEGER"),
            ("movie", "intro_start", "FLOAT"),
            ("movie", "intro_end", "FLOAT"),
            ("movie", "credits_start", "FLOAT"),
            ("movie", "credits_end", "FLOAT"),
            ("tvshow", "intro_start", "FLOAT"),
            ("tvshow", "intro_end", "FLOAT"),
            ("tvshow", "credits_start", "FLOAT"),
            ("tvshow", "credits_end", "FLOAT"),
            ("watchsession", "is_active", "BOOLEAN DEFAULT 1"),
            ("user", "music_access", "BOOLEAN DEFAULT 0"),
            ("invitecode", "music_access", "BOOLEAN DEFAULT 0"),
            ("album", "genre", "VARCHAR"),
            ("album", "theaudiodb_id", "INTEGER"),
            ("album", "artist_name", "VARCHAR"),
            ("album", "year", "VARCHAR"),
            ("album", "cover_filename", "VARCHAR"),
            ("artist", "bio", "TEXT"),
            ("artist", "thumbnail_filename", "VARCHAR"),
            ("artist", "banner_filename", "VARCHAR"),
            ("artist", "theaudiodb_id", "INTEGER"),
            ("track", "genre", "VARCHAR"),
            ("track", "bitrate", "INTEGER"),
            ("track", "codec", "VARCHAR"),
            ("track", "disc_number", "INTEGER DEFAULT 1"),
            ("track", "duration", "FLOAT DEFAULT 0.0"),
            ("track", "track_number", "INTEGER DEFAULT 1"),
            ("track", "year", "VARCHAR"),
            ("track", "cover_filename", "VARCHAR"),
        ]
        for table, col, dtype in columns:
            try:
                session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}"))
                session.commit()
            except Exception as e:
                session.rollback()
        
        try:
            session.execute(text("UPDATE user SET photos_access = 1 WHERE photos_access IS NULL"))
            session.execute(text("UPDATE user SET downloads_access = 1 WHERE downloads_access IS NULL"))
            session.execute(text("UPDATE user SET watch_together_access = 1 WHERE watch_together_access IS NULL"))
            session.execute(text("UPDATE user SET music_access = 1 WHERE is_admin = 1 AND (music_access IS NULL OR music_access = 0)"))
            session.commit()
        except Exception:
            session.rollback()
                
        # Ensure federation key exists
        fed_key = session.exec(select(SystemConfig).where(SystemConfig.key == "federation_api_key")).first()
        if not fed_key:
            fed_key = SystemConfig(key="federation_api_key", value=str(uuid.uuid4()))
            session.add(fed_key)
            session.commit()

        # Consolidate any split music featured artists and fragmented albums (run once at setup)
        try:
            cons_done = session.exec(select(SystemConfig).where(SystemConfig.key == "music_consolidation_done")).first()
            if not cons_done:
                from .scanner import consolidate_music_artists_and_albums
                consolidate_music_artists_and_albums(session)
                session.add(SystemConfig(key="music_consolidation_done", value="true"))
                session.commit()
        except Exception as e:
            session.rollback()
            print(f"[Startup] Note during music artist/album consolidation: {e}")
            
    import threading
    def fix_sideways_photo_thumbnails():
        try:
            with Session(engine) as s:
                orient_done = s.exec(select(SystemConfig).where(SystemConfig.key == "photo_orientation_done")).first()
                if orient_done:
                    return
            from PIL import Image
            from .scanner import METADATA_DIR, _generate_photo_thumbnail
            with Session(engine) as s:
                photos = s.exec(select(Photo)).all()
            for p in photos:
                if not p.file_path or not os.path.exists(p.file_path):
                    continue
                ext = os.path.splitext(p.file_path)[1].lower()
                if ext in (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"):
                    continue
                p_hash = hashlib.md5(p.file_path.encode("utf-8")).hexdigest()[:16]
                t_path = os.path.join(METADATA_DIR, f"photo_{p_hash}_poster.jpg")
                if not os.path.exists(t_path):
                    _generate_photo_thumbnail(p.file_path, t_path, False)
                    continue
                try:
                    with Image.open(p.file_path) as src_im:
                        ex = src_im.getexif()
                        orient = ex.get(0x0112)
                        if orient and orient in (2, 3, 4, 5, 6, 7, 8):
                            with Image.open(t_path) as th_im:
                                src_is_portrait = (src_im.height > src_im.width) if orient not in (5, 6, 7, 8) else (src_im.width > src_im.height)
                                th_is_portrait = (th_im.height > th_im.width)
                                if src_is_portrait != th_is_portrait:
                                    _generate_photo_thumbnail(p.file_path, t_path, False)
                except Exception:
                    pass
            with Session(engine) as s:
                s.add(SystemConfig(key="photo_orientation_done", value="true"))
                s.commit()
        except Exception as e:
            print(f"[Startup] Note checking photo thumbnails: {e}")

    threading.Thread(target=fix_sideways_photo_thumbnails, daemon=True).start()

    def initial_scan():
        with Session(engine) as scan_session:
            scan_media_library(scan_session)
            
    threading.Thread(target=initial_scan, daemon=True).start()
    
    # Download ffmpeg if missing
    ensure_ffmpeg()
    
    start_watcher()

@app.on_event("shutdown")
def on_shutdown():
    stop_watcher()
    hls_manager.shutdown()

def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    if not token:
        token = request.query_params.get("token")
    if not token:
        return None
    with Session(engine) as session:
        return session.exec(select(User).where(User.session_token == token)).first()

PIN_REGEX = re.compile(r"^\d{4,15}$")

def validate_pin(pin: Optional[str]) -> bool:
    if not pin:
        return False
    return bool(PIN_REGEX.match(pin.strip()))

def hash_pin(pin: str) -> str:
    return bcrypt.hashpw(pin.strip().encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_pin(pin: str, pin_hash: Optional[str]) -> bool:
    if not pin or not pin_hash:
        return False
    try:
        return bcrypt.checkpw(pin.strip().encode('utf-8'), pin_hash.encode('utf-8'))
    except Exception:
        return False

def make_profile_unlock_token(profile_id: int, user_session_token: str) -> str:
    if not user_session_token:
        return ""
    return hmac.new(user_session_token.encode('utf-8'), str(profile_id).encode('utf-8'), hashlib.sha256).hexdigest()[:32]

CHILD_SAFE_RATINGS = {"G", "PG", "TV-Y", "TV-Y7", "TV-Y7-FV", "TV-G", "TV-PG"}
TEEN_SAFE_RATINGS = {"G", "PG", "PG-13", "TV-Y", "TV-Y7", "TV-Y7-FV", "TV-G", "TV-PG", "TV-14"}

def is_rating_allowed(rating: Optional[str], rating_level: int) -> bool:
    """
    Checks if a media item's content rating is permitted for the given rating level:
    - 0: Adult (Unrestricted / all content allowed)
    - 1: Teen (Up to PG-13 & TV-14)
    - 2: Child (Up to PG & TV-PG)
    """
    if rating_level <= 0:
        return True
    if not rating:
        return False
    norm = rating.strip().upper()
    if rating_level == 1:
        return norm in TEEN_SAFE_RATINGS
    if rating_level >= 2:
        return norm in CHILD_SAFE_RATINGS
    return True

def is_kids_safe(rating: Optional[str]) -> bool:
    return is_rating_allowed(rating, 2)

def get_profile_rating_level(user: Optional[User], profile: Optional[Profile]) -> int:
    if profile:
        if getattr(profile, "rating_level", None) is not None:
            return profile.rating_level
        if getattr(profile, "is_kids", False):
            return 2
    return 0

def is_profile_kids(user: Optional[User], profile: Optional[Profile]) -> bool:
    return get_profile_rating_level(user, profile) >= 2

def is_actual_admin(user: Optional[User], profile: Optional[Profile]) -> bool:
    if not user or not user.is_admin:
        return False
    if not profile or not profile.is_primary:
        return False
    return True

def get_current_profile(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user),
    session: Session = Depends(get_session)
) -> Optional[Profile]:
    if not current_user:
        return None

    # Check active_profile_id cookie or query parameter
    active_id_str = request.cookies.get("active_profile_id") or request.query_params.get("profile_id")
    profile = None
    if active_id_str:
        try:
            pid = int(active_id_str)
            p = session.get(Profile, pid)
            if p and p.user_id == current_user.id:
                if not p.pin_hash:
                    profile = p
                else:
                    unlock_token = request.cookies.get(f"profile_unlocked_{p.id}")
                    expected = make_profile_unlock_token(p.id, current_user.session_token or "")
                    if unlock_token and hmac.compare_digest(unlock_token, expected):
                        profile = p
        except (ValueError, TypeError):
            pass

    # Fallback to primary profile if no valid/unlocked profile found
    if not profile:
        candidate = session.exec(select(Profile).where(Profile.user_id == current_user.id, Profile.is_primary == True)).first()
        if candidate:
            if not candidate.pin_hash:
                profile = candidate
            else:
                unlock_token = request.cookies.get(f"profile_unlocked_{candidate.id}")
                expected = make_profile_unlock_token(candidate.id, current_user.session_token or "")
                if unlock_token and hmac.compare_digest(unlock_token, expected):
                    profile = candidate

    # Fallback to first profile without a PIN or unlocked
    if not profile:
        candidates = session.exec(select(Profile).where(Profile.user_id == current_user.id)).all()
        for cand in candidates:
            if not cand.pin_hash:
                profile = cand
                break
            else:
                unlock_token = request.cookies.get(f"profile_unlocked_{cand.id}")
                expected = make_profile_unlock_token(cand.id, current_user.session_token or "")
                if unlock_token and hmac.compare_digest(unlock_token, expected):
                    profile = cand
                    break

    # Auto-seed primary profile if none exists for this user
    if not profile:
        profile = Profile(
            user_id=current_user.id,
            name=current_user.username,
            avatar_color="#e50914",
            avatar_icon="user",
            is_primary=True,
            is_kids=False,
            rewind_seconds=current_user.rewind_seconds or 0
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)

    return profile

# Security & Rate Limiting Helpers
def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        if ip:
            return ip
    real_ip = request.headers.get("x-real-ip")
    if real_ip and real_ip.strip():
        return real_ip.strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"

def is_request_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    proto = request.headers.get("x-forwarded-proto", "")
    return proto.lower() == "https"

rate_limit_records: dict[str, dict] = {}

def check_rate_limit(
    target: Union[Request, str],
    max_attempts: int = 5,
    window_seconds: int = 60,
    error_message: str = "Too many attempts. Please try again later."
):
    key = get_client_ip(target) if isinstance(target, Request) else target
    now = datetime.now(timezone.utc)
    if len(rate_limit_records) > 500:
        for k in list(rate_limit_records.keys()):
            if (now - rate_limit_records[k]["last_attempt"]).total_seconds() > 3600:
                del rate_limit_records[k]

    entry = rate_limit_records.get(key)
    if entry:
        elapsed = (now - entry["last_attempt"]).total_seconds()
        if elapsed > window_seconds:
            del rate_limit_records[key]
        elif entry["count"] >= max_attempts:
            raise HTTPException(status_code=429, detail=error_message)

def record_failed_attempt(target: Union[Request, str]):
    key = get_client_ip(target) if isinstance(target, Request) else target
    now = datetime.now(timezone.utc)
    entry = rate_limit_records.get(key)
    if not entry or (now - entry["last_attempt"]).total_seconds() > 3600:
        rate_limit_records[key] = {"count": 1, "last_attempt": now}
    else:
        entry["count"] += 1
        entry["last_attempt"] = now

def clear_rate_limit(target: Union[Request, str]):
    key = get_client_ip(target) if isinstance(target, Request) else target
    rate_limit_records.pop(key, None)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    fav_path = "app/static/favicon.svg"
    if os.path.exists(fav_path):
        return FileResponse(fav_path, media_type="image/svg+xml")
    return Response(status_code=204)

# ----------------- AUTH & REGISTRATION -----------------

@app.get("/login", response_class=HTMLResponse)
async def view_login(request: Request, session: Session = Depends(get_session)):
    if not session.exec(select(User)).first():
        return templates.TemplateResponse(request=request, name="setup.html")
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/login")
async def process_login(request: Request, username: str = Form(...), password: str = Form(...), remember_me: Optional[str] = Form(None), session: Session = Depends(get_session)):
    check_rate_limit(request)
    user = session.exec(select(User).where(User.username == username)).first()
    if user and bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8')):
        user.session_token = str(uuid.uuid4())
        session.add(user)
        session.commit()

        # Check or ensure primary profile
        profiles = session.exec(select(Profile).where(Profile.user_id == user.id)).all()
        if not profiles:
            p = Profile(
                user_id=user.id,
                name=user.username,
                avatar_color="#e50914",
                avatar_icon="user",
                is_primary=True,
                is_kids=False,
                rewind_seconds=user.rewind_seconds or 0
            )
            session.add(p)
            session.commit()
            session.refresh(p)
            profiles = [p]

        # If user has multiple profiles or single profile has a PIN, route to /profiles
        is_secure = is_request_secure(request)
        clear_rate_limit(request)
        cookie_max_age = (60 * 60 * 24 * 30) if remember_me else None
        if len(profiles) > 1 or (len(profiles) == 1 and profiles[0].pin_hash):
            redirect = RedirectResponse(url="/profiles", status_code=303)
            redirect.set_cookie(key="session_token", value=user.session_token, httponly=True, max_age=cookie_max_age, samesite="lax", secure=is_secure)
            return redirect
        else:
            redirect = RedirectResponse(url="/", status_code=303)
            redirect.set_cookie(key="session_token", value=user.session_token, httponly=True, max_age=cookie_max_age, samesite="lax", secure=is_secure)
            redirect.set_cookie(key="active_profile_id", value=str(profiles[0].id), httponly=False, max_age=cookie_max_age, samesite="lax", secure=is_secure)
            return redirect

    record_failed_attempt(request)
    return RedirectResponse(url="/login?error=1", status_code=303)

class APILoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/login")
async def api_login(request: Request, login_data: APILoginRequest, session: Session = Depends(get_session)):
    check_rate_limit(request)
    user = session.exec(select(User).where(User.username == login_data.username)).first()
    if user and bcrypt.checkpw(login_data.password.encode('utf-8'), user.password_hash.encode('utf-8')):
        clear_rate_limit(request)
        user.session_token = str(uuid.uuid4())
        session.add(user)
        session.commit()
        return {"success": True, "token": user.session_token, "user": {"id": user.id, "username": user.username}}
    record_failed_attempt(request)
    return JSONResponse(status_code=401, content={"success": False, "error": "Invalid credentials"})


@app.api_route("/logout", methods=["GET", "POST"])
async def process_logout(response: Response, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if current_user:
        current_user.session_token = None
        session.add(current_user)
        session.commit()
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie("session_token")
    redirect.delete_cookie("active_profile_id")
    return redirect

@app.post("/forgot_password")
async def request_password_reset(request: Request, username: str = Form(...), session: Session = Depends(get_session)):
    client_ip = get_client_ip(request)
    rate_key = f"forgot_pw_{client_ip}"
    check_rate_limit(rate_key, max_attempts=5, window_seconds=300, error_message="Too many password reset requests. Please wait a few minutes.")
    record_failed_attempt(rate_key)
    user = session.exec(select(User).where(User.username == username)).first()
    if user and not user.is_admin:
        # Check if there's already a pending request
        existing = session.exec(select(PasswordResetRequest).where(PasswordResetRequest.user_id == user.id, PasswordResetRequest.status == "pending")).first()
        if not existing:
            reset_req = PasswordResetRequest(user_id=user.id)
            session.add(reset_req)
            session.commit()
    # Always redirect with success to avoid username enumeration
    return RedirectResponse(url="/login?reset=1", status_code=303)

@app.get("/reset_password/{token}", response_class=HTMLResponse)
async def view_reset_password(request: Request, token: str, session: Session = Depends(get_session)):
    reset_req = session.exec(select(PasswordResetRequest).where(PasswordResetRequest.token == token, PasswordResetRequest.status == "approved")).first()
    if not reset_req:
        return RedirectResponse(url="/login?reset_error=1", status_code=303)
    user = session.get(User, reset_req.user_id)
    return templates.TemplateResponse(request=request, name="reset_password.html", context={"token": token, "username": user.username if user else ""})

@app.post("/reset_password/{token}")
async def process_reset_password(request: Request, token: str, password: str = Form(...), confirm_password: Optional[str] = Form(None), session: Session = Depends(get_session)):
    client_ip = get_client_ip(request)
    rate_key = f"reset_pw_{client_ip}"
    check_rate_limit(rate_key, max_attempts=5, window_seconds=60, error_message="Too many attempts. Please try again later.")
    if confirm_password is not None and password != confirm_password:
        return RedirectResponse(url=f"/reset_password/{token}?error=passwords_mismatch", status_code=303)
    reset_req = session.exec(select(PasswordResetRequest).where(PasswordResetRequest.token == token, PasswordResetRequest.status == "approved")).first()
    if not reset_req:
        record_failed_attempt(rate_key)
        return RedirectResponse(url="/login?reset_error=1", status_code=303)
    user = session.get(User, reset_req.user_id)
    if user:
        user.password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        user.session_token = None
        session.add(user)
        reset_req.status = "used"
        session.add(reset_req)
        session.commit()
        clear_rate_limit(rate_key)
    return RedirectResponse(url="/login?reset_success=1", status_code=303)

@app.post("/password_reset/approve/{request_id}")
async def approve_password_reset(
    request_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile):
        raise HTTPException(status_code=403, detail="Forbidden")
    reset_req = session.get(PasswordResetRequest, request_id)
    if reset_req and reset_req.status == "pending":
        reset_req.status = "approved"
        session.add(reset_req)
        session.commit()
    return {"success": True}

@app.post("/password_reset/deny/{request_id}")
async def deny_password_reset(
    request_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile):
        raise HTTPException(status_code=403, detail="Forbidden")
    reset_req = session.get(PasswordResetRequest, request_id)
    if reset_req and reset_req.status == "pending":
        reset_req.status = "denied"
        session.add(reset_req)
        session.commit()
    return {"success": True}


@app.post("/setup")
async def process_setup(request: Request, username: str = Form(...), password: str = Form(...), confirm_password: Optional[str] = Form(None), session: Session = Depends(get_session)):
    if session.exec(select(User)).first(): 
        return RedirectResponse(url="/", status_code=303)
    if confirm_password is not None and password != confirm_password:
        return RedirectResponse(url="/login?error=passwords_mismatch", status_code=303)
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    new_user = User(
        username=username,
        password_hash=hashed,
        is_admin=True,
        session_token=str(uuid.uuid4()),
        movies_access=True,
        tv_access=True,
        photos_access=True,
        music_access=True,
        downloads_access=True,
        watch_together_access=True,
        can_manage_profiles=True
    )
    session.add(new_user)
    session.commit()

    primary_profile = Profile(
        user_id=new_user.id,
        name=username,
        avatar_color="#e50914",
        avatar_icon="user",
        is_primary=True,
        is_kids=False,
        rewind_seconds=0
    )
    session.add(primary_profile)
    session.commit()

    is_secure = is_request_secure(request)
    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(key="session_token", value=new_user.session_token, httponly=True, samesite="lax", secure=is_secure)
    redirect.set_cookie(key="active_profile_id", value=str(primary_profile.id), httponly=False, samesite="lax", secure=is_secure)
    return redirect

@app.get("/register", response_class=HTMLResponse)
async def view_register(request: Request, invite: str = ""):
    return templates.TemplateResponse(request=request, name="register.html", context={"invite_code": invite})

@app.post("/register")
async def process_register(request: Request, username: str = Form(...), password: str = Form(...), invite_code: str = Form(...), confirm_password: Optional[str] = Form(None), session: Session = Depends(get_session)):
    check_rate_limit(request)
    if confirm_password is not None and password != confirm_password:
        return RedirectResponse(url=f"/register?invite={invite_code}&error=passwords_mismatch", status_code=303)
    invite = session.exec(select(InviteCode).where(InviteCode.code == invite_code)).first()
    expires = invite.expires_at if invite else None
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if not invite or invite.used or (expires and expires < datetime.now(timezone.utc)):
        record_failed_attempt(request)
        raise HTTPException(status_code=400, detail="Invalid, used, or expired invite code")
    if session.exec(select(User).where(User.username == username.strip())).first():
        return RedirectResponse(url=f"/register?invite={invite_code}&error=username_taken", status_code=303)
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    new_user = User(
        username=username,
        password_hash=hashed,
        is_admin=False,
        session_token=str(uuid.uuid4()),
        movies_access=bool(invite.movies_access),
        tv_access=bool(invite.tv_access),
        photos_access=bool(invite.photos_access),
        music_access=bool(invite.music_access),
        downloads_access=bool(invite.downloads_access),
        watch_together_access=bool(invite.watch_together_access),
        kids_mode=False,
        can_manage_profiles=bool(invite.can_manage_profiles)
    )
    session.add(new_user)
    session.delete(invite)
    session.commit()

    primary_profile = Profile(
        user_id=new_user.id,
        name=username,
        avatar_color="#e50914",
        avatar_icon="user",
        is_primary=True,
        is_kids=False,
        rewind_seconds=0
    )
    session.add(primary_profile)
    session.commit()

    clear_rate_limit(request)
    is_secure = is_request_secure(request)
    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(key="session_token", value=new_user.session_token, httponly=True, samesite="lax", secure=is_secure)
    redirect.set_cookie(key="active_profile_id", value=str(primary_profile.id), httponly=False, samesite="lax", secure=is_secure)
    return redirect

# ----------------- PROFILES & PIN MANAGEMENT -----------------

@app.get("/profiles", response_class=HTMLResponse)
async def view_profiles(
    request: Request,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()
    if not profiles:
        primary = Profile(
            user_id=current_user.id,
            name=current_user.username,
            avatar_color="#e50914",
            avatar_icon="user",
            is_primary=True,
            is_kids=False,
            rewind_seconds=current_user.rewind_seconds or 0
        )
        session.add(primary)
        session.commit()
        session.refresh(primary)
        profiles = [primary]

    active_id = request.cookies.get("active_profile_id")
    is_household_manager = bool(current_profile and current_profile.is_primary)
    can_manage = bool(is_household_manager and (current_user.is_admin or current_user.can_manage_profiles))
    can_manage_self = bool(current_profile and not is_household_manager)
    manage_mode = (request.query_params.get("manage") == "1") and (can_manage or can_manage_self)

    profile_data = []
    for p in profiles:
        can_view_pin = (manage_mode and p.pin_hash and (can_manage or (can_manage_self and current_profile and current_profile.id == p.id)))
        profile_data.append({
            "id": p.id,
            "name": p.name,
            "avatar_color": p.avatar_color,
            "avatar_icon": p.avatar_icon or "user",
            "is_primary": p.is_primary,
            "is_kids": (p.rating_level == 2 if p.rating_level is not None else p.is_kids),
            "rating_level": p.rating_level if p.rating_level is not None else (2 if p.is_kids else 0),
            "has_pin": bool(p.pin_hash),
            "pin_hash": p.pin_hash,
            "pin": (p.pin or "__EXISTING_PIN__") if can_view_pin else "",
            "rewind_seconds": p.rewind_seconds or 0
        })

    return templates.TemplateResponse(request=request, name="profiles.html", context={
        "user": current_user,
        "current_profile": current_profile,
        "profiles": profile_data,
        "can_manage": can_manage,
        "can_manage_self": can_manage_self,
        "manage_mode": manage_mode,
        "active_profile_id": int(active_id) if (active_id and active_id.isdigit()) else None
    })

@app.get("/api/profiles/list")
async def list_profiles(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()
    return [{
        "id": p.id,
        "name": p.name,
        "avatar_color": p.avatar_color,
        "avatar_icon": p.avatar_icon or "user",
        "is_primary": p.is_primary,
        "is_kids": (p.rating_level == 2 if p.rating_level is not None else p.is_kids),
        "rating_level": p.rating_level if p.rating_level is not None else (2 if p.is_kids else 0),
        "has_pin": bool(p.pin_hash),
        "rewind_seconds": p.rewind_seconds or 0
    } for p in profiles]

class ProfileSelectRequest(BaseModel):
    profile_id: int

@app.post("/api/profile/select")
async def select_profile(
    request: Request,
    body: ProfileSelectRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    profile = session.get(Profile, body.profile_id)
    if not profile or profile.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Profile not found")

    if profile.pin_hash:
        unlock_token = request.cookies.get(f"profile_unlocked_{profile.id}")
        expected = make_profile_unlock_token(profile.id, current_user.session_token or "")
        if not unlock_token or not hmac.compare_digest(unlock_token, expected):
            return JSONResponse(status_code=200, content={
                "success": False,
                "requires_pin": True,
                "profile_id": profile.id,
                "name": profile.name,
                "avatar_color": profile.avatar_color,
                "avatar_icon": profile.avatar_icon or "user"
            })

    is_secure = is_request_secure(request)
    resp = JSONResponse(content={"success": True, "redirect": "/"})
    resp.set_cookie(key="active_profile_id", value=str(profile.id), httponly=False, max_age=60*60*24*30, samesite="lax", secure=is_secure)
    return resp

class VerifyPinRequest(BaseModel):
    pin: str

@app.post("/api/profile/{profile_id}/verify-pin")
async def verify_profile_pin(
    request: Request,
    profile_id: int,
    body: VerifyPinRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    client_ip = get_client_ip(request)
    rate_key = f"pin_{current_user.id}_{profile_id}_{client_ip}"
    check_rate_limit(rate_key, max_attempts=5, window_seconds=60, error_message="Too many incorrect PIN attempts. Please wait 1 minute.")

    profile = session.get(Profile, profile_id)
    if not profile or profile.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Profile not found")

    is_secure = is_request_secure(request)
    if not profile.pin_hash:
        clear_rate_limit(rate_key)
        resp = JSONResponse(content={"success": True, "redirect": "/"})
        resp.set_cookie(key="active_profile_id", value=str(profile.id), httponly=False, max_age=60*60*24*30, samesite="lax", secure=is_secure)
        return resp

    pin_clean = body.pin.strip()
    if not validate_pin(pin_clean) or not verify_pin(pin_clean, profile.pin_hash):
        record_failed_attempt(rate_key)
        return JSONResponse(status_code=400, content={"success": False, "error": "Incorrect PIN"})

    clear_rate_limit(rate_key)
    unlock_token = make_profile_unlock_token(profile.id, current_user.session_token or "")
    resp = JSONResponse(content={"success": True, "redirect": "/"})
    resp.set_cookie(key="active_profile_id", value=str(profile.id), httponly=False, max_age=60*60*24*30, samesite="lax", secure=is_secure)
    resp.set_cookie(key=f"profile_unlocked_{profile.id}", value=unlock_token, httponly=True, max_age=60*60*24*7, samesite="lax", secure=is_secure)
    return resp

class ResetPinWithPasswordRequest(BaseModel):
    password: str

@app.post("/api/profile/{profile_id}/reset-pin-with-password")
async def reset_pin_with_password(
    profile_id: int,
    body: ResetPinWithPasswordRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    client_ip = get_client_ip(request)
    rate_key = f"reset_pin_{current_user.id}_{client_ip}"
    check_rate_limit(rate_key, max_attempts=5, window_seconds=60, error_message="Too many failed password attempts. Please wait 1 minute.")

    profile = session.get(Profile, profile_id)
    if not profile or profile.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Profile not found")

    pwd = body.password.strip() if body.password else ""
    if not pwd or not bcrypt.checkpw(pwd.encode('utf-8'), current_user.password_hash.encode('utf-8')):
        record_failed_attempt(rate_key)
        return JSONResponse(status_code=400, content={"success": False, "error": "Incorrect account password"})

    clear_rate_limit(rate_key)
    profile.pin = None
    profile.pin_hash = None
    session.add(profile)
    session.commit()

    is_secure = is_request_secure(request)
    unlock_token = make_profile_unlock_token(profile.id, current_user.session_token or "")
    resp = JSONResponse(content={"success": True, "redirect": "/"})
    resp.set_cookie(key="active_profile_id", value=str(profile.id), httponly=False, max_age=60*60*24*30, samesite="lax", secure=is_secure)
    resp.set_cookie(key=f"profile_unlocked_{profile.id}", value=unlock_token, httponly=True, max_age=60*60*24*7, samesite="lax", secure=is_secure)
    return resp

class CreateProfileRequest(BaseModel):
    name: str
    avatar_color: Optional[str] = "#e50914"
    avatar_icon: Optional[str] = "user"
    rating_level: Optional[int] = 0
    is_kids: Optional[bool] = False
    pin: Optional[str] = None
    rewind_seconds: Optional[int] = 0

@app.post("/api/profile/create")
async def create_profile(
    body: CreateProfileRequest,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    is_household_manager = bool(current_profile and current_profile.is_primary)
    if not is_household_manager or not (current_user.is_admin or current_user.can_manage_profiles):
        raise HTTPException(status_code=403, detail="Only household managers can create profiles.")

    name = body.name.strip()
    if not name or len(name) > 30:
        return JSONResponse(status_code=400, content={"success": False, "error": "Profile name must be 1 to 30 characters."})

    existing_count = session.exec(select(func.count(Profile.id)).where(Profile.user_id == current_user.id)).one()
    if existing_count >= 6:
        return JSONResponse(status_code=400, content={"success": False, "error": "Maximum limit of 6 profiles reached for this account."})

    pin_hash = None
    pin_val = body.pin.strip() if (body.pin and body.pin.strip()) else None
    if pin_val:
        if not validate_pin(pin_val):
            return JSONResponse(status_code=400, content={"success": False, "error": "PIN must be 4 to 15 numeric digits."})
        pin_hash = hash_pin(pin_val)

    r_level = body.rating_level if body.rating_level is not None else (2 if body.is_kids else 0)
    is_kids = (r_level == 2)
    new_profile = Profile(
        user_id=current_user.id,
        name=name,
        avatar_color=body.avatar_color or "#e50914",
        avatar_icon=body.avatar_icon or "user",
        is_primary=False,
        is_kids=is_kids,
        rating_level=r_level,
        pin=pin_val if pin_hash else None,
        pin_hash=pin_hash,
        rewind_seconds=0
    )
    session.add(new_profile)
    session.commit()
    session.refresh(new_profile)

    return {
        "success": True,
        "profile": {
            "id": new_profile.id,
            "name": new_profile.name,
            "avatar_color": new_profile.avatar_color,
            "avatar_icon": new_profile.avatar_icon,
            "is_primary": new_profile.is_primary,
            "is_kids": new_profile.is_kids,
            "rating_level": new_profile.rating_level,
            "has_pin": bool(new_profile.pin_hash),
            "rewind_seconds": new_profile.rewind_seconds
        }
    }

class UpdateProfileRequest(BaseModel):
    name: str
    avatar_color: Optional[str] = "#e50914"
    avatar_icon: Optional[str] = "user"
    rating_level: Optional[int] = None
    is_kids: Optional[bool] = False
    pin: Optional[str] = None
    remove_pin: Optional[bool] = False
    rewind_seconds: Optional[int] = 0

@app.post("/api/profile/{profile_id}/update")
async def update_profile(
    profile_id: int,
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    profile = session.get(Profile, profile_id)
    if not profile or profile.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Profile not found")

    is_household_manager = bool(current_profile and current_profile.is_primary) and (current_user.is_admin or current_user.can_manage_profiles)
    is_self = bool(current_profile and profile.id == current_profile.id)

    if not is_household_manager and not is_self:
        raise HTTPException(status_code=403, detail="You do not have permission to manage this profile.")

    name = body.name.strip()
    if not name or len(name) > 30:
        return JSONResponse(status_code=400, content={"success": False, "error": "Profile name must be 1 to 30 characters."})

    profile.name = name
    profile.avatar_color = body.avatar_color or profile.avatar_color
    profile.avatar_icon = body.avatar_icon or profile.avatar_icon

    # Content rating can ONLY be changed by household managers
    if is_household_manager:
        if body.rating_level is not None:
            profile.rating_level = body.rating_level
            profile.is_kids = (body.rating_level == 2)
        elif body.is_kids is not None:
            profile.is_kids = bool(body.is_kids)
            profile.rating_level = 2 if body.is_kids else 0

    if body.remove_pin or (body.pin is not None and not body.pin.strip()):
        profile.pin = None
        profile.pin_hash = None
    elif body.pin and body.pin.strip():
        pin_val = body.pin.strip()
        if pin_val in ("__EXISTING_PIN__", "••••", "••••••••") or (profile.pin and pin_val == profile.pin):
            pass
        else:
            if not validate_pin(pin_val):
                return JSONResponse(status_code=400, content={"success": False, "error": "PIN must be 4 to 15 numeric digits."})
            profile.pin = pin_val
            profile.pin_hash = hash_pin(pin_val)

    session.add(profile)
    session.commit()
    return {"success": True}

@app.post("/api/profile/{profile_id}/delete")
async def delete_profile(
    profile_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    profile = session.get(Profile, profile_id)
    if not profile or profile.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Profile not found")

    if profile.is_primary:
        return JSONResponse(status_code=400, content={"success": False, "error": "The primary account profile cannot be deleted."})

    is_household_manager = bool(current_profile and current_profile.is_primary)
    if not is_household_manager or not (current_user.is_admin or current_user.can_manage_profiles):
        raise HTTPException(status_code=403, detail="Only household managers can manage profiles.")

    for h in session.exec(select(WatchHistory).where(WatchHistory.profile_id == profile.id)).all():
        session.delete(h)
    for w in session.exec(select(Watchlist).where(Watchlist.profile_id == profile.id)).all():
        session.delete(w)

    session.delete(profile)
    session.commit()
    return {"success": True}

# ----------------- DASHBOARD & MEDIA -----------------

import re
import os

def get_media_resolution_label(probe: dict) -> str:
    if not probe:
        return "HD"
    h = probe.get("height") or 0
    w = probe.get("width") or 0
    if h >= 2100 or w >= 3800:
        return "4K"
    elif h >= 1400 or w >= 2500:
        return "1440p"
    elif h >= 1000 or w >= 1900:
        return "1080p"
    elif h >= 700 or w >= 1200:
        return "720p"
    elif h >= 460:
        return "480p"
    elif h > 0:
        return f"{h}p"
    return "HD"

def format_file_size(size_bytes: int) -> str:
    if not size_bytes:
        return "0 B"
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"

def process_movie_versions(movies: list[Movie]) -> list[Movie]:
    """
    Groups multiple movie editions sharing the same tmdb_id or normalized (title, year)
    into a unified media representation for library and dashboard grids.
    """
    grouped = {}
    for m in movies:
        raw_year = str(m.year or "").split(" • ")[0].strip()
        key = ("tmdb", m.tmdb_id) if m.tmdb_id else ("title_year", m.title.strip().lower(), raw_year)
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(m)

    result = []
    for group in grouped.values():
        primary = group[0]
        if not primary.poster_filename:
            with_p = next((x for x in group if x.poster_filename), None)
            if with_p:
                primary.poster_filename = with_p.poster_filename
        if not primary.backdrop_filename:
            with_b = next((x for x in group if x.backdrop_filename), None)
            if with_b:
                primary.backdrop_filename = with_b.backdrop_filename
        result.append(primary)
    return result

def get_sibling_movie_ids(session: Session, movie: Movie) -> list[int]:
    """Returns all Movie IDs sharing the same TMDB ID or normalized (title, year)."""
    if not movie:
        return []
    raw_year = str(movie.year or "").split(" • ")[0].strip()
    if movie.tmdb_id:
        q = select(Movie.id).where(
            (Movie.tmdb_id == movie.tmdb_id) |
            ((func.lower(Movie.title) == movie.title.strip().lower()) & (Movie.year.like(f"{raw_year}%") if raw_year else True))
        )
    else:
        q = select(Movie.id).where(func.lower(Movie.title) == movie.title.strip().lower())
        if raw_year:
            q = q.where(Movie.year.like(f"{raw_year}%"))
    ids = list(session.exec(q).all())
    if movie.id and movie.id not in ids:
        ids.append(movie.id)
    return ids

def process_tv_shows(shows):
    grouped = {}
    for t in shows:
        title_key = t.title.strip().lower()
        if title_key not in grouped:
            grouped[title_key] = {"item": t, "seasons": set()}
        m = re.search(r'S(\d+)', t.file_path, re.IGNORECASE)
        if m: grouped[title_key]["seasons"].add(int(m.group(1)))
        
    result = []
    for g in grouped.values():
        item = g["item"]
        season_count = len(g["seasons"])
        if season_count > 0:
            season_text = f"{season_count} Season{'s' if season_count > 1 else ''}"
            item.year = f"{item.year} • {season_text}" if item.year else season_text
        result.append(item)
    return result


_enriching_movies = set()
_enriching_tv = set()
_enrich_lock = threading.Lock()

async def async_enrich_movie(movie_id: int):
    """Non-blocking background enrichment of movie logo, cast photos, and metadata."""
    with _enrich_lock:
        if movie_id in _enriching_movies:
            return
        _enriching_movies.add(movie_id)

    try:
        with Session(engine) as session:
            movie = session.get(Movie, movie_id)
            if not movie:
                return

            tmdb_config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
            tmdb_key = tmdb_config.value if tmdb_config and tmdb_config.value else os.getenv("TMDB_API_KEY")
            if not tmdb_key:
                return

            tmdb_id = movie.tmdb_id
            if not tmdb_id and movie.poster_filename:
                m_match = re.search(r'movie_(\d+)_', movie.poster_filename)
                if m_match:
                    tmdb_id = int(m_match.group(1))

            raw_client = httpx.AsyncClient(timeout=10.0) if httpx is not None else None
            if not raw_client:
                return
            client = SafeHTTPClient(raw_client)

            try:
                if not tmdb_id:
                    s_url = "https://api.themoviedb.org/3/search/movie"
                    params = {"query": movie.title, "api_key": tmdb_key}
                    raw_year = (movie.year or "")[:4]
                    if raw_year.isdigit():
                        params["year"] = raw_year
                    s_res = await client.get(s_url, params=params)
                    if s_res.status_code == 200:
                        results = s_res.json().get("results", [])
                        if results:
                            tmdb_id = results[0].get("id")

                if not tmdb_id:
                    if not movie.logo_filename:
                        movie.logo_filename = "none"
                        session.add(movie)
                        session.commit()
                    return

                movie.tmdb_id = tmdb_id

                cast_str, dir_str, content_rating, genres_str, trailer_key, col_id, col_name, col_poster, col_backdrop, logo_filename = await async_fetch_additional_details(client, tmdb_id, "movie", tmdb_key)

                changed = False
                if logo_filename and (not movie.logo_filename or movie.logo_filename == "none"):
                    movie.logo_filename = logo_filename
                    changed = True
                elif not movie.logo_filename:
                    movie.logo_filename = "none"
                    changed = True

                if cast_str and (not movie.cast or movie.cast == "[]" or '"profile_filename": null' in (movie.cast or '')):
                    movie.cast = cast_str
                    changed = True

                if dir_str and not movie.director:
                    movie.director = dir_str
                    changed = True

                if content_rating and not movie.content_rating:
                    movie.content_rating = content_rating
                    changed = True

                if genres_str and not movie.genres:
                    movie.genres = genres_str
                    changed = True

                if trailer_key and not movie.trailer_key:
                    movie.trailer_key = trailer_key
                    changed = True

                if col_name and not movie.collection_name:
                    movie.collection_id = col_id
                    movie.collection_name = col_name
                    movie.collection_poster_filename = col_poster
                    movie.collection_backdrop_filename = col_backdrop
                    changed = True

                if changed:
                    session.add(movie)
                    session.commit()
            finally:
                await raw_client.aclose()
    except Exception as e:
        print(f"[Enrichment] Error enriching movie {movie_id}: {e}")
    finally:
        with _enrich_lock:
            _enriching_movies.discard(movie_id)


async def async_enrich_tv_show(title: str):
    """Non-blocking background enrichment of TV show logo, cast photos, and season posters."""
    with _enrich_lock:
        if title in _enriching_tv:
            return
        _enriching_tv.add(title)

    try:
        with Session(engine) as session:
            eps = session.exec(select(TVShow).where(TVShow.title == title)).all()
            if not eps:
                return
            rep = eps[0]

            tmdb_config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
            tmdb_key = tmdb_config.value if tmdb_config and tmdb_config.value else os.getenv("TMDB_API_KEY")
            if not tmdb_key:
                return

            tmdb_id = None
            for e in eps:
                if e.poster_filename:
                    m_match = re.search(r'tv_(\d+)_', e.poster_filename)
                    if m_match:
                        tmdb_id = int(m_match.group(1))
                        break

            raw_client = httpx.AsyncClient(timeout=10.0) if httpx is not None else None
            if not raw_client:
                return
            client = SafeHTTPClient(raw_client)

            try:
                if not tmdb_id:
                    s_url = "https://api.themoviedb.org/3/search/tv"
                    params = {"query": title, "api_key": tmdb_key}
                    raw_year = (rep.year or "")[:4]
                    if raw_year.isdigit():
                        params["first_air_date_year"] = raw_year
                    s_res = await client.get(s_url, params=params)
                    if s_res.status_code == 200:
                        results = s_res.json().get("results", [])
                        if results:
                            tmdb_id = results[0].get("id")

                if not tmdb_id:
                    if not rep.logo_filename:
                        for e in eps:
                            e.logo_filename = "none"
                            session.add(e)
                        session.commit()
                    return

                # 1. Fetch additional details (cast with photos, director, genres, logo)
                cast_str, dir_str, content_rating, genres_str, _, _, _, _, _, logo_filename = await async_fetch_additional_details(client, tmdb_id, "tv", tmdb_key)

                # 2. Check missing season posters across the seasons of this show
                seasons = set()
                for e in eps:
                    m = re.search(r'S(\d+)', e.file_path, re.IGNORECASE)
                    if m:
                        seasons.add(int(m.group(1)))
                    elif e.season is not None:
                        seasons.add(e.season)

                season_posters = {}
                for s in seasons:
                    has_sp = any(e.season_poster_filename for e in eps if ((re.search(r'S(\d+)', e.file_path, re.IGNORECASE) and int(re.search(r'S(\d+)', e.file_path, re.IGNORECASE).group(1)) == s) or e.season == s))
                    if not has_sp:
                        try:
                            s_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{s}?api_key={tmdb_key}"
                            s_res = await client.get(s_url, timeout=10.0)
                            if s_res.status_code == 200:
                                s_data = s_res.json()
                                if s_data.get("poster_path"):
                                    sp_fn = await async_download_image(client, s_data["poster_path"], f"tv_s{s}", tmdb_id, "poster")
                                    if sp_fn:
                                        season_posters[s] = sp_fn
                        except Exception:
                            pass

                # 3. Update all episodes
                for e in eps:
                    if logo_filename and (not e.logo_filename or e.logo_filename == "none"):
                        e.logo_filename = logo_filename
                    elif not e.logo_filename:
                        e.logo_filename = "none"

                    if cast_str and (not e.cast or e.cast == "[]" or '"profile_filename": null' in (e.cast or '')):
                        e.cast = cast_str

                    if dir_str and not e.director:
                        e.director = dir_str

                    if content_rating and not e.content_rating:
                        e.content_rating = content_rating

                    if genres_str and not e.genres:
                        e.genres = genres_str

                    m = re.search(r'S(\d+)', e.file_path, re.IGNORECASE)
                    s_num = int(m.group(1)) if m else e.season
                    if s_num in season_posters and not e.season_poster_filename:
                        e.season_poster_filename = season_posters[s_num]

                    session.add(e)

                session.commit()
            finally:
                await raw_client.aclose()
    except Exception as e:
        print(f"[Enrichment] Error enriching TV show {title}: {e}")
    finally:
        with _enrich_lock:
            _enriching_tv.discard(title)

def get_related_movies(session: Session, movie: Movie, rating_level: int = 0, limit: int = 15) -> list[Movie]:
    """
    Returns up to limit related movies based on collection, genres, director, and cast,
    filtered by parental rating and deduplicated across editions/versions.
    """
    sibling_ids = set(get_sibling_movie_ids(session, movie))
    if movie.id:
        sibling_ids.add(movie.id)

    q = select(Movie).where(~Movie.id.in_(sibling_ids))
    if rating_level == 1:
        q = q.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
    elif rating_level >= 2:
        q = q.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))

    raw_candidates = session.exec(q).all()
    unique_candidates = process_movie_versions(raw_candidates)

    cur_genres = set(g.strip().lower() for g in (movie.genres or '').split(',') if g.strip())
    cur_collection = (movie.collection_name or '').strip().lower()
    cur_director = (movie.director or '').strip().lower()

    cur_cast = set()
    if movie.cast:
        try:
            if movie.cast.startswith('['):
                import json
                for a in json.loads(movie.cast):
                    if isinstance(a, dict) and a.get('name'):
                        cur_cast.add(a['name'].strip().lower())
                    elif isinstance(a, str):
                        cur_cast.add(a.strip().lower())
            else:
                for a in movie.cast.split(','):
                    if a.strip(): cur_cast.add(a.strip().lower())
        except Exception:
            pass

    scored = []
    for cand in unique_candidates:
        score = 0
        cand_genres = set(g.strip().lower() for g in (cand.genres or '').split(',') if g.strip())
        shared_genres = cur_genres & cand_genres
        score += len(shared_genres) * 3

        if cur_collection and cand.collection_name and cand.collection_name.strip().lower() == cur_collection:
            score += 10

        if cur_director and cand.director and cand.director.strip().lower() == cur_director:
            score += 4

        if cur_cast and cand.cast:
            cand_cast = set()
            try:
                if cand.cast.startswith('['):
                    import json
                    for a in json.loads(cand.cast):
                        if isinstance(a, dict) and a.get('name'):
                            cand_cast.add(a['name'].strip().lower())
                        elif isinstance(a, str):
                            cand_cast.add(a.strip().lower())
                else:
                    for a in cand.cast.split(','):
                        if a.strip(): cand_cast.add(a.strip().lower())
            except Exception:
                pass
            score += len(cur_cast & cand_cast)

        scored.append((score, cand.rating or 0, cand.id or 0, cand))

    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return [item[3] for item in scored[:limit]]


def get_related_shows(session: Session, show: TVShow, rating_level: int = 0, limit: int = 15) -> list[TVShow]:
    """
    Returns up to limit related TV shows based on collection, genres, director, and cast,
    filtered by parental rating and grouped into unified series representations.
    """
    title_clean = (show.title or '').strip().lower()

    subq = select(func.min(TVShow.id)).where(func.lower(TVShow.title) != title_clean).group_by(func.lower(TVShow.title))
    q = select(TVShow).where(TVShow.id.in_(subq))
    if rating_level == 1:
        q = q.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
    elif rating_level >= 2:
        q = q.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))

    candidates = session.exec(q).all()
    unique_candidates = process_tv_shows(candidates)

    cur_genres = set(g.strip().lower() for g in (show.genres or '').split(',') if g.strip())
    cur_collection = (show.collection_name or '').strip().lower()
    cur_director = (show.director or '').strip().lower()

    cur_cast = set()
    if show.cast:
        try:
            if show.cast.startswith('['):
                import json
                for a in json.loads(show.cast):
                    if isinstance(a, dict) and a.get('name'):
                        cur_cast.add(a['name'].strip().lower())
                    elif isinstance(a, str):
                        cur_cast.add(a.strip().lower())
            else:
                for a in show.cast.split(','):
                    if a.strip(): cur_cast.add(a.strip().lower())
        except Exception:
            pass

    scored = []
    for cand in unique_candidates:
        score = 0
        cand_genres = set(g.strip().lower() for g in (cand.genres or '').split(',') if g.strip())
        shared_genres = cur_genres & cand_genres
        score += len(shared_genres) * 3

        if cur_collection and cand.collection_name and cand.collection_name.strip().lower() == cur_collection:
            score += 10

        if cur_director and cand.director and cand.director.strip().lower() == cur_director:
            score += 4

        if cur_cast and cand.cast:
            cand_cast = set()
            try:
                if cand.cast.startswith('['):
                    import json
                    for a in json.loads(cand.cast):
                        if isinstance(a, dict) and a.get('name'):
                            cand_cast.add(a['name'].strip().lower())
                        elif isinstance(a, str):
                            cand_cast.add(a.strip().lower())
                else:
                    for a in cand.cast.split(','):
                        if a.strip(): cand_cast.add(a.strip().lower())
            except Exception:
                pass
            score += len(cur_cast & cand_cast)

        scored.append((score, cand.rating or 0, cand.id or 0, cand))

    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return [item[3] for item in scored[:limit]]


def get_tv_episode_sort_key(t: TVShow) -> tuple[int, int, int]:
    m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', t.file_path, re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(2)), t.id or 0)
    s = t.season if t.season is not None else 0
    e = t.episode if t.episode is not None else 0
    if s > 0 or e > 0:
        return (s, e, t.id or 0)
    return (0, 0, t.id or 0)

def get_next_uncompleted_episode(
    session: Session,
    current_ep: TVShow,
    user_id: int,
    prof_id: Optional[int] = None
) -> Optional[TVShow]:
    series_episodes = session.exec(
        select(TVShow).where(func.lower(TVShow.title) == current_ep.title.strip().lower())
    ).all()
    if not series_episodes:
        return None
    sorted_eps = sorted(series_episodes, key=get_tv_episode_sort_key)
    cur_idx = -1
    for i, ep in enumerate(sorted_eps):
        if ep.id == current_ep.id:
            cur_idx = i
            break
    if cur_idx == -1 or cur_idx + 1 >= len(sorted_eps):
        return None

    for ep in sorted_eps[cur_idx + 1:]:
        q = select(WatchHistory).where(
            WatchHistory.user_id == user_id,
            WatchHistory.media_type == "tv",
            WatchHistory.item_id == ep.id
        )
        if prof_id:
            q = q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
        h = session.exec(q.order_by(WatchHistory.last_watched.desc())).first()
        if not h or h.progress < 95.0:
            return ep
    return None

def get_recently_watching(
    session: Session,
    current_user: User,
    prof_id: Optional[int],
    rating_level: int = 0,
    is_kids: bool = False,
    is_html: bool = False,
    limit: int = 15
) -> list[dict]:
    r_level = rating_level if rating_level is not None and rating_level > 0 else (2 if is_kids else 0)
    hist_query = select(WatchHistory).where(WatchHistory.user_id == current_user.id)
    if prof_id:
        hist_query = hist_query.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    # Query a sufficiently large window so that after collapsing series and skipping finished movies,
    # we have up to `limit` items
    history_db = session.exec(hist_query.order_by(WatchHistory.last_watched.desc()).limit(limit * 6)).all()

    history = []
    seen_movie_keys = set()
    seen_tv_shows = set()

    # Batch fetch movies and TV shows for history records to avoid N+1 queries
    hist_m_ids = [h.item_id for h in history_db if h.media_type == 'movie']
    hist_t_ids = [h.item_id for h in history_db if h.media_type == 'tv']
    movies_by_id = {m.id: m for m in session.exec(select(Movie).where(Movie.id.in_(hist_m_ids))).all()} if hist_m_ids else {}
    tv_by_id = {t.id: t for t in session.exec(select(TVShow).where(TVShow.id.in_(hist_t_ids))).all()} if hist_t_ids else {}

    for h in history_db:
        if len(history) >= limit:
            break
        if h.media_type == 'movie' and not current_user.movies_access:
            continue
        if h.media_type == 'tv' and not current_user.tv_access:
            continue

        item_obj = movies_by_id.get(h.item_id) if h.media_type == 'movie' else tv_by_id.get(h.item_id)
        if not item_obj:
            continue

        if r_level > 0 and not is_rating_allowed(getattr(item_obj, 'content_rating', ''), r_level):
            continue

        if h.media_type == 'movie':
            if h.progress >= 95.0:
                continue
            raw_year = str(getattr(item_obj, "year", "") or "").split(" • ")[0].strip()
            m_key = ("tmdb", item_obj.tmdb_id) if item_obj.tmdb_id else ("title_year", item_obj.title.strip().lower(), raw_year)
            if m_key in seen_movie_keys:
                continue
            seen_movie_keys.add(m_key)
            item = {
                "id": item_obj.id,
                "title": item_obj.title,
                "poster_filename": item_obj.poster_filename,
                "file_path": item_obj.file_path,
                "year": getattr(item_obj, "year", "")
            }
            history.append({"item": item, "media_type": "movie", "progress": h.progress})

        elif h.media_type == 'tv':
            tv_show_key = item_obj.title.strip().lower()
            if tv_show_key in seen_tv_shows:
                continue
            seen_tv_shows.add(tv_show_key)

            display_item = item_obj
            display_progress = h.progress

            if h.progress >= 95.0:
                next_ep = get_next_uncompleted_episode(session, item_obj, current_user.id, prof_id)
                if not next_ep:
                    continue
                if r_level > 0 and not is_rating_allowed(getattr(next_ep, 'content_rating', getattr(item_obj, 'content_rating', '')), r_level):
                    continue
                display_item = next_ep
                q_n = select(WatchHistory).where(
                    WatchHistory.user_id == current_user.id,
                    WatchHistory.media_type == "tv",
                    WatchHistory.item_id == next_ep.id
                )
                if prof_id:
                    q_n = q_n.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
                n_hist = session.exec(q_n.order_by(WatchHistory.last_watched.desc())).first()
                display_progress = n_hist.progress if n_hist else 0.0

            item = {
                "id": display_item.id,
                "title": display_item.title,
                "poster_filename": display_item.poster_filename,
                "file_path": display_item.file_path,
                "year": getattr(display_item, "year", "")
            }

            m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', item['file_path'])
            if m:
                s_num = int(m.group(1))
                e_num = int(m.group(2))
                if is_html:
                    enc_title = urllib.parse.quote(display_item.title)
                    s_link = f'<a href="/series/{enc_title}?season={s_num}">S{s_num}</a>'
                    e_link = f'<a href="/series/{enc_title}?ep={display_item.id}">E{e_num}</a>'
                    base = f"{s_link} • {e_link}"
                    filename = os.path.splitext(os.path.basename(item['file_path']))[0]
                    name_match = re.search(r'[Ss]\d+\s*[-_]?\s*[Ee]?\d+[ \-]*(.+)', filename, re.IGNORECASE)
                    if name_match and name_match.group(1).strip():
                        ep_title = name_match.group(1).strip()
                        ep_link = f'<a href="/series/{enc_title}?ep={display_item.id}">{ep_title}</a>'
                        item['year'] = f"{base}<br>{ep_link}"
                    else:
                        item['year'] = base
                else:
                    item['year'] = f"S{s_num} E{e_num}"

            history.append({"item": item, "media_type": "tv", "progress": display_progress})

    return history

COLLECTION_FRANCHISE_DEFINITIONS = [
    {
        "name": "Marvel Cinematic Universe",
        "keywords": [
            "iron man", "thor", "captain america", "avengers", "guardians of the galaxy",
            "ant-man", "doctor strange", "spider-man", "black panther", "captain marvel",
            "black widow", "shang-chi", "eternals", "deadpool & wolverine", "deadpool 3",
            "the incredible hulk", "hulk"
        ]
    },
    {
        "name": "Star Wars Saga",
        "keywords": [
            "star wars", "the empire strikes back", "return of the jedi", "the phantom menace",
            "attack of the clones", "revenge of the sith", "the force awakens", "the last jedi",
            "the rise of skywalker", "rogue one", "solo: a star wars story"
        ]
    },
    {
        "name": "Wizarding World",
        "keywords": ["harry potter", "fantastic beasts"]
    },
    {
        "name": "Middle-earth",
        "keywords": [
            "lord of the rings", "the hobbit", "the fellowship of the ring",
            "the two towers", "the return of the king"
        ]
    },
    {
        "name": "The Dark Knight / Batman",
        "keywords": ["batman", "the dark knight"]
    },
    {
        "name": "James Bond (007)",
        "keywords": [
            "007", "james bond", "casino royale", "quantum of solace",
            "skyfall", "spectre", "no time to die", "goldeneye", "goldfinger"
        ]
    },
    {
        "name": "Fast & Furious",
        "keywords": [
            "fast & furious", "the fast and the furious", "2 fast 2 furious",
            "fast five", "fast & furious 6", "furious 7", "the fate of the furious",
            "f9", "fast x", "hobbs & shaw"
        ]
    },
    {
        "name": "Jurassic Park",
        "keywords": [
            "jurassic park", "the lost world: jurassic park", "jurassic park iii",
            "jurassic world", "fallen kingdom", "jurassic world dominion"
        ]
    },
    {
        "name": "Toy Story",
        "keywords": ["toy story", "lightyear"]
    },
    {
        "name": "Shrek Universe",
        "keywords": ["shrek", "puss in boots"]
    },
    {
        "name": "Mission: Impossible",
        "keywords": ["mission: impossible", "mission impossible"]
    },
    {
        "name": "Pirates of the Caribbean",
        "keywords": ["pirates of the caribbean"]
    },
    {
        "name": "Indiana Jones",
        "keywords": [
            "indiana jones", "raiders of the lost ark", "temple of doom",
            "the last crusade", "kingdom of the crystal skull", "dial of destiny"
        ]
    },
    {
        "name": "The Matrix",
        "keywords": ["the matrix", "matrix reloaded", "matrix revolutions", "matrix resurrections"]
    },
    {
        "name": "John Wick",
        "keywords": ["john wick"]
    },
    {
        "name": "Despicable Me & Minions",
        "keywords": ["despicable me", "minions"]
    },
    {
        "name": "Planet of the Apes",
        "keywords": [
            "planet of the apes", "rise of the planet of the apes",
            "dawn of the planet of the apes", "war for the planet of the apes",
            "kingdom of the planet of the apes"
        ]
    },
    {
        "name": "The Hunger Games",
        "keywords": ["hunger games", "catching fire", "mockingjay", "ballad of songbirds and snakes"]
    },
    {
        "name": "Alien & Predator",
        "keywords": [
            "alien", "aliens", "alien 3", "alien resurrection", "alien: covenant",
            "alien: romulus", "predator", "predators", "the predator", "prey", "alien vs. predator"
        ]
    },
    {
        "name": "Back to the Future",
        "keywords": ["back to the future"]
    },
    {
        "name": "X-Men",
        "keywords": [
            "x-men", "wolverine", "logan", "deadpool", "the new mutants",
            "first class", "days of future past", "apocalypse", "dark phoenix"
        ]
    },
]

def item_matches_collection(item, collection_name: str) -> bool:
    if not item or not getattr(item, "title", None):
        return False
    title_lower = item.title.strip().lower()
    col_name_lower = collection_name.strip().lower()
    
    # 1. Direct match on item's TMDB collection name
    if getattr(item, "collection_name", None) and item.collection_name.strip().lower() == col_name_lower:
        return True

    # 2. Check predefined franchises
    for franchise in COLLECTION_FRANCHISE_DEFINITIONS:
        if franchise["name"].lower() == col_name_lower:
            for kw in franchise["keywords"]:
                if kw.lower() in title_lower:
                    return True
            return False
            
    # 3. Check prefix-based collection (e.g. "Dune Collection" -> prefix "Dune")
    clean_col = collection_name
    if clean_col.endswith(" Collection"):
        prefix = clean_col[:-11].strip().lower()
    else:
        prefix = clean_col.strip().lower()
        
    if prefix and title_lower.startswith(prefix):
        return True
    return False

def build_categories_data(items) -> list[dict]:
    categories = {}
    for item in items:
        if not getattr(item, "genres", None):
            continue
        genres = [g.strip() for g in item.genres.split(",") if g.strip()]
        for g in genres:
            if g not in categories:
                categories[g] = {
                    "name": g,
                    "count": 0,
                    "backdrop_filename": None,
                    "poster_filename": None,
                }
            categories[g]["count"] += 1
            if not categories[g]["backdrop_filename"] and getattr(item, "backdrop_filename", None):
                categories[g]["backdrop_filename"] = item.backdrop_filename
            if not categories[g]["poster_filename"] and getattr(item, "poster_filename", None):
                categories[g]["poster_filename"] = item.poster_filename
                
    sorted_cats = sorted(categories.values(), key=lambda x: (-x["count"], x["name"].lower()))
    return sorted_cats

def build_collections_data(items) -> list[dict]:
    collections = []
    used_item_ids_by_predefined = set()
    
    # 1. Predefined franchises (Cinematic Universes: MCU, Star Wars, Middle-earth, etc.)
    for franchise in COLLECTION_FRANCHISE_DEFINITIONS:
        matched = []
        for item in items:
            t_lower = item.title.strip().lower()
            if any(kw.lower() in t_lower for kw in franchise["keywords"]):
                matched.append(item)
        if matched:
            matched.sort(key=lambda x: str(getattr(x, "year", "") or ""))
            years = [str(getattr(x, "year", ""))[:4] for x in matched if getattr(x, "year", None)]
            years = [y for y in years if y.isdigit()]
            year_range = ""
            if years:
                min_y, max_y = min(years), max(years)
                year_range = f"{min_y}" if min_y == max_y else f"{min_y} – {max_y}"
            
            poster = None
            backdrop = None
            for it in matched:
                if not poster and getattr(it, "collection_poster_filename", None):
                    poster = it.collection_poster_filename
                if not backdrop and getattr(it, "collection_backdrop_filename", None):
                    backdrop = it.collection_backdrop_filename
            for it in matched:
                if not poster and getattr(it, "poster_filename", None):
                    poster = it.poster_filename
                if not backdrop and getattr(it, "backdrop_filename", None):
                    backdrop = it.backdrop_filename
                    
            collections.append({
                "name": franchise["name"],
                "count": len(matched),
                "poster_filename": poster,
                "backdrop_filename": backdrop,
                "year_range": year_range,
                "items": matched
            })
            for it in matched:
                used_item_ids_by_predefined.add(it.id)

    # 2. Native TMDB Collections (belongs_to_collection)
    tmdb_groups = {}
    for item in items:
        if item.id in used_item_ids_by_predefined:
            continue
        c_name = getattr(item, "collection_name", None)
        if c_name and c_name.strip():
            tmdb_groups.setdefault(c_name.strip(), []).append(item)

    for col_name, matched in tmdb_groups.items():
        matched.sort(key=lambda x: str(getattr(x, "year", "") or ""))
        years = [str(getattr(x, "year", ""))[:4] for x in matched if getattr(x, "year", None)]
        years = [y for y in years if y.isdigit()]
        year_range = ""
        if years:
            min_y, max_y = min(years), max(years)
            year_range = f"{min_y}" if min_y == max_y else f"{min_y} – {max_y}"

        poster = None
        backdrop = None
        for it in matched:
            if not poster and getattr(it, "collection_poster_filename", None):
                poster = it.collection_poster_filename
            if not backdrop and getattr(it, "collection_backdrop_filename", None):
                backdrop = it.collection_backdrop_filename
        for it in matched:
            if not poster and getattr(it, "poster_filename", None):
                poster = it.poster_filename
            if not backdrop and getattr(it, "backdrop_filename", None):
                backdrop = it.backdrop_filename

        collections.append({
            "name": col_name,
            "count": len(matched),
            "poster_filename": poster,
            "backdrop_filename": backdrop,
            "year_range": year_range,
            "items": matched
        })
        for it in matched:
            used_item_ids_by_predefined.add(it.id)
                
    # 3. Dynamic prefix-based collections (e.g. "Dune: Part One", "Dune: Part Two")
    prefix_groups = {}
    for item in items:
        if item.id in used_item_ids_by_predefined:
            continue
        title = item.title.strip()
        sep = None
        if ":" in title:
            sep = ":"
        elif " - " in title:
            sep = " - "
        if sep:
            prefix = title.split(sep)[0].strip()
            if len(prefix) >= 3:
                prefix_groups.setdefault(prefix, []).append(item)
                
    for prefix, matched in prefix_groups.items():
        if len(matched) >= 2:
            matched.sort(key=lambda x: str(getattr(x, "year", "") or ""))
            years = [str(getattr(x, "year", ""))[:4] for x in matched if getattr(x, "year", None)]
            years = [y for y in years if y.isdigit()]
            year_range = ""
            if years:
                min_y, max_y = min(years), max(years)
                year_range = f"{min_y}" if min_y == max_y else f"{min_y} – {max_y}"
            
            poster = None
            backdrop = None
            for it in matched:
                if not poster and getattr(it, "poster_filename", None):
                    poster = it.poster_filename
                if not backdrop and getattr(it, "backdrop_filename", None):
                    backdrop = it.backdrop_filename
                    
            collections.append({
                "name": f"{prefix} Collection",
                "count": len(matched),
                "poster_filename": poster,
                "backdrop_filename": backdrop,
                "year_range": year_range,
                "items": matched
            })
            
    collections.sort(key=lambda x: x["name"].lower())
    return collections

def get_release_year(item) -> int:
    raw = str(getattr(item, "year", "") or "").split(" • ")[0].strip()
    digits = "".join([c for c in raw if c.isdigit()])
    return int(digits[:4]) if len(digits) >= 4 else 0

def get_user_watched_status(session: Session, user_id: int, prof_id: Optional[int] = None):
    from sqlmodel import or_
    hist_q = select(WatchHistory).where(
        WatchHistory.user_id == user_id,
        WatchHistory.progress >= 90.0
    )
    if prof_id:
        hist_q = hist_q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    watched_records = session.exec(hist_q).all()
    
    watched_movie_ids = {h.item_id for h in watched_records if h.media_type == 'movie'}
    if watched_movie_ids:
        all_watched_movies = session.exec(select(Movie.id, Movie.tmdb_id, Movie.title).where(Movie.id.in_(list(watched_movie_ids)))).all()
        tmdb_ids = {m[1] for m in all_watched_movies if m[1]}
        titles = {m[2].strip().lower() for m in all_watched_movies if m[2]}
        conditions = []
        if tmdb_ids:
            conditions.append(Movie.tmdb_id.in_(list(tmdb_ids)))
        if titles:
            conditions.append(func.lower(Movie.title).in_(list(titles)))
        if conditions:
            sibling_ids = session.exec(select(Movie.id).where(or_(*conditions))).all()
            watched_movie_ids.update(sibling_ids)

    watched_tv_episode_ids = {h.item_id for h in watched_records if h.media_type == 'tv'}
    watched_tv_titles = set()
    if watched_tv_episode_ids:
        cand_titles = session.exec(
            select(TVShow.title).where(TVShow.id.in_(list(watched_tv_episode_ids))).distinct()
        ).all()
        if cand_titles:
            cand_episodes = session.exec(
                select(TVShow.id, TVShow.title).where(TVShow.title.in_(cand_titles))
            ).all()
            show_episodes_map = defaultdict(set)
            for ep_id, ep_title in cand_episodes:
                show_episodes_map[ep_title.strip().lower()].add(ep_id)
            for title_key, ep_ids in show_episodes_map.items():
                if ep_ids and ep_ids.issubset(watched_tv_episode_ids):
                    watched_tv_titles.add(title_key)
            
    return watched_movie_ids, watched_tv_episode_ids, watched_tv_titles

def get_available_volumes(session: Session) -> dict[str, bool]:
    volumes = session.exec(select(VolumePath)).all()
    has_movie_vol = any(v.media_type == "movie" and os.path.exists(v.path.strip()) for v in volumes)
    has_tv_vol = any(v.media_type == "tv" and os.path.exists(v.path.strip()) for v in volumes)
    has_photo_vol = any(v.media_type == "photo" and os.path.exists(v.path.strip()) for v in volumes)
    has_music_vol = any(v.media_type == "music" and os.path.exists(v.path.strip()) for v in volumes)
    return {
        "movie": has_movie_vol or (session.exec(select(Movie.id)).first() is not None),
        "tv": has_tv_vol or (session.exec(select(TVShow.id)).first() is not None),
        "photo": has_photo_vol or (session.exec(select(Photo.id)).first() is not None),
        "music": has_music_vol or (session.exec(select(Track.id)).first() is not None),
    }

@app.get("/", response_class=HTMLResponse)
def root(
    request: Request,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not session.exec(select(User)).first():
        return templates.TemplateResponse(request=request, name="setup.html")
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    rating_level = get_profile_rating_level(current_user, current_profile)
    prof_id = current_profile.id if current_profile else None

    # High performance: SQL-level filtering and pagination instead of loading the entire library
    if current_user.movies_access:
        m_q = select(Movie)
        if rating_level == 1:
            m_q = m_q.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            m_q = m_q.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
        raw_recent_movies = session.exec(m_q.order_by(Movie.id.desc()).limit(45)).all()
        recent_movies = process_movie_versions(raw_recent_movies)[:15]
    else:
        recent_movies = []

    if current_user.tv_access:
        t_q = select(TVShow)
        if rating_level == 1:
            t_q = t_q.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            t_q = t_q.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
        tv_shows_raw = session.exec(t_q.order_by(TVShow.id.desc()).limit(80)).all()
    else:
        tv_shows_raw = []

    if current_user.photos_access:
        photo_q = select(Photo).where(~Photo.file_path.like("%#recycle%")).order_by(Photo.id.desc()).limit(15)
        recent_photos = session.exec(photo_q).all()
    else:
        recent_photos = []

    recent_albums = []
    if current_user.music_access:
        try:
            recent_albums = session.exec(select(Album).order_by(Album.id.desc()).limit(15)).all()
        except Exception:
            recent_albums = []
    
    # Format raw episodes for the dashboard and group adjacent ones
    import os
    recent_tv_groups = []
    for t_obj in tv_shows_raw:
        if not recent_tv_groups:
            recent_tv_groups.append({"item": t_obj, "count": 1})
        elif recent_tv_groups[-1]["item"].title == t_obj.title:
            recent_tv_groups[-1]["count"] += 1
        else:
            if len(recent_tv_groups) >= 15:
                break
            recent_tv_groups.append({"item": t_obj, "count": 1})
            
    recent_tv = []
    for g in recent_tv_groups[:15]:
        t_obj = g["item"]
        count = g["count"]
        t = {
            "id": t_obj.id,
            "title": t_obj.title,
            "year": t_obj.year,
            "poster_filename": t_obj.poster_filename,
            "file_path": t_obj.file_path,
            "episode_name": "",
            "media_type": "tv",
            "item": t_obj
        }
        if count > 1:
            t['year'] = f"{count} Episodes"
        else:
            m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', t['file_path'])
            if m:
                s_num = int(m.group(1))
                e_num = int(m.group(2))
                enc_title = urllib.parse.quote(t_obj.title)
                s_link = f'<a href="/series/{enc_title}?season={s_num}">S{s_num}</a>'
                e_link = f'<a href="/series/{enc_title}?ep={t_obj.id}">E{e_num}</a>'
                base = f"{s_link} • {e_link}"
                filename = os.path.splitext(os.path.basename(t['file_path']))[0]
                name_match = re.search(r'[Ss]\d+\s*[-_]?\s*[Ee]?\d+[ \-]*(.+)', filename, re.IGNORECASE)
                if name_match and name_match.group(1).strip():
                    ep_title = name_match.group(1).strip()
                    ep_link = f'<a href="/series/{enc_title}?ep={t_obj.id}">{ep_title}</a>'
                    t['year'] = f"{base}<br>{ep_link}"
                else:
                    t['year'] = base
        recent_tv.append(t)
    
    if prof_id:
        watchlist_db = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))).all()
    else:
        watchlist_db = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id)).all()

    watchlist = []
    if watchlist_db:
        w_m_ids = [w.item_id for w in watchlist_db if w.media_type == 'movie']
        w_t_ids = [w.item_id for w in watchlist_db if w.media_type == 'tv']
        w_movies = {m.id: m for m in session.exec(select(Movie).where(Movie.id.in_(w_m_ids))).all()} if w_m_ids else {}
        w_tv = {t.id: t for t in session.exec(select(TVShow).where(TVShow.id.in_(w_t_ids))).all()} if w_t_ids else {}
        for w in watchlist_db:
            if w.media_type == 'movie' and not current_user.movies_access: continue
            if w.media_type == 'tv' and not current_user.tv_access: continue
            item = w_movies.get(w.item_id) if w.media_type == 'movie' else w_tv.get(w.item_id)
            if item:
                if rating_level > 0 and not is_rating_allowed(getattr(item, 'content_rating', ''), rating_level):
                    continue
                watchlist.append({"db_id": w.id, "item": item, "media_type": w.media_type})

    history = get_recently_watching(session, current_user, prof_id, rating_level=rating_level, is_html=True, limit=15)
    
    user_watchlist_movies = [w.item_id for w in watchlist_db if w.media_type == 'movie']
    user_watchlist_tv = [w.item_id for w in watchlist_db if w.media_type == 'tv']
    
    import random
    from collections import Counter
    from sqlmodel import or_
    full_hist_query = select(WatchHistory).where(WatchHistory.user_id == current_user.id)
    if prof_id:
        full_hist_query = full_hist_query.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    history_db_full = session.exec(full_hist_query.order_by(WatchHistory.last_watched.desc()).limit(100)).all()
    
    hist_m_ids = [h.item_id for h in history_db_full if h.media_type == 'movie']
    hist_t_ids = [h.item_id for h in history_db_full if h.media_type == 'tv']
    h_movies = {m.id: m for m in session.exec(select(Movie).where(Movie.id.in_(hist_m_ids))).all()} if hist_m_ids else {}
    h_tv = {t.id: t for t in session.exec(select(TVShow).where(TVShow.id.in_(hist_t_ids))).all()} if hist_t_ids else {}

    watched_movie_ids = set()
    watched_tv_titles = set()
    genre_counts = Counter()
    for h in history_db_full:
        item_obj = h_movies.get(h.item_id) if h.media_type == 'movie' else h_tv.get(h.item_id)
        if item_obj:
            if h.media_type == 'movie': watched_movie_ids.add(h.item_id)
            else: watched_tv_titles.add(item_obj.title.strip().lower())
            if item_obj.genres:
                for g in item_obj.genres.split(','): genre_counts[g.strip()] += 1
                
    top_genres = [g for g, _ in genre_counts.most_common(3)]
    recommended = []
    if top_genres:
        if current_user.movies_access:
            m_filters = [Movie.genres.like(f"%{g}%") for g in top_genres]
            cand_m_q = select(Movie).where(or_(*m_filters))
            if rating_level == 1:
                cand_m_q = cand_m_q.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                cand_m_q = cand_m_q.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
            if watched_movie_ids:
                cand_m_q = cand_m_q.where(Movie.id.not_in(list(watched_movie_ids)[:500]))
            cand_movies = session.exec(cand_m_q.order_by(Movie.rating.desc().nullslast()).limit(20)).all()
            for m in cand_movies:
                recommended.append({"item": {"id": m.id, "title": m.title, "poster_filename": m.poster_filename, "year": m.year}, "media_type": "movie"})

        if current_user.tv_access:
            tv_filters = [TVShow.genres.like(f"%{g}%") for g in top_genres]
            cand_tv_q = select(TVShow).where(or_(*tv_filters))
            if rating_level == 1:
                cand_tv_q = cand_tv_q.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                cand_tv_q = cand_tv_q.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
            cand_shows_raw = session.exec(cand_tv_q.order_by(TVShow.rating.desc().nullslast()).limit(40)).all()
            cand_shows = process_tv_shows(cand_shows_raw)
            for s in cand_shows:
                if s.title.strip().lower() not in watched_tv_titles:
                    recommended.append({"item": {"id": s.id, "title": s.title, "poster_filename": s.poster_filename, "year": getattr(s, 'year', '')}, "media_type": "tv"})
            
        random.shuffle(recommended)
        recommended = recommended[:15]

    user_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()

    watched_movie_ids, watched_tv_episode_ids, watched_tv_titles = get_user_watched_status(session, current_user.id, prof_id)

    if "application/json" in request.headers.get("Accept", ""):
        return {
            "user": {"id": current_user.id, "username": current_user.username, "is_admin": is_actual_admin(current_user, current_profile), "watch_together_access": current_user.watch_together_access, "downloads_access": current_user.downloads_access},
            "current_profile": {
                "id": current_profile.id,
                "name": current_profile.name,
                "avatar_color": current_profile.avatar_color,
                "avatar_icon": current_profile.avatar_icon,
                "is_kids": (current_profile.rating_level == 2 if getattr(current_profile, 'rating_level', None) is not None else current_profile.is_kids),
                "rating_level": getattr(current_profile, 'rating_level', 2 if current_profile.is_kids else 0)
            } if current_profile else None,
            "recent_movies": recent_movies,
            "recent_tv": recent_tv,
            "recent_photos": recent_photos,
            "watchlist": watchlist,
            "history": history,
            "recommended": recommended,
            "user_watchlist_movies": user_watchlist_movies,
            "user_watchlist_tv": user_watchlist_tv,
            "watched_movie_ids": list(watched_movie_ids),
            "watched_tv_episode_ids": list(watched_tv_episode_ids),
            "watched_tv_titles": list(watched_tv_titles)
        }

    avail_vols = get_available_volumes(session)
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "user": current_user, "current_profile": current_profile, "user_profiles": user_profiles,
        "recent_movies": recent_movies, "recent_tv": recent_tv, "recent_photos": recent_photos, "recent_albums": recent_albums, "watchlist": watchlist, "history": history,
        "recommended": recommended,
        "user_watchlist_movies": user_watchlist_movies, "user_watchlist_tv": user_watchlist_tv,
        "watched_movie_ids": watched_movie_ids, "watched_tv_episode_ids": watched_tv_episode_ids, "watched_tv_titles": watched_tv_titles,
        "has_movies": avail_vols["movie"], "has_tv": avail_vols["tv"], "has_photos": avail_vols["photo"], "has_music": avail_vols["music"]
    })

@app.get("/api/library/{media_type}")
def api_library(
    request: Request, media_type: str, page: int = 1, limit: int = 50, 
    search: str = "", sort: str = "title_asc",
    genre: Optional[str] = None,
    collection: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    rating_level = get_profile_rating_level(current_user, current_profile)
    prof_id = current_profile.id if current_profile else None
    
    if media_type == "movie":
        if not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
        
        # Deduplicate editions at SQL level via grouping by tmdb_id or normalized title
        subq = select(func.min(Movie.id)).group_by(func.coalesce(Movie.tmdb_id, func.lower(Movie.title)))
        query = select(Movie).where(Movie.id.in_(subq))
        
        if rating_level == 1:
            query = query.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            query = query.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
        if search:
            query = query.where(Movie.title.ilike(f"%{search}%"))
        if genre:
            query = query.where(Movie.genres.ilike(f"%{genre.strip()}%"))
        if collection:
            query = query.where((Movie.collection_name.ilike(f"%{collection.strip()}%")) | (Movie.title.ilike(f"%{collection.strip()}%")))

        if sort == "title_asc": query = query.order_by(Movie.title.asc())
        elif sort == "title_desc": query = query.order_by(Movie.title.desc())
        elif sort in ("rating_desc", "rating"): query = query.order_by(Movie.rating.desc().nullslast(), Movie.title.asc())
        elif sort == "rating_asc": query = query.order_by(Movie.rating.asc().nullslast(), Movie.title.asc())
        elif sort == "year_desc": query = query.order_by(Movie.year.desc(), Movie.title.asc())
        elif sort == "year_asc": query = query.order_by(Movie.year.asc(), Movie.title.asc())
        elif sort == "date_desc": query = query.order_by(Movie.id.desc())
        elif sort == "date_asc": query = query.order_by(Movie.id.asc())

        items = session.exec(query.offset((page - 1) * limit).limit(limit)).all()
    elif media_type == "tv":
        if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
        
        # One representative show record per distinct title
        subq = select(func.min(TVShow.id)).group_by(func.lower(TVShow.title))
        query = select(TVShow).where(TVShow.id.in_(subq))
        
        if rating_level == 1:
            query = query.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            query = query.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
        if search:
            query = query.where(TVShow.title.ilike(f"%{search}%"))
        if genre:
            query = query.where(TVShow.genres.ilike(f"%{genre.strip()}%"))
        if collection:
            query = query.where((TVShow.collection_name.ilike(f"%{collection.strip()}%")) | (TVShow.title.ilike(f"%{collection.strip()}%")))
        
        if sort == "title_asc": query = query.order_by(TVShow.title.asc())
        elif sort == "title_desc": query = query.order_by(TVShow.title.desc())
        elif sort in ("rating_desc", "rating"): query = query.order_by(TVShow.rating.desc().nullslast(), TVShow.title.asc())
        elif sort == "rating_asc": query = query.order_by(TVShow.rating.asc().nullslast(), TVShow.title.asc())
        elif sort == "year_desc": query = query.order_by(TVShow.year.desc(), TVShow.title.asc())
        elif sort == "year_asc": query = query.order_by(TVShow.year.asc(), TVShow.title.asc())
        elif sort == "date_desc": query = query.order_by(TVShow.id.desc())
        elif sort == "date_asc": query = query.order_by(TVShow.id.asc())
        
        paged_shows = session.exec(query.offset((page - 1) * limit).limit(limit)).all()
        if paged_shows:
            p_titles = [s.title for s in paged_shows]
            season_counts_res = session.exec(
                select(TVShow.title, func.count(func.distinct(TVShow.season))).where(TVShow.title.in_(p_titles)).group_by(TVShow.title)
            ).all()
            s_counts = {t.lower(): c for t, c in season_counts_res}
            for s in paged_shows:
                sc = s_counts.get(s.title.lower(), 0)
                if sc > 0:
                    season_text = f"{sc} Season{'s' if sc > 1 else ''}"
                    s.year = f"{s.year} • {season_text}" if s.year else season_text
        items = paged_shows
    elif media_type == "photo":
        if not current_user.photos_access: raise HTTPException(status_code=403, detail="Forbidden")
        query = select(Photo)
        if search:
            query = query.where(Photo.title.ilike(f"%{search}%"))
        
        if sort == "title_asc": query = query.order_by(Photo.title.asc())
        elif sort == "title_desc": query = query.order_by(Photo.title.desc())
        elif sort == "date_desc": query = query.order_by(Photo.date_added.desc())
        elif sort == "date_asc": query = query.order_by(Photo.date_added.asc())
        
        items = session.exec(query.offset((page - 1) * limit).limit(limit)).all()
    elif media_type == "music":
        if not current_user.music_access: raise HTTPException(status_code=403, detail="Forbidden")
        view_type = request.query_params.get("view", "albums")
        music_result = []
        try:
            if view_type == "artists":
                q_art = select(Artist)
                if search:
                    q_art = q_art.where(Artist.name.ilike(f"%{search}%"))
                if sort == "title_desc":
                    q_art = q_art.order_by(Artist.name.desc())
                else:
                    q_art = q_art.order_by(Artist.name.asc())
                artists = session.exec(q_art.offset((page - 1) * limit).limit(limit)).all()
                art_ids = [a.id for a in artists]
                album_counts_map = dict(session.exec(select(Album.artist_id, func.count(Album.id)).where(Album.artist_id.in_(art_ids)).group_by(Album.artist_id)).all()) if art_ids else {}
                for art in artists:
                    album_count = album_counts_map.get(art.id, 0)
                    music_result.append({
                        "id": art.id,
                        "title": art.name,
                        "artist_name": art.name,
                        "year": f"{album_count} Albums" if album_count != 1 else "1 Album",
                        "poster_filename": art.thumbnail_filename,
                        "url": f"/artist/{art.id}",
                        "type": "artist"
                    })
                return {"items": music_result}

            elif view_type == "tracks":
                q_trk = select(Track)
                if search:
                    q_trk = q_trk.where((Track.title.ilike(f"%{search}%")) | (Track.artist_name.ilike(f"%{search}%")) | (Track.album_title.ilike(f"%{search}%")))
                if sort == "title_desc":
                    q_trk = q_trk.order_by(Track.title.desc())
                else:
                    q_trk = q_trk.order_by(Track.title.asc())
                tracks = session.exec(q_trk.offset((page - 1) * limit).limit(limit)).all()
                for trk in tracks:
                    mins = int((trk.duration or 0) // 60)
                    secs = int((trk.duration or 0) % 60)
                    music_result.append({
                        "id": trk.id,
                        "title": trk.title,
                        "artist_name": trk.artist_name,
                        "album_title": trk.album_title,
                        "year": f"{mins}:{secs:02d}",
                        "poster_filename": trk.cover_filename,
                        "url": f"/album/{trk.album_id}" if trk.album_id else "#",
                        "type": "track",
                        "duration": trk.duration or 0
                    })
                return {"items": music_result}

            elif view_type == "playlists":
                q_pl = select(Playlist).where(Playlist.user_id == current_user.id).order_by(Playlist.name.asc())
                if search:
                    q_pl = q_pl.where(Playlist.name.ilike(f"%{search}%"))
                playlists = session.exec(q_pl.offset((page - 1) * limit).limit(limit)).all()
                pl_ids = [p.id for p in playlists]
                pl_counts_map = dict(session.exec(select(PlaylistTrack.playlist_id, func.count(PlaylistTrack.id)).where(PlaylistTrack.playlist_id.in_(pl_ids)).group_by(PlaylistTrack.playlist_id)).all()) if pl_ids else {}
                for pl in playlists:
                    track_count = pl_counts_map.get(pl.id, 0)
                    first_trk_id = session.exec(select(PlaylistTrack.track_id).where(PlaylistTrack.playlist_id == pl.id).order_by(PlaylistTrack.track_order.asc())).first()
                    first_cov = None
                    if first_trk_id:
                        ft = session.get(Track, first_trk_id)
                        if ft: first_cov = ft.cover_filename
                    music_result.append({
                        "id": pl.id,
                        "title": pl.name,
                        "artist_name": "Playlist",
                        "year": f"{track_count} Tracks",
                        "poster_filename": first_cov,
                        "url": f"/playlist/{pl.id}",
                        "type": "playlist"
                    })
                return {"items": music_result}

            else: # Albums (default)
                q_alb = select(Album)
                if search:
                    q_alb = q_alb.where((Album.title.ilike(f"%{search}%")) | (Album.artist_name.ilike(f"%{search}%")))
                if sort == "title_desc":
                    q_alb = q_alb.order_by(Album.title.desc())
                elif sort == "year_desc":
                    q_alb = q_alb.order_by(Album.year.desc())
                elif sort == "year_asc":
                    q_alb = q_alb.order_by(Album.year.asc())
                else:
                    q_alb = q_alb.order_by(Album.title.asc())
                albums = session.exec(q_alb.offset((page - 1) * limit).limit(limit)).all()
                for alb in albums:
                    music_result.append({
                        "id": alb.id,
                        "title": alb.title,
                        "artist_name": alb.artist_name,
                        "year": alb.year or (alb.artist_name if alb.artist_name else ""),
                        "poster_filename": alb.cover_filename,
                        "url": f"/album/{alb.id}",
                        "type": "album"
                    })
                return {"items": music_result}
        except Exception:
            return {"items": []}
    else:
        raise HTTPException(status_code=404, detail="Not Found")
        
    if prof_id:
        watchlist_db = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None)).where(Watchlist.media_type == media_type)).all()
    else:
        watchlist_db = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.media_type == media_type)).all()
    user_watchlist = {w.item_id for w in watchlist_db}
    
    import urllib.parse
    result = []
    
    if media_type == "photo":
        raw_album = request.query_params.get("album")
        current_album = raw_album.strip().strip("/").replace("\\", "/") if raw_album else None
        if current_album == "":
            current_album = None

        all_items = []
        subfolder_items = []
        direct_photo_items = []

        # 1. Discover immediate subfolders
        all_albums = session.exec(select(Photo.album).where(Photo.album != None).distinct()).all()
        seen_subfolders = {}  # immediate_sub_name -> full_sub_path

        prefix = (current_album + "/") if current_album else ""
        for alb in all_albums:
            if not alb or is_recycle_name(alb) or is_recycle_path(alb):
                continue
            norm_alb = alb.strip().replace("\\", "/").strip("/")
            if current_album:
                if norm_alb.lower().startswith(prefix.lower()):
                    remainder = norm_alb[len(prefix):]
                    sub_name = remainder.split("/")[0]
                    full_sub = f"{current_album}/{sub_name}"
                    seen_subfolders[sub_name] = full_sub
            else:
                sub_name = norm_alb.split("/")[0]
                seen_subfolders[sub_name] = sub_name

        for sub_name, full_sub in seen_subfolders.items():
            if search and search.lower() not in sub_name.lower():
                continue
            # Find cover photo (most recent photo in this subfolder or any of its descendants)
            cover_photo = session.exec(
                select(Photo)
                .where((Photo.album == full_sub) | (Photo.album.like(f"{full_sub}/%")))
                .order_by(func.coalesce(Photo.date_taken, Photo.date_added).desc())
            ).first()
            if cover_photo:
                cov_date = cover_photo.date_taken or cover_photo.date_added or datetime.min
                subfolder_items.append({
                    "id": full_sub,
                    "title": sub_name,
                    "type": "album",
                    "year": "",
                    "poster_filename": cover_photo.poster_filename,
                    "album": full_sub,
                    "url": f"/library/photo?album={urllib.parse.quote(full_sub)}",
                    "in_watchlist": False,
                    "date": cov_date
                })

        # 2. Direct photos in current_album (or all photos matching search if searching from root)
        if search and not current_album:
            query = select(Photo).where(Photo.title.ilike(f"%{search}%"))
        elif search and current_album:
            query = select(Photo).where(
                ((Photo.album == current_album) | (Photo.album.like(f"{current_album}/%"))),
                Photo.title.ilike(f"%{search}%")
            )
        elif current_album:
            query = select(Photo).where(Photo.album == current_album)
        else:
            query = select(Photo).where(Photo.album == None)

        photos = session.exec(query).all()
        for item in photos:
            if item.file_path and is_recycle_path(item.file_path):
                continue
            disp_date = item.date_taken or item.date_added
            direct_photo_items.append({
                "id": item.id,
                "title": item.title,
                "type": "photo",
                "year": disp_date.strftime("%b %d, %Y") if disp_date else None,
                "poster_filename": item.poster_filename,
                "album": item.album,
                "url": f"/photo/{item.id}",
                "in_watchlist": False,
                "date": disp_date or datetime.min
            })

        # 3. Sort subfolders and direct photos
        if sort == "title_asc":
            subfolder_items.sort(key=lambda x: x["title"].lower())
            direct_photo_items.sort(key=lambda x: x["title"].lower())
        elif sort == "title_desc":
            subfolder_items.sort(key=lambda x: x["title"].lower(), reverse=True)
            direct_photo_items.sort(key=lambda x: x["title"].lower(), reverse=True)
        elif sort in ("year_desc", "date_desc"):
            subfolder_items.sort(key=lambda x: x["date"], reverse=True)
            direct_photo_items.sort(key=lambda x: x["date"], reverse=True)
        elif sort in ("year_asc", "date_asc"):
            subfolder_items.sort(key=lambda x: x["date"])
            direct_photo_items.sort(key=lambda x: x["date"])
        else:
            subfolder_items.sort(key=lambda x: x["title"].lower())
            direct_photo_items.sort(key=lambda x: x["title"].lower())

        all_items = subfolder_items + direct_photo_items
        result = all_items[(page - 1) * limit : page * limit]
    else:
        watched_movie_ids, _, watched_tv_titles = get_user_watched_status(session, current_user.id, prof_id)
        for item in items:
            if media_type == "tv":
                url_path = f"/series/{urllib.parse.quote(item.title)}"
                is_w = item.title.strip().lower() in watched_tv_titles
            else:
                url_path = f"/movie/{item.id}"
                is_w = item.id in watched_movie_ids
            
            result.append({
                "id": item.id,
                "title": item.title,
                "year": item.year if hasattr(item, 'year') else None,
                "poster_filename": item.poster_filename if hasattr(item, 'poster_filename') else None,
                "rating": getattr(item, 'rating', None),
                "album": None,
                "url": url_path,
                "in_watchlist": item.id in user_watchlist if hasattr(item, 'id') else False,
                "is_watched": is_w
            })
        
    return {"items": result}

@app.get("/api/me")
async def api_me(current_user: User = Depends(get_current_user)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    return {"id": current_user.id, "username": current_user.username}

@app.get("/api/dashboard")
def api_dashboard(
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    rating_level = get_profile_rating_level(current_user, current_profile)
    prof_id = current_profile.id if current_profile else None
    
    if current_user.movies_access:
        m_q = select(Movie)
        if rating_level == 1:
            m_q = m_q.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            m_q = m_q.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
        raw_movies = session.exec(m_q.order_by(Movie.id.desc()).limit(45)).all()
        recent_movies = [{"id": m.id, "title": m.title, "poster_filename": m.poster_filename, "year": m.year} for m in process_movie_versions(raw_movies)[:15]]
    else:
        recent_movies = []
    
    if current_user.tv_access:
        t_q = select(TVShow)
        if rating_level == 1:
            t_q = t_q.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            t_q = t_q.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
        tv_shows_raw = session.exec(t_q.order_by(TVShow.id.desc()).limit(80)).all()
    else:
        tv_shows_raw = []
    
    import os, re
    recent_tv_groups = []
    for t_obj in tv_shows_raw:
        if not recent_tv_groups:
            recent_tv_groups.append({"item": t_obj, "count": 1})
        elif recent_tv_groups[-1]["item"].title == t_obj.title:
            recent_tv_groups[-1]["count"] += 1
        else:
            if len(recent_tv_groups) >= 15: break
            recent_tv_groups.append({"item": t_obj, "count": 1})
            
    recent_tv = []
    for g in recent_tv_groups[:15]:
        t_obj = g["item"]
        count = g["count"]
        t = {
            "id": t_obj.id,
            "title": t_obj.title,
            "year": t_obj.year,
            "poster_filename": t_obj.poster_filename,
            "file_path": t_obj.file_path,
        }
        if count > 1:
            t['year'] = f"{count} Episodes"
        else:
            m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', t['file_path'])
            if m:
                base = f"S{int(m.group(1))} E{int(m.group(2))}"
                t['year'] = base
        recent_tv.append(t)
        
    if prof_id:
        watchlist_db = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))).all()
    else:
        watchlist_db = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id)).all()

    watchlist = []
    if watchlist_db:
        w_m_ids = [w.item_id for w in watchlist_db if w.media_type == 'movie']
        w_t_ids = [w.item_id for w in watchlist_db if w.media_type == 'tv']
        w_movies = {m.id: m for m in session.exec(select(Movie).where(Movie.id.in_(w_m_ids))).all()} if w_m_ids else {}
        w_tv = {t.id: t for t in session.exec(select(TVShow).where(TVShow.id.in_(w_t_ids))).all()} if w_t_ids else {}
        for w in watchlist_db:
            if w.media_type == 'movie' and not current_user.movies_access: continue
            if w.media_type == 'tv' and not current_user.tv_access: continue
            item_obj = w_movies.get(w.item_id) if w.media_type == 'movie' else w_tv.get(w.item_id)
            if item_obj:
                if rating_level > 0 and not is_rating_allowed(getattr(item_obj, 'content_rating', ''), rating_level):
                    continue
                item = {
                    "id": item_obj.id,
                    "title": item_obj.title,
                    "poster_filename": item_obj.poster_filename,
                    "year": getattr(item_obj, "year", "")
                }
                if w.media_type == 'tv':
                    m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', item_obj.file_path)
                    if m:
                        item['year'] = f"S{int(m.group(1))} E{int(m.group(2))}"
                watchlist.append({"item": item, "media_type": w.media_type})
        
    history = get_recently_watching(session, current_user, prof_id, rating_level=rating_level, is_html=False, limit=15)

    import random
    from collections import Counter
    from sqlmodel import or_
    full_hist_query = select(WatchHistory).where(WatchHistory.user_id == current_user.id)
    if prof_id:
        full_hist_query = full_hist_query.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    history_db_full = session.exec(full_hist_query.order_by(WatchHistory.last_watched.desc()).limit(100)).all()
    
    hist_m_ids = [h.item_id for h in history_db_full if h.media_type == 'movie']
    hist_t_ids = [h.item_id for h in history_db_full if h.media_type == 'tv']
    h_movies = {m.id: m for m in session.exec(select(Movie).where(Movie.id.in_(hist_m_ids))).all()} if hist_m_ids else {}
    h_tv = {t.id: t for t in session.exec(select(TVShow).where(TVShow.id.in_(hist_t_ids))).all()} if hist_t_ids else {}

    watched_movie_ids = set()
    watched_tv_titles = set()
    genre_counts = Counter()
    for h in history_db_full:
        item_obj = h_movies.get(h.item_id) if h.media_type == 'movie' else h_tv.get(h.item_id)
        if item_obj:
            if h.media_type == 'movie': watched_movie_ids.add(h.item_id)
            else: watched_tv_titles.add(item_obj.title.strip().lower())
            if item_obj.genres:
                for g in item_obj.genres.split(','): genre_counts[g.strip()] += 1
                
    top_genres = [g for g, _ in genre_counts.most_common(3)]
    recommended = []
    if top_genres:
        if current_user.movies_access:
            m_filters = [Movie.genres.like(f"%{g}%") for g in top_genres]
            cand_m_q = select(Movie).where(or_(*m_filters))
            if rating_level == 1:
                cand_m_q = cand_m_q.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                cand_m_q = cand_m_q.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
            if watched_movie_ids:
                cand_m_q = cand_m_q.where(Movie.id.not_in(list(watched_movie_ids)[:500]))
            cand_movies = session.exec(cand_m_q.order_by(Movie.rating.desc().nullslast()).limit(20)).all()
            for m in cand_movies:
                recommended.append({"item": {"id": m.id, "title": m.title, "poster_filename": m.poster_filename, "year": m.year}, "media_type": "movie"})

        if current_user.tv_access:
            tv_filters = [TVShow.genres.like(f"%{g}%") for g in top_genres]
            cand_tv_q = select(TVShow).where(or_(*tv_filters))
            if rating_level == 1:
                cand_tv_q = cand_tv_q.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                cand_tv_q = cand_tv_q.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
            cand_shows_raw = session.exec(cand_tv_q.order_by(TVShow.rating.desc().nullslast()).limit(40)).all()
            cand_shows = process_tv_shows(cand_shows_raw)
            for s in cand_shows:
                if s.title.strip().lower() not in watched_tv_titles:
                    recommended.append({"item": {"id": s.id, "title": s.title, "poster_filename": s.poster_filename, "year": getattr(s, 'year', '')}, "media_type": "tv"})
            
        random.shuffle(recommended)
        recommended = recommended[:15]

    if current_user.photos_access:
        photo_q = select(Photo).where(~Photo.file_path.like("%#recycle%")).order_by(Photo.date_added.desc()).limit(15)
        photos = session.exec(photo_q).all()
        recent_photos = [{
            "id": p.id,
            "title": p.title,
            "poster_filename": p.poster_filename,
            "album": p.album,
            "date": (p.date_taken or p.date_added).strftime("%b %d, %Y") if (p.date_taken or p.date_added) else ""
        } for p in photos]
    else:
        recent_photos = []

    return {
        "recent_movies": recent_movies,
        "recent_tv": recent_tv,
        "history": history,
        "watchlist": watchlist,
        "recommended": recommended,
        "recent_photos": recent_photos
    }

@app.get("/api/movie/{movie_id}")
def api_movie(
    movie_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    rating_level = get_profile_rating_level(current_user, current_profile)
    prof_id = current_profile.id if current_profile else None
    
    movie = session.get(Movie, movie_id)
    if not movie: raise HTTPException(status_code=404, detail="Movie not found")
    if rating_level > 0 and not is_rating_allowed(movie.content_rating, rating_level):
        raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")

    if not movie.logo_filename or not movie.cast or '"profile_filename": null' in (movie.cast or ''):
        background_tasks.add_task(async_enrich_movie, movie.id)
        
    cast_list = []
    if movie.cast and movie.cast.startswith("["):
        try:
            import json
            cast_list = json.loads(movie.cast)
        except: pass

    # Non-blocking background enrichment of missing logo, cast photos, and collection info
    if not movie.logo_filename or not movie.cast or '"profile_filename": null' in (movie.cast or '') or (not movie.collection_poster_filename and movie.collection_name):
        background_tasks.add_task(async_enrich_movie, movie.id)
        
    hist_q = select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == movie.id).where(WatchHistory.media_type == 'movie')
    if prof_id:
        hist_q = hist_q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    history = session.exec(hist_q).first()
    has_progress = history.progress > 0 if history and history.progress else False
    
    wl_q = select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.item_id == movie.id).where(Watchlist.media_type == 'movie')
    if prof_id:
        wl_q = wl_q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    in_watchlist = bool(session.exec(wl_q).first())
    
    return {
        "id": movie.id,
        "title": movie.title,
        "year": movie.year,
        "plot": movie.plot,
        "rating": movie.rating,
        "runtime": movie.runtime,
        "content_rating": movie.content_rating,
        "poster_filename": movie.poster_filename,
        "backdrop_filename": movie.backdrop_filename,
        "director": movie.director,
        "cast": cast_list,
        "file_path": movie.file_path,
        "has_progress": has_progress,
        "progress": history.progress if history else 0,
        "in_watchlist": in_watchlist
    }

@app.get("/api/series/{series_id}")
def api_series(
    series_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    rating_level = get_profile_rating_level(current_user, current_profile)
    prof_id = current_profile.id if current_profile else None
    
    show = session.get(TVShow, series_id)
    if not show: raise HTTPException(status_code=404, detail="Show not found")
    
    if rating_level > 0 and not is_rating_allowed(show.content_rating, rating_level):
        raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")
        
    cast_list = []
    if show.cast and show.cast.startswith("["):
        try:
            import json
            cast_list = json.loads(show.cast)
        except: pass

    # Get all episodes with the same title
    episodes = session.exec(select(TVShow).where(TVShow.title == show.title).order_by(TVShow.file_path.asc())).all()
    
    # Batch query watch history for all episodes to eliminate N+1 queries
    ep_ids = [ep.id for ep in episodes]
    hist_q = select(WatchHistory).where(
        WatchHistory.user_id == current_user.id,
        WatchHistory.item_id.in_(ep_ids),
        WatchHistory.media_type == 'tv'
    )
    if prof_id:
        hist_q = hist_q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    hist_list = session.exec(hist_q).all()
    hist_by_ep = {h.item_id: h for h in hist_list}

    import re, os
    seasons = {}
    for ep in episodes:
        m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', ep.file_path)
        season_num = int(m.group(1)) if m else (ep.season or 1)
        ep_num = int(m.group(2)) if m else (ep.episode or ep.id)
        
        if season_num not in seasons:
            seasons[season_num] = []
            
        history = hist_by_ep.get(ep.id)
        
        seasons[season_num].append({
            "id": ep.id,
            "season": season_num,
            "episode": ep_num,
            "file_path": ep.file_path,
            "has_progress": history.progress > 0 if history and history.progress else False,
            "progress": history.progress if history else 0
        })
        
    wl_q = select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.item_id == show.id).where(Watchlist.media_type == 'tv')
    if prof_id:
        wl_q = wl_q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    in_watchlist = bool(session.exec(wl_q).first())
    
    return {
        "id": show.id,
        "title": show.title,
        "year": show.year,
        "plot": show.plot,
        "rating": show.rating,
        "runtime": show.runtime,
        "content_rating": show.content_rating,
        "poster_filename": show.poster_filename,
        "backdrop_filename": show.backdrop_filename,
        "director": show.director,
        "cast": cast_list,
        "in_watchlist": in_watchlist,
        "seasons": seasons
    }

@app.get("/api/search")
def api_search(
    request: Request, q: str = "",
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not q: return {"items": []}
    rating_level = get_profile_rating_level(current_user, current_profile)
    
    import urllib.parse
    results = []
    if current_user.movies_access:
        query = select(Movie).where(Movie.title.ilike(f"%{q}%"))
        if rating_level == 1:
            query = query.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            query = query.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
        raw_movies = session.exec(query.limit(25)).all()
        movies = process_movie_versions(raw_movies)
        for m in movies[:10]:
            results.append({
                "id": m.id, "title": m.title, "type": "movie", "url": f"/movie/{m.id}",
                "poster": m.poster_filename, "year": m.year
            })
            
    if current_user.tv_access:
        # Avoid pulling 50,000 episode rows on broad search terms
        subq = select(func.min(TVShow.id)).where(TVShow.title.ilike(f"%{q}%")).group_by(func.lower(TVShow.title))
        query = select(TVShow).where(TVShow.id.in_(subq))
        if rating_level == 1:
            query = query.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            query = query.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
        shows = session.exec(query.limit(15)).all()
        processed = process_tv_shows(shows)
        for t in processed[:10]:
            results.append({
                "id": t.id, "title": t.title, "type": "tv", "url": f"/series/{urllib.parse.quote(t.title)}",
                "poster": t.poster_filename, "year": t.year
            })
            
    if current_user.photos_access:
        photos = session.exec(select(Photo).where(Photo.title.ilike(f"%{q}%")).limit(10)).all()
        for p in photos:
            if p.file_path and is_recycle_path(p.file_path):
                continue
            disp_date = p.date_taken or p.date_added
            results.append({
                "id": p.id, "title": p.title, "type": "photo", "url": f"/photo/{p.id}",
                "poster": p.poster_filename, "year": disp_date.strftime("%Y") if disp_date else ""
            })

    if current_user.music_access:
        try:
            albums = session.exec(select(Album).where((Album.title.ilike(f"%{q}%")) | (Album.artist_name.ilike(f"%{q}%"))).limit(5)).all()
            for a in albums:
                results.append({
                    "id": a.id, "title": a.title, "type": "music", "url": f"/album/{a.id}",
                    "poster": a.cover_filename, "year": f"Album • {a.artist_name}"
                })
        except Exception:
            pass
        try:
            artists = session.exec(select(Artist).where(Artist.name.ilike(f"%{q}%")).limit(5)).all()
            for art in artists:
                results.append({
                    "id": art.id, "title": art.name, "type": "music", "url": f"/artist/{art.id}",
                    "poster": art.thumbnail_filename, "year": "Artist"
                })
        except Exception:
            pass
        try:
            tracks = session.exec(select(Track).where((Track.title.ilike(f"%{q}%")) | (Track.artist_name.ilike(f"%{q}%"))).limit(8)).all()
            for trk in tracks:
                results.append({
                    "id": trk.id, "title": trk.title, "type": "music", "url": f"/album/{trk.album_id}" if trk.album_id else "#",
                    "poster": trk.cover_filename, "year": f"Track • {trk.artist_name}"
                })
        except Exception:
            pass
            
    if current_user.movies_access or current_user.tv_access:
        try:
            seen_people = set()
            matching_movies = session.exec(
                select(Movie).where((Movie.cast.ilike(f"%{q}%")) | (Movie.director.ilike(f"%{q}%"))).limit(10)
            ).all()
            for mm in matching_movies:
                if mm.director and q.lower() in mm.director.lower():
                    for d in mm.director.split(','):
                        d_clean = d.strip()
                        if q.lower() in d_clean.lower() and d_clean.lower() not in seen_people:
                            seen_people.add(d_clean.lower())
                            results.append({
                                "id": d_clean, "title": d_clean, "type": "person", "url": f"/person/{urllib.parse.quote(d_clean)}",
                                "poster": None, "year": "Director"
                            })
                if mm.cast and mm.cast.startswith("["):
                    try:
                        cast_arr = json.loads(mm.cast)
                        for actor in cast_arr:
                            act_name = actor.get("name", "").strip()
                            if q.lower() in act_name.lower() and act_name.lower() not in seen_people:
                                seen_people.add(act_name.lower())
                                results.append({
                                    "id": act_name, "title": act_name, "type": "person", "url": f"/person/{urllib.parse.quote(act_name)}",
                                    "poster": actor.get("profile_filename"), "year": "Actor"
                                })
                    except Exception:
                        pass
        except Exception:
            pass

    # Sort results by title roughly
    results.sort(key=lambda x: x["title"].lower())
            
    return {"items": results}

@app.get("/photo/{item_id}", response_class=HTMLResponse)
def photo_view(
    request: Request,
    item_id: int,
    from_url: Optional[str] = Query(None, alias="from"),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if not current_user.photos_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    photo = session.get(Photo, item_id)
    if not photo: raise HTTPException(status_code=404, detail="Photo not found")
        
    ext = os.path.splitext(photo.file_path)[1].lower()
    is_video = ext in (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm")
    
    if photo.album:
        album_photos = session.exec(select(Photo).where(Photo.album == photo.album)).all()
    else:
        album_photos = session.exec(select(Photo).where(Photo.album == None)).all()
    album_photos = [p for p in album_photos if not (p.file_path and is_recycle_path(p.file_path))]
    album_photos.sort(key=lambda x: (x.title.lower(), x.id))

    idx = next((i for i, p in enumerate(album_photos) if p.id == item_id), -1)
    prev_photo = album_photos[idx - 1] if idx > 0 else None
    next_photo = album_photos[idx + 1] if 0 <= idx < len(album_photos) - 1 else None
    user_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()

    close_url = None
    if from_url:
        parsed_from = urllib.parse.urlparse(from_url)
        if not parsed_from.netloc or parsed_from.netloc == request.url.netloc:
            close_url = parsed_from.path + ("?" + parsed_from.query if parsed_from.query else "")

    if not close_url:
        referer = request.headers.get("referer")
        if referer:
            parsed = urllib.parse.urlparse(referer)
            if (not parsed.netloc or parsed.netloc == request.url.netloc) and not parsed.path.startswith("/photo/"):
                close_url = parsed.path + ("?" + parsed.query if parsed.query else "")

    if not close_url:
        if photo.album:
            close_url = f"/library/photo?album={urllib.parse.quote(photo.album)}"
        else:
            close_url = "/library/photo"

    return templates.TemplateResponse(request=request, name="photo.html", context={
        "user": current_user, "current_profile": current_profile, "user_profiles": user_profiles,
        "photo": photo, "is_video": is_video,
        "prev_photo_id": prev_photo.id if prev_photo else None,
        "next_photo_id": next_photo.id if next_photo else None,
        "close_url": close_url
    })

@app.get("/photo_file/{item_id}")
def get_photo_file(item_id: int, current_user: User = Depends(get_current_user)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.photos_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    with Session(engine) as session:
        photo = session.get(Photo, item_id)
        if not photo or not os.path.exists(photo.file_path): raise HTTPException(status_code=404, detail="File missing")
        file_path = photo.file_path
    
    return FileResponse(file_path)

@app.get("/library/{media_type}", response_class=HTMLResponse)
def library_view(
    request: Request,
    media_type: str,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    rating_level = get_profile_rating_level(current_user, current_profile)
    prof_id = current_profile.id if current_profile else None
    avail_vols = get_available_volumes(session)

    if media_type in avail_vols and not avail_vols[media_type]:
        return RedirectResponse(url="/", status_code=303)

    album_breadcrumbs = []
    clean_album = None
    current_view = request.query_params.get("view", "library")
    active_genre = request.query_params.get("genre")
    active_collection = request.query_params.get("collection")
    categories = []
    collections = []
    recommended_items = []
    recently_added = []
    recently_released = []
    top_rated_items = []

    if media_type == "movie":
        if not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
        
        if current_view == "categories":
            subq = select(func.min(Movie.id)).group_by(func.coalesce(Movie.tmdb_id, func.lower(Movie.title)))
            q_cat = select(Movie).where(Movie.id.in_(subq))
            if rating_level == 1:
                q_cat = q_cat.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                q_cat = q_cat.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
            all_movies = session.exec(q_cat).all()
            categories = build_categories_data(all_movies)
            total_count = len(categories)
        elif current_view == "collections":
            subq = select(func.min(Movie.id)).group_by(func.coalesce(Movie.tmdb_id, func.lower(Movie.title)))
            q_col = select(Movie).where(Movie.id.in_(subq))
            if rating_level == 1:
                q_col = q_col.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                q_col = q_col.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
            all_movies = session.exec(q_col).all()
            collections = build_collections_data(all_movies)
            total_count = len(collections)
        elif current_view == "recommended":
            watched_movie_ids, _, _ = get_user_watched_status(session, current_user.id, prof_id)
            q_rec = select(Movie)
            if rating_level == 1:
                q_rec = q_rec.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                q_rec = q_rec.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
            all_rec_movies = session.exec(q_rec.order_by(Movie.id.desc()).limit(100)).all()
            rated = [m for m in all_rec_movies if m.rating is not None]
            rated.sort(key=lambda m: m.rating or 0, reverse=True)
            top_rated_items = rated[:15]
            recently_added = sorted(all_rec_movies, key=lambda m: m.id or 0, reverse=True)[:15]
            recently_released = sorted(all_rec_movies, key=get_release_year, reverse=True)[:15]
            recommended_items = [m for m in all_rec_movies if m.id not in watched_movie_ids][:15] or all_rec_movies[:15]
            total_count = len(recommended_items)
        else: # view == "library" (fast SQL count without full table deserialization)
            q_count = select(func.count(func.distinct(func.coalesce(Movie.tmdb_id, Movie.title))))
            if rating_level == 1:
                q_count = q_count.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                q_count = q_count.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))
            if active_genre:
                q_count = q_count.where(Movie.genres.ilike(f"%{active_genre.strip()}%"))
            elif active_collection:
                q_count = q_count.where((Movie.collection_name.ilike(f"%{active_collection.strip()}%")) | (Movie.title.ilike(f"%{active_collection.strip()}%")))
            total_count = session.exec(q_count).one()

    elif media_type == "tv":
        if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
        
        if current_view == "categories":
            subq = select(func.min(TVShow.id)).group_by(func.lower(TVShow.title))
            q_cat = select(TVShow).where(TVShow.id.in_(subq))
            if rating_level == 1:
                q_cat = q_cat.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                q_cat = q_cat.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
            all_shows = session.exec(q_cat).all()
            categories = build_categories_data(all_shows)
            total_count = len(categories)
        elif current_view == "collections":
            subq = select(func.min(TVShow.id)).group_by(func.lower(TVShow.title))
            q_col = select(TVShow).where(TVShow.id.in_(subq))
            if rating_level == 1:
                q_col = q_col.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                q_col = q_col.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
            all_shows = session.exec(q_col).all()
            collections = build_collections_data(all_shows)
            total_count = len(collections)
        elif current_view == "recommended":
            _, _, watched_tv_titles = get_user_watched_status(session, current_user.id, prof_id)
            q_rec = select(TVShow)
            if rating_level == 1:
                q_rec = q_rec.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                q_rec = q_rec.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
            rec_shows = process_tv_shows(session.exec(q_rec.order_by(TVShow.id.desc()).limit(150)).all())
            rated = [s for s in rec_shows if s.rating is not None]
            rated.sort(key=lambda s: s.rating or 0, reverse=True)
            top_rated_items = rated[:15]
            recently_added = sorted(rec_shows, key=lambda s: s.id or 0, reverse=True)[:15]
            recently_released = sorted(rec_shows, key=get_release_year, reverse=True)[:15]
            recommended_items = [s for s in rec_shows if s.title.strip().lower() not in watched_tv_titles][:15] or rec_shows[:15]
            total_count = len(recommended_items)
        else: # view == "library" (fast SQL count without full table deserialization)
            q_count = select(func.count(func.distinct(TVShow.title)))
            if rating_level == 1:
                q_count = q_count.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
            elif rating_level >= 2:
                q_count = q_count.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))
            if active_genre:
                q_count = q_count.where(TVShow.genres.ilike(f"%{active_genre.strip()}%"))
            elif active_collection:
                q_count = q_count.where((TVShow.collection_name.ilike(f"%{active_collection.strip()}%")) | (TVShow.title.ilike(f"%{active_collection.strip()}%")))
            total_count = session.exec(q_count).one()

    elif media_type == "photo":
        if not current_user.photos_access: raise HTTPException(status_code=403, detail="Forbidden")
        raw_album = request.query_params.get("album")
        if raw_album:
            clean_album = raw_album.strip().strip("/").replace("\\", "/")
            if clean_album:
                parts = clean_album.split("/")
                accum = []
                for p in parts:
                    accum.append(p)
                    album_breadcrumbs.append({
                        "name": p,
                        "path": "/".join(accum)
                    })
                total_count = session.exec(
                    select(func.count(Photo.id)).where(
                        (Photo.album == clean_album) | (Photo.album.like(f"{clean_album}/%"))
                    )
                ).one()
            else:
                clean_album = None
                total_count = session.exec(select(func.count(Photo.id))).one()
        else:
            total_count = session.exec(select(func.count(Photo.id))).one()
    elif media_type == "music":
        if not current_user.music_access: raise HTTPException(status_code=403, detail="Forbidden")
        view_type = request.query_params.get("view", "albums")
        if view_type == "artists":
            total_count = session.exec(select(func.count(Artist.id))).one()
        elif view_type == "tracks":
            total_count = session.exec(select(func.count(Track.id))).one()
        elif view_type == "playlists":
            total_count = session.exec(select(func.count(Playlist.id)).where(Playlist.user_id == current_user.id)).one()
        else:
            total_count = session.exec(select(func.count(Album.id))).one()
    else:
        raise HTTPException(status_code=404, detail="Not Found")
        
    user_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()

    return templates.TemplateResponse(request=request, name="library.html", context={
        "user": current_user, "current_profile": current_profile, "user_profiles": user_profiles,
        "media_type": media_type, "total_count": total_count, "album": clean_album, "album_breadcrumbs": album_breadcrumbs,
        "has_movies": avail_vols["movie"], "has_tv": avail_vols["tv"], "has_photos": avail_vols["photo"], "has_music": avail_vols["music"],
        "music_view": request.query_params.get("view", "albums"),
        "current_view": current_view,
        "active_genre": active_genre,
        "active_collection": active_collection,
        "categories": categories,
        "collections": collections,
        "recommended_items": recommended_items,
        "recently_added": recently_added,
        "recently_released": recently_released,
        "top_rated_items": top_rated_items
    })
@app.get("/movie/{movie_id}", response_class=HTMLResponse)
def movie_view(
    request: Request,
    movie_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    rating_level = get_profile_rating_level(current_user, current_profile)
    prof_id = current_profile.id if current_profile else None
    
    movie = session.get(Movie, movie_id)
    if not movie: raise HTTPException(status_code=404, detail="Movie not found")
    if rating_level > 0 and not is_rating_allowed(movie.content_rating, rating_level):
        raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")
        
    cast_list = []
    if movie.cast and movie.cast.startswith("["):
        try:
            import json
            cast_list = json.loads(movie.cast)
        except: pass
        
    # Discover all editions/versions for this movie
    raw_year = str(movie.year or "").split(" • ")[0].strip()
    if movie.tmdb_id:
        v_q = select(Movie).where(
            (Movie.tmdb_id == movie.tmdb_id) |
            ((func.lower(Movie.title) == movie.title.strip().lower()) & (Movie.year.like(f"{raw_year}%") if raw_year else True))
        )
    else:
        v_q = select(Movie).where(
            func.lower(Movie.title) == movie.title.strip().lower()
        )
        if raw_year:
            v_q = v_q.where(Movie.year.like(f"{raw_year}%"))

    raw_versions = session.exec(v_q).all()
    if rating_level > 0:
        raw_versions = [v for v in raw_versions if is_rating_allowed(v.content_rating, rating_level)]

    movie_versions = []
    for v in raw_versions:
        f_size = 0
        try:
            if v.file_path and os.path.exists(v.file_path):
                f_size = os.path.getsize(v.file_path)
        except Exception:
            pass

        probe = probe_media_streams(v.file_path) if (v.file_path and os.path.exists(v.file_path)) else {}
        res_label = get_media_resolution_label(probe)
        height = probe.get("height") or 0

        movie_versions.append({
            "id": v.id,
            "filename": os.path.basename(v.file_path),
            "file_path": v.file_path,
            "resolution": res_label,
            "file_size": format_file_size(f_size),
            "raw_size": f_size,
            "height": height,
            "is_current": v.id == movie.id
        })

    movie_versions.sort(key=lambda x: (x["height"], x["raw_size"]), reverse=True)
    current_version = next((v for v in movie_versions if v["is_current"]), movie_versions[0] if movie_versions else None)

    hist_q = select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == movie.id).where(WatchHistory.media_type == 'movie')
    if prof_id:
        hist_q = hist_q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    history = session.exec(hist_q).first()

    # If current version has no progress, check if user has progress on a sibling version
    if (not history or not history.progress) and raw_versions:
        sibling_ids = [v.id for v in raw_versions if v.id != movie.id]
        if sibling_ids:
            sib_q = select(WatchHistory).where(
                WatchHistory.user_id == current_user.id,
                WatchHistory.item_id.in_(sibling_ids),
                WatchHistory.media_type == 'movie',
                WatchHistory.progress > 0
            )
            if prof_id:
                sib_q = sib_q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
            sibling_hist = session.exec(sib_q.order_by(WatchHistory.last_watched.desc())).first()
            if sibling_hist:
                history = sibling_hist

    has_progress = history.progress > 0 if history and history.progress else False
    
    wl_q = select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.item_id == movie.id).where(Watchlist.media_type == 'movie')
    if prof_id:
        wl_q = wl_q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    in_watchlist = bool(session.exec(wl_q).first())

    rt_str = get_runtime_str(movie, session)
    if rt_str:
        movie.year = f"{movie.year} • {rt_str}" if movie.year else rt_str

    watched_movie_ids, _, _ = get_user_watched_status(session, current_user.id, prof_id)
    is_watched = movie.id in watched_movie_ids

    user_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()

    related_movies = get_related_movies(session, movie, rating_level=rating_level, limit=15)

    wl_m_q = select(Watchlist.item_id).where(Watchlist.user_id == current_user.id, Watchlist.media_type == 'movie')
    if prof_id:
        wl_m_q = wl_m_q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    user_watchlist_movies = set(session.exec(wl_m_q).all())

    return templates.TemplateResponse(request=request, name="movie.html", context={
        "user": current_user, "current_profile": current_profile, "user_profiles": user_profiles,
        "movie": movie, "cast_list": cast_list, "has_progress": has_progress, "in_watchlist": in_watchlist,
        "is_watched": is_watched, "movie_versions": movie_versions, "current_version": current_version,
        "related_movies": related_movies, "watched_movie_ids": watched_movie_ids, "user_watchlist_movies": user_watchlist_movies
    })

def get_runtime_str(item, session):
    if hasattr(item, 'runtime') and item.runtime is None:
        try:
            cmd = [get_ffprobe_path(), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", item.file_path]
            import subprocess
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8').strip()
            item.runtime = int(float(out))
            session.commit()
        except:
            item.runtime = 0
            
    if not hasattr(item, 'runtime') or not item.runtime: return ""
    m = int(item.runtime // 60)
    if m < 60: return f"{m}min"
    return f"{m//60}hr {m%60}min"

@app.get("/api/movie/{movie_id}/trailer")
async def get_movie_trailer(
    movie_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.movies_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    movie = session.get(Movie, movie_id)
    if not movie:
        raise HTTPException(status_code=404, detail="Movie not found")

    rating_level = get_profile_rating_level(current_user, current_profile)
    if rating_level > 0 and not is_rating_allowed(getattr(movie, 'content_rating', ''), rating_level):
        raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")

    # 1. Local trailer hoarder support (checked FIRST by default)
    local_path = find_local_trailer(movie.file_path)
    if local_path:
        return {
            "source": "local",
            "title": movie.title,
            "stream_url": f"/stream/trailer/movie/{movie.id}"
        }

    # 2. Database cached YouTube trailer_key
    if movie.trailer_key:
        return {
            "source": "youtube",
            "title": movie.title,
            "key": movie.trailer_key,
            "embed_url": f"https://www.youtube-nocookie.com/embed/{movie.trailer_key}?autoplay=1&rel=0"
        }

    # 3. TMDB API Lookup
    tmdb_config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
    tmdb_key = tmdb_config.value if tmdb_config else os.getenv("TMDB_API_KEY")

    if tmdb_key:
        tmdb_id = movie.tmdb_id
        if not tmdb_id and movie.poster_filename:
            match = re.search(r'movie_(\d+)_', movie.poster_filename)
            if match:
                tmdb_id = int(match.group(1))

        raw_client = httpx.AsyncClient(timeout=10.0) if httpx is not None else None
        try:
            client = SafeHTTPClient(raw_client)
            if not tmdb_id:
                search_url = "https://api.themoviedb.org/3/search/movie"
                params = {"query": movie.title, "api_key": tmdb_key}
                if movie.year:
                    params["year"] = movie.year[:4]
                s_res = await client.get(search_url, params=params)
                if s_res.status_code == 200:
                    results = s_res.json().get("results", [])
                    if results:
                        clean_title = re.sub(r'[^a-zA-Z0-9]', '', movie.title).lower()
                        match_item = next((r for r in results if re.sub(r'[^a-zA-Z0-9]', '', r.get("title", "")).lower() == clean_title), results[0])
                        tmdb_id = match_item.get("id")

            if tmdb_id:
                movie.tmdb_id = tmdb_id
                v_res = await client.get(f"https://api.themoviedb.org/3/movie/{tmdb_id}/videos", params={"api_key": tmdb_key})
                if v_res.status_code == 200:
                    videos = v_res.json().get("results", [])
                    trailer_key = extract_tmdb_trailer_key(videos)
                    if trailer_key:
                        movie.trailer_key = trailer_key
                        session.add(movie)
                        session.commit()
                        return {
                            "source": "youtube",
                            "title": movie.title,
                            "key": trailer_key,
                            "embed_url": f"https://www.youtube-nocookie.com/embed/{trailer_key}?autoplay=1&rel=0"
                        }
        except Exception as e:
            print(f"[Trailer] TMDB trailer lookup error for {movie.title}: {e}")

    # 4. Fallback search URL
    clean_year = f" ({movie.year[:4]})" if movie.year else ""
    query_str = urllib.parse.quote_plus(f"{movie.title}{clean_year} official trailer")
    return {
        "source": "none",
        "title": movie.title,
        "message": "No trailer found locally or on TMDB.",
        "search_url": f"https://www.youtube.com/results?search_query={query_str}"
    }

@app.get("/stream/trailer/movie/{item_id}")
async def stream_movie_trailer(
    item_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.movies_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    movie = session.get(Movie, item_id)
    if not movie or not os.path.exists(movie.file_path):
        raise HTTPException(status_code=404, detail="Movie not found")
    rating_level = get_profile_rating_level(current_user, current_profile)
    if rating_level > 0 and not is_rating_allowed(getattr(movie, 'content_rating', ''), rating_level):
        raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")

    trailer_path = find_local_trailer(movie.file_path)
    if not trailer_path or not os.path.exists(trailer_path):
        raise HTTPException(status_code=404, detail="No local trailer found")

    file_ext = os.path.splitext(trailer_path)[1].lower()
    probe = probe_media_streams(trailer_path)

    if file_ext in [".mp4", ".m4v", ".webm"] and probe.get("can_direct_stream_video", True) and probe.get("can_direct_stream_audio", True):
        media_type = "video/webm" if file_ext == ".webm" else "video/mp4"
        return FileResponse(trailer_path, media_type=media_type)

    cmd = [
        get_ffmpeg_path(),
        "-i", trailer_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-max_muxing_queue_size", "1024",
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1"
    ]

    def generate():
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            process.kill()

    return StreamingResponse(generate(), media_type="video/mp4")

@app.get("/series/{title}", response_class=HTMLResponse)
def series_view(
    request: Request,
    title: str,
    background_tasks: BackgroundTasks,
    ep: Optional[int] = None,
    season: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    rating_level = get_profile_rating_level(current_user, current_profile)
    prof_id = current_profile.id if current_profile else None
    
    shows = session.exec(select(TVShow).where(TVShow.title == title)).all()
    if not shows:
        raise HTTPException(status_code=404, detail="Series not found")
    if rating_level > 0 and not is_rating_allowed(shows[0].content_rating, rating_level):
        raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")

    # Non-blocking background enrichment of missing logo, cast photos, and season posters
    needs_tv_enrichment = (
        not shows[0].logo_filename or
        not shows[0].cast or
        shows[0].cast == "[]" or
        '"profile_filename": null' in (shows[0].cast or '') or
        any(not e.season_poster_filename for e in shows)
    )
    if needs_tv_enrichment:
        background_tasks.add_task(async_enrich_tv_show, title)
        
    cast_list = []
    if shows[0].cast and shows[0].cast.startswith("["):
        try:
            import json
            cast_list = json.loads(shows[0].cast)
        except: pass
        
    series_ep_ids = [e.id for e in shows]
    hist_q = select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id.in_(series_ep_ids)).where(WatchHistory.media_type == 'tv')
    if prof_id:
        hist_q = hist_q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    history = session.exec(hist_q.order_by(WatchHistory.last_watched.desc())).first()
    
    has_progress = False
    resume_link = ""
    selected_episode = None
    
    if ep:
        # User requested a specific episode
        selected_episode = next((e for e in shows if e.id == ep), None)
        if selected_episode:
            resume_link = f"/play/tv/{selected_episode.id}"
    elif season is not None:
        def get_ep_season(e):
            if getattr(e, 'season', None) is not None: return e.season
            m = re.search(r'S(\d+)E(\d+)', e.file_path, re.IGNORECASE)
            return int(m.group(1)) if m else 0
        matching_season_eps = [e for e in shows if get_ep_season(e) == season]
        if matching_season_eps:
            selected_episode = matching_season_eps[0]
            resume_link = f"/play/tv/{selected_episode.id}"
            
    if not selected_episode:
        # Default behavior: use first episode or history
        sorted_shows = sorted(shows, key=get_tv_episode_sort_key)
        selected_episode = sorted_shows[0]
        if history:
            hist_ep = next((e for e in shows if e.id == history.item_id), None)
            if history.progress < 95.0 and history.progress > 0:
                has_progress = True
                if hist_ep:
                    selected_episode = hist_ep
                resume_link = f"/play/tv/{history.item_id}"
            elif history.progress >= 95.0:
                next_ep = get_next_uncompleted_episode(session, hist_ep, current_user.id, prof_id) if hist_ep else None
                if next_ep:
                    selected_episode = next_ep
                    resume_link = f"/play/tv/{next_ep.id}"
                else:
                    resume_link = f"/play/tv/{selected_episode.id}"
            else:
                if hist_ep:
                    selected_episode = hist_ep
                resume_link = f"/play/tv/{selected_episode.id}"
        else:
            resume_link = f"/play/tv/{selected_episode.id}"
        
    wl_q = select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.item_id == selected_episode.id).where(Watchlist.media_type == 'tv')
    if prof_id:
        wl_q = wl_q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    in_watchlist = bool(session.exec(wl_q).first())

    _, watched_tv_episode_ids, watched_tv_titles = get_user_watched_status(session, current_user.id, prof_id)
    is_series_watched = title.strip().lower() in watched_tv_titles
        
    def get_s_e(t):
        m = re.search(r'S(\d+)E(\d+)', t.file_path, re.IGNORECASE)
        if m: return (int(m.group(1)), int(m.group(2)))
        return (0, 0)

    ep_hist_q = select(WatchHistory).where(
        WatchHistory.user_id == current_user.id,
        WatchHistory.item_id.in_(series_ep_ids),
        WatchHistory.media_type == 'tv'
    )
    if prof_id:
        ep_hist_q = ep_hist_q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    ep_histories = session.exec(ep_hist_q).all()
    progress_map = {}
    for h in ep_histories:
        if h.item_id not in progress_map or (h.last_watched and (h.progress or 0) > 0):
            progress_map[h.item_id] = bool(0 < (h.progress or 0) < 95.0)
        
    episodes = []
    seasons = set()
    for ep_obj in sorted(shows, key=get_s_e):
        ep_dict = {
            "id": ep_obj.id,
            "title": ep_obj.title,
            "poster_filename": ep_obj.poster_filename,
            "season_poster_filename": ep_obj.season_poster_filename,
            "file_path": ep_obj.file_path,
            "season_episode": "",
            "episode_name": "",
            "season": 0
        }
        m = re.search(r'S(\d+)E(\d+)', ep_dict['file_path'], re.IGNORECASE)
        if m:
            s_num = int(m.group(1))
            e_num = int(m.group(2))
            ep_dict["season"] = s_num
            ep_dict["episode"] = ep_obj.episode or e_num
            seasons.add(s_num)
            base = f"Episode {e_num}"
        else:
            ep_dict["season"] = ep_obj.season or 0
            ep_dict["episode"] = ep_obj.episode or 1
            seasons.add(ep_dict["season"])
            base = f"Episode {ep_dict['episode']}" if ep_dict.get("episode") else "Special"
        
        rt_str = get_runtime_str(ep_obj, session)
        rt_suffix = f" • {rt_str}" if rt_str else ""
        
        filename = os.path.splitext(os.path.basename(ep_dict['file_path']))[0]
        name_match = re.search(r'S\d+E\d+[ \-]*(.+)', filename, re.IGNORECASE)
        
        if name_match and name_match.group(1).strip():
            ep_dict['episode_name'] = name_match.group(1).strip()
            ep_dict['season_episode'] = f"{ep_dict['episode_name']} • {base}{rt_suffix}"
        else:
            ep_dict['season_episode'] = f"{base}{rt_suffix}"
            
        thumb_name = getattr(ep_obj, "thumbnail_filename", None) or f"tv_thumb_{ep_obj.id}.jpg"
        metadata_dir = METADATA_DIR if os.path.exists(METADATA_DIR) else "config/metadata"
        thumb_exists = os.path.exists(os.path.join(metadata_dir, thumb_name))
        ep_dict["thumbnail_filename"] = thumb_name if thumb_exists else None
        ep_dict["plot"] = ep_obj.plot or ""
        ep_dict["has_progress"] = progress_map.get(ep_obj.id, False)
        ep_dict["play_url"] = f"/play/tv/{ep_obj.id}"

        ep_dict['is_watched'] = ep_obj.id in watched_tv_episode_ids
        episodes.append(ep_dict)
    
    sorted_seasons = sorted(list(seasons))
    
    selected_ep_dict = next((e for e in episodes if e['id'] == selected_episode.id), None)
    
    if not resume_link and episodes:
        resume_link = f"/play/tv/{episodes[0]['id']}"

    user_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()

    related_shows = get_related_shows(session, selected_episode, rating_level=rating_level, limit=15)

    wl_t_q = select(Watchlist.item_id).where(Watchlist.user_id == current_user.id, Watchlist.media_type == 'tv')
    if prof_id:
        wl_t_q = wl_t_q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    user_watchlist_tv = set(session.exec(wl_t_q).all())

    # Background ensure 16:9 episode frame thumbnails exist on disk
    ep_tuples = [(s.id, s.file_path) for s in shows if s.id and s.file_path]
    def _bg_generate_episode_thumbnails(tuples):
        m_dir = METADATA_DIR if os.path.exists(METADATA_DIR) else "config/metadata"
        for ep_id, ep_path in tuples:
            t_name = f"tv_thumb_{ep_id}.jpg"
            t_path = os.path.join(m_dir, t_name)
            if not os.path.exists(t_path) and ep_path and os.path.exists(ep_path):
                if extract_video_frame(ep_path, t_path, target_fraction=0.40):
                    with Session(engine) as s:
                        db_ep = s.get(TVShow, ep_id)
                        if db_ep and not db_ep.thumbnail_filename:
                            db_ep.thumbnail_filename = t_name
                            s.add(db_ep)
                            try:
                                s.commit()
                            except Exception:
                                s.rollback()
    threading.Thread(target=_bg_generate_episode_thumbnails, args=(ep_tuples,), daemon=True).start()

    return templates.TemplateResponse(request=request, name="series.html", context={
        "user": current_user, "current_profile": current_profile, "user_profiles": user_profiles,
        "title": title, "episodes": episodes, "seasons": sorted_seasons, "series": selected_episode, "cast_list": cast_list,
        "has_progress": has_progress, "in_watchlist": in_watchlist, "resume_link": resume_link, "selected_ep_id": selected_episode.id,
        "selected_ep_dict": selected_ep_dict, "is_series_watched": is_series_watched,
        "related_shows": related_shows, "watched_tv_titles": watched_tv_titles, "user_watchlist_tv": user_watchlist_tv
    })

def format_seconds_to_duration(seconds: float) -> str:
    if not seconds:
        return "0 min"
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0:
        return f"{hours} hr {minutes} min"
    return f"{minutes} min"

def format_person_dates(birthday: Optional[str], deathday: Optional[str]) -> Optional[str]:
    if not birthday:
        return None
    try:
        bday = datetime.strptime(birthday.strip(), "%Y-%m-%d").date()
        ref_date = datetime.strptime(deathday.strip(), "%Y-%m-%d").date() if deathday else datetime.now().date()
        age = ref_date.year - bday.year - ((ref_date.month, ref_date.day) < (bday.month, bday.day))
        
        bday_fmt = bday.strftime("%B %d, %Y")
        if deathday:
            dday = datetime.strptime(deathday.strip(), "%Y-%m-%d").date()
            dday_fmt = dday.strftime("%B %d, %Y")
            return f"{bday_fmt} - {dday_fmt} ({age} years old)"
        else:
            return f"{bday_fmt} ({age} years old)"
    except Exception:
        if deathday:
            return f"{birthday} - {deathday}"
        return birthday

# ----------------- PERSON DETAIL VIEW -----------------

@app.get("/person/{person_identifier}")
@app.get("/people/{person_identifier}")
async def person_view(
    request: Request,
    person_identifier: str,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not current_user.movies_access and not current_user.tv_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    rating_level = get_profile_rating_level(current_user, current_profile)
    prof_id = current_profile.id if current_profile else None

    person_str = urllib.parse.unquote(person_identifier).strip()
    if not person_str:
        raise HTTPException(status_code=404, detail="Person not found")

    tmdb_config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
    tmdb_key = tmdb_config.value if tmdb_config and tmdb_config.value else os.getenv("TMDB_API_KEY")

    person_data = None
    combined_credits = None
    tmdb_person_id = None
    target_name = person_str

    if tmdb_key and httpx is not None:
        try:
            raw_client = httpx.AsyncClient(timeout=10.0)
            client = SafeHTTPClient(raw_client)

            if person_str.isdigit():
                tmdb_person_id = int(person_str)
            else:
                s_url = f"https://api.themoviedb.org/3/search/person?query={urllib.parse.quote(person_str)}&api_key={tmdb_key}"
                s_res = await client.get(s_url)
                if s_res.status_code == 200:
                    results = s_res.json().get("results", [])
                    if results:
                        tmdb_person_id = results[0].get("id")
                        target_name = results[0].get("name") or person_str

            if tmdb_person_id:
                p_url = f"https://api.themoviedb.org/3/person/{tmdb_person_id}?api_key={tmdb_key}&append_to_response=combined_credits"
                p_res = await client.get(p_url)
                if p_res.status_code == 200:
                    person_data = p_res.json()
                    target_name = person_data.get("name") or target_name
                    combined_credits = person_data.get("combined_credits", {})
        except Exception:
            pass

    # Profile picture resolution
    profile_filename = None
    if person_data and person_data.get("profile_path") and tmdb_person_id:
        profile_path = person_data["profile_path"]
        profile_filename = f"person_{tmdb_person_id}_profile.jpg"
        target_file = os.path.join(METADATA_DIR, profile_filename)
        if not os.path.exists(target_file) and tmdb_key:
            try:
                img_url = f"https://image.tmdb.org/t/p/w600_and_h900_bestv2{profile_path}"
                raw_client = httpx.AsyncClient(timeout=10.0)
                client = SafeHTTPClient(raw_client)
                img_res = await client.get(img_url)
                if img_res.status_code == 200:
                    os.makedirs(METADATA_DIR, exist_ok=True)
                    with open(target_file, "wb") as f:
                        f.write(img_res.content)
            except Exception:
                pass

    # Fallback to local DB cast profile picture if not found
    if not profile_filename or not os.path.exists(os.path.join(METADATA_DIR, profile_filename)):
        cast_match = session.exec(select(Movie.cast).where(Movie.cast.ilike(f"%{target_name}%")).limit(15)).all()
        for cm in cast_match:
            if cm and cm.startswith("["):
                try:
                    items = json.loads(cm)
                    for it in items:
                        if it.get("name", "").strip().lower() == target_name.lower():
                            if it.get("profile_filename") and os.path.exists(os.path.join(METADATA_DIR, it["profile_filename"])):
                                profile_filename = it["profile_filename"]
                                break
                except Exception:
                    pass
            if profile_filename:
                break

    # Birthdate / Age / Biography
    birthday = person_data.get("birthday") if person_data else None
    deathday = person_data.get("deathday") if person_data else None
    dates_display = format_person_dates(birthday, deathday)
    place_of_birth = person_data.get("place_of_birth") if person_data else None
    biography = person_data.get("biography") if person_data else None

    # Collect TMDB IDs & titles from credits
    tmdb_movie_ids = set()
    tmdb_movie_titles = set()
    tmdb_tv_titles = set()

    if combined_credits:
        for c in combined_credits.get("cast", []) + combined_credits.get("crew", []):
            m_type = c.get("media_type")
            if m_type == "movie":
                if c.get("id"):
                    tmdb_movie_ids.add(c["id"])
                if c.get("title"):
                    tmdb_movie_titles.add(c["title"].strip().lower())
            elif m_type == "tv":
                if c.get("name"):
                    tmdb_tv_titles.add(c["name"].strip().lower())

    watched_movie_ids, _, watched_tv_titles = get_user_watched_status(session, current_user.id, prof_id)

    wl_m_q = select(Watchlist.item_id).where(Watchlist.user_id == current_user.id, Watchlist.media_type == 'movie')
    if prof_id:
        wl_m_q = wl_m_q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    user_watchlist_movies = set(session.exec(wl_m_q).all())

    wl_t_q = select(Watchlist.item_id).where(Watchlist.user_id == current_user.id, Watchlist.media_type == 'tv')
    if prof_id:
        wl_t_q = wl_t_q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    user_watchlist_tv = set(session.exec(wl_t_q).all())

    from sqlalchemy import or_

    known_for = []
    seen_movie_titles = set()

    if current_user.movies_access:
        m_conds = [
            Movie.cast.ilike(f"%{target_name}%"),
            Movie.director.ilike(f"%{target_name}%")
        ]
        if tmdb_movie_ids:
            m_conds.append(Movie.tmdb_id.in_(list(tmdb_movie_ids)))

        m_q = select(Movie).where(or_(*m_conds))
        if rating_level == 1:
            m_q = m_q.where(Movie.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            m_q = m_q.where(Movie.content_rating.in_(CHILD_SAFE_RATINGS))

        matched_movies = session.exec(m_q).all()
        for m in matched_movies:
            norm_t = m.title.strip().lower()
            if norm_t in seen_movie_titles:
                continue
            seen_movie_titles.add(norm_t)

            known_for.append({
                "id": m.id,
                "media_type": "movie",
                "title": m.title,
                "year": m.year,
                "rating": m.rating or 0.0,
                "poster_filename": m.poster_filename,
                "url": f"/movie/{m.id}",
                "play_url": f"/play/movie/{m.id}",
                "is_watched": m.id in watched_movie_ids,
                "in_watchlist": m.id in user_watchlist_movies,
                "director": m.director or "",
                "cast": m.cast or ""
            })

    seen_tv_titles = set()
    if current_user.tv_access:
        t_conds = [
            TVShow.cast.ilike(f"%{target_name}%"),
            TVShow.director.ilike(f"%{target_name}%")
        ]
        if tmdb_tv_titles:
            t_conds.append(func.lower(TVShow.title).in_(list(tmdb_tv_titles)))

        t_q = select(TVShow).where(or_(*t_conds))
        if rating_level == 1:
            t_q = t_q.where(TVShow.content_rating.in_(TEEN_SAFE_RATINGS))
        elif rating_level >= 2:
            t_q = t_q.where(TVShow.content_rating.in_(CHILD_SAFE_RATINGS))

        matched_shows = session.exec(t_q).all()
        distinct_titles = {t.title for t in matched_shows}
        if distinct_titles:
            all_episodes_for_shows = session.exec(
                select(TVShow).where(TVShow.title.in_(list(distinct_titles)))
            ).all()
            processed_shows = process_tv_shows(all_episodes_for_shows)
        else:
            processed_shows = []

        for t in processed_shows:
            norm_t = t.title.strip().lower()
            if norm_t in seen_tv_titles:
                continue
            seen_tv_titles.add(norm_t)

            known_for.append({
                "id": t.id,
                "media_type": "tv",
                "title": t.title,
                "year": t.year,
                "rating": t.rating or 0.0,
                "poster_filename": t.poster_filename or t.season_poster_filename,
                "url": f"/series/{urllib.parse.quote(t.title)}",
                "play_url": f"/play/tv/{t.id}",
                "is_watched": norm_t in watched_tv_titles,
                "in_watchlist": t.id in user_watchlist_tv,
                "director": t.director or "",
                "cast": t.cast or ""
            })

    # Sort known_for by rating descending
    known_for.sort(key=lambda x: (x.get("rating") or 0.0, str(x.get("year") or "")), reverse=True)

    # Determine role display
    roles = []
    directed = any(target_name.lower() in item.get("director", "").lower() for item in known_for)
    acted = any(target_name.lower() in item.get("cast", "").lower() for item in known_for)

    if directed:
        roles.append("Director")
    if acted:
        roles.append("Actor")

    if not roles and person_data and person_data.get("known_for_department"):
        dept = person_data["known_for_department"].strip()
        dept_map = {
            "Acting": "Actor",
            "Directing": "Director",
            "Writing": "Writer",
            "Production": "Producer",
            "Creator": "Creator",
            "Camera": "Cinematographer",
            "Sound": "Composer",
            "Editing": "Editor",
            "Crew": "Crew"
        }
        roles.append(dept_map.get(dept, dept))

    if not roles:
        roles.append("Actor")

    role_display = ", ".join(roles)

    person = {
        "id": tmdb_person_id,
        "name": target_name,
        "profile_filename": profile_filename,
        "role_display": role_display,
        "dates_display": dates_display,
        "place_of_birth": place_of_birth,
        "biography": biography
    }

    user_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()

    return templates.TemplateResponse(request=request, name="person.html", context={
        "user": current_user,
        "current_profile": current_profile,
        "user_profiles": user_profiles,
        "person": person,
        "known_for": known_for
    })


# ----------------- MUSIC ROUTES & DETAIL VIEWS -----------------

@app.get("/stream/music/{track_id}")
def stream_music(
    track_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    track = session.get(Track, track_id)
    if not track or not track.file_path or not os.path.exists(track.file_path):
        raise HTTPException(status_code=404, detail="Track not found")

    ext = os.path.splitext(track.file_path)[1].lower()
    media_type = "audio/mpeg"
    if ext == ".flac":
        media_type = "audio/flac"
    elif ext in [".m4a", ".alac"]:
        media_type = "audio/mp4"
    elif ext == ".aac":
        media_type = "audio/aac"
    elif ext in [".ogg", ".opus"]:
        media_type = "audio/ogg"
    elif ext == ".wav":
        media_type = "audio/wav"
    elif ext == ".aiff":
        media_type = "audio/aiff"
    elif ext == ".wma":
        media_type = "audio/x-ms-wma"

    return FileResponse(track.file_path, media_type=media_type)


@app.get("/album/{album_id}", response_class=HTMLResponse)
def album_view(
    request: Request,
    album_id: int,
    from_url: Optional[str] = Query(None, alias="from"),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    artist = session.get(Artist, album.artist_id) if album.artist_id else None
    if not artist and album.artist_name:
        artist = session.exec(select(Artist).where(func.lower(Artist.name) == album.artist_name.strip().lower())).first()

    tracks_db = session.exec(
        select(Track).where(Track.album_id == album.id).order_by(Track.disc_number.asc(), Track.track_number.asc(), Track.title.asc())
    ).all()

    total_duration = 0.0
    tracks = []
    for t in tracks_db:
        dur = t.duration or 0.0
        total_duration += dur
        mins = int(dur // 60)
        secs = int(dur % 60)
        t_data = {
            "id": t.id,
            "title": t.title,
            "artist_name": t.artist_name or album.artist_name,
            "album_title": album.title,
            "album_id": album.id,
            "track_number": t.track_number or 1,
            "disc_number": t.disc_number or 1,
            "duration": dur,
            "duration_str": f"{mins}:{secs:02d}",
            "bitrate": f"{int(t.bitrate)} kbps" if t.bitrate else None,
            "codec": (t.codec or "").upper(),
            "cover_url": f"/metadata/{t.cover_filename or album.cover_filename}" if (t.cover_filename or album.cover_filename) else None,
            "stream_url": f"/stream/music/{t.id}"
        }
        tracks.append(t_data)

    total_dur_str = format_seconds_to_duration(total_duration)
    playlists = session.exec(select(Playlist).where(Playlist.user_id == current_user.id).order_by(Playlist.name.asc())).all()
    user_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()
    avail_vols = get_available_volumes(session)

    close_url = None
    if from_url:
        parsed_from = urllib.parse.urlparse(from_url)
        if not parsed_from.netloc or parsed_from.netloc == request.url.netloc:
            close_url = parsed_from.path + ("?" + parsed_from.query if parsed_from.query else "")

    if not close_url:
        referer = request.headers.get("referer")
        if referer:
            parsed = urllib.parse.urlparse(referer)
            if (not parsed.netloc or parsed.netloc == request.url.netloc) and not parsed.path.startswith(f"/album/{album_id}"):
                close_url = parsed.path + ("?" + parsed.query if parsed.query else "")

    if not close_url:
        close_url = "/library/music?view=albums"

    return templates.TemplateResponse(request=request, name="album.html", context={
        "user": current_user,
        "current_profile": current_profile,
        "user_profiles": user_profiles,
        "album": album,
        "artist": artist,
        "tracks": tracks,
        "total_duration_str": total_dur_str,
        "total_tracks": len(tracks),
        "playlists": playlists,
        "close_url": close_url,
        "has_movies": avail_vols["movie"],
        "has_tv": avail_vols["tv"],
        "has_photos": avail_vols["photo"],
        "has_music": avail_vols["music"]
    })


@app.get("/artist/{artist_id}", response_class=HTMLResponse)
def artist_view(
    request: Request,
    artist_id: int,
    from_url: Optional[str] = Query(None, alias="from"),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    artist = session.get(Artist, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")

    albums = session.exec(
        select(Album).where((Album.artist_id == artist.id) | (func.lower(Album.artist_name) == artist.name.strip().lower())).order_by(Album.year.desc(), Album.title.asc())
    ).all()

    tracks_db = session.exec(
        select(Track).where((Track.artist_id == artist.id) | (func.lower(Track.artist_name) == artist.name.strip().lower())).limit(20)
    ).all()

    tracks = []
    for t in tracks_db:
        dur = t.duration or 0.0
        mins = int(dur // 60)
        secs = int(dur % 60)
        tracks.append({
            "id": t.id,
            "title": t.title,
            "artist_name": artist.name,
            "album_title": t.album_title,
            "album_id": t.album_id,
            "track_number": t.track_number or 1,
            "duration": dur,
            "duration_str": f"{mins}:{secs:02d}",
            "cover_url": f"/metadata/{t.cover_filename}" if t.cover_filename else None,
            "stream_url": f"/stream/music/{t.id}"
        })

    playlists = session.exec(select(Playlist).where(Playlist.user_id == current_user.id).order_by(Playlist.name.asc())).all()
    user_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()
    avail_vols = get_available_volumes(session)

    close_url = None
    if from_url:
        parsed_from = urllib.parse.urlparse(from_url)
        if not parsed_from.netloc or parsed_from.netloc == request.url.netloc:
            close_url = parsed_from.path + ("?" + parsed_from.query if parsed_from.query else "")

    if not close_url:
        referer = request.headers.get("referer")
        if referer:
            parsed = urllib.parse.urlparse(referer)
            if (not parsed.netloc or parsed.netloc == request.url.netloc) and not parsed.path.startswith(f"/artist/{artist_id}"):
                close_url = parsed.path + ("?" + parsed.query if parsed.query else "")

    if not close_url:
        close_url = "/library/music?view=artists"

    return templates.TemplateResponse(request=request, name="artist.html", context={
        "user": current_user,
        "current_profile": current_profile,
        "user_profiles": user_profiles,
        "artist": artist,
        "albums": albums,
        "tracks": tracks,
        "playlists": playlists,
        "close_url": close_url,
        "has_movies": avail_vols["movie"],
        "has_tv": avail_vols["tv"],
        "has_photos": avail_vols["photo"],
        "has_music": avail_vols["music"]
    })


@app.get("/playlist/{playlist_id}", response_class=HTMLResponse)
def playlist_view(
    request: Request,
    playlist_id: int,
    from_url: Optional[str] = Query(None, alias="from"),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    playlist = session.get(Playlist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if playlist.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")

    pl_tracks = session.exec(
        select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id).order_by(PlaylistTrack.track_order.asc())
    ).all()

    total_duration = 0.0
    tracks = []
    cover_url = None
    for item in pl_tracks:
        t = session.get(Track, item.track_id)
        if not t:
            continue
        dur = t.duration or 0.0
        total_duration += dur
        mins = int(dur // 60)
        secs = int(dur % 60)
        t_cover = f"/metadata/{t.cover_filename}" if t.cover_filename else None
        if not cover_url and t_cover:
            cover_url = t_cover
        tracks.append({
            "id": t.id,
            "playlist_track_id": item.id,
            "title": t.title,
            "artist_name": t.artist_name,
            "album_title": t.album_title,
            "album_id": t.album_id,
            "track_order": item.track_order,
            "duration": dur,
            "duration_str": f"{mins}:{secs:02d}",
            "cover_url": t_cover,
            "stream_url": f"/stream/music/{t.id}"
        })

    total_dur_str = format_seconds_to_duration(total_duration)
    playlists = session.exec(select(Playlist).where(Playlist.user_id == current_user.id).order_by(Playlist.name.asc())).all()
    user_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id).order_by(Profile.id.asc())).all()
    avail_vols = get_available_volumes(session)

    close_url = None
    if from_url:
        parsed_from = urllib.parse.urlparse(from_url)
        if not parsed_from.netloc or parsed_from.netloc == request.url.netloc:
            close_url = parsed_from.path + ("?" + parsed_from.query if parsed_from.query else "")

    if not close_url:
        referer = request.headers.get("referer")
        if referer:
            parsed = urllib.parse.urlparse(referer)
            if (not parsed.netloc or parsed.netloc == request.url.netloc) and not parsed.path.startswith(f"/playlist/{playlist_id}"):
                close_url = parsed.path + ("?" + parsed.query if parsed.query else "")

    if not close_url:
        close_url = "/library/music?view=playlists"

    return templates.TemplateResponse(request=request, name="playlist.html", context={
        "user": current_user,
        "current_profile": current_profile,
        "user_profiles": user_profiles,
        "playlist": playlist,
        "cover_url": cover_url,
        "tracks": tracks,
        "total_duration_str": total_dur_str,
        "total_tracks": len(tracks),
        "playlists": playlists,
        "close_url": close_url,
        "has_movies": avail_vols["movie"],
        "has_tv": avail_vols["tv"],
        "has_photos": avail_vols["photo"],
        "has_music": avail_vols["music"]
    })


# ----------------- MUSIC API ROUTES -----------------

@app.get("/api/music/track/{track_id}")
def api_track_info(
    track_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    track = session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    mins = int((track.duration or 0) // 60)
    secs = int((track.duration or 0) % 60)
    return {
        "id": track.id,
        "title": track.title,
        "artist_name": track.artist_name,
        "artist_id": track.artist_id,
        "album_title": track.album_title,
        "album_id": track.album_id,
        "track_number": track.track_number,
        "disc_number": track.disc_number,
        "duration": track.duration or 0,
        "duration_str": f"{mins}:{secs:02d}",
        "bitrate": track.bitrate,
        "codec": track.codec,
        "year": track.year,
        "genre": track.genre,
        "cover_url": f"/metadata/{track.cover_filename}" if track.cover_filename else None,
        "stream_url": f"/stream/music/{track.id}"
    }


@app.get("/api/music/album/{album_id}/tracks")
def api_album_tracks(
    album_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    tracks = session.exec(
        select(Track).where(Track.album_id == album_id).order_by(Track.disc_number.asc(), Track.track_number.asc(), Track.title.asc())
    ).all()
    track_list = []
    for t in tracks:
        mins = int((t.duration or 0) // 60)
        secs = int((t.duration or 0) % 60)
        track_list.append({
            "id": t.id,
            "title": t.title,
            "artist_name": t.artist_name or album.artist_name,
            "album_title": album.title,
            "album_id": album.id,
            "track_number": t.track_number,
            "disc_number": t.disc_number,
            "duration": t.duration or 0,
            "duration_str": f"{mins}:{secs:02d}",
            "bitrate": t.bitrate,
            "codec": t.codec,
            "cover_url": f"/metadata/{t.cover_filename or album.cover_filename}" if (t.cover_filename or album.cover_filename) else None,
            "stream_url": f"/stream/music/{t.id}"
        })
    return {
        "album": {
            "id": album.id,
            "title": album.title,
            "artist_name": album.artist_name,
            "artist_id": album.artist_id,
            "year": album.year,
            "genre": album.genre,
            "cover_url": f"/metadata/{album.cover_filename}" if album.cover_filename else None
        },
        "tracks": track_list
    }


@app.get("/api/music/artist/{artist_id}/tracks")
def api_artist_tracks(
    artist_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    artist = session.get(Artist, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    tracks = session.exec(
        select(Track).where((Track.artist_id == artist_id) | (func.lower(Track.artist_name) == artist.name.strip().lower())).limit(50)
    ).all()
    track_list = []
    for t in tracks:
        mins = int((t.duration or 0) // 60)
        secs = int((t.duration or 0) % 60)
        track_list.append({
            "id": t.id,
            "title": t.title,
            "artist_name": artist.name,
            "album_title": t.album_title,
            "album_id": t.album_id,
            "track_number": t.track_number,
            "duration": t.duration or 0,
            "duration_str": f"{mins}:{secs:02d}",
            "cover_url": f"/metadata/{t.cover_filename}" if t.cover_filename else None,
            "stream_url": f"/stream/music/{t.id}"
        })
    return track_list


@app.get("/api/playlists")
def api_get_playlists(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    playlists = session.exec(
        select(Playlist).where(Playlist.user_id == current_user.id).order_by(Playlist.name.asc())
    ).all()
    res = []
    for pl in playlists:
        count = len(session.exec(select(PlaylistTrack.id).where(PlaylistTrack.playlist_id == pl.id)).all())
        res.append({
            "id": pl.id,
            "name": pl.name,
            "track_count": count,
            "created_at": pl.created_at.isoformat() if pl.created_at else None
        })
    return res


@app.post("/api/playlists")
async def api_create_playlist(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        body = await request.json()
        name = (body.get("name") or "").strip()
    except Exception:
        name = ""
    if not name:
        form = await request.form()
        name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Playlist name is required")
    playlist = Playlist(name=name, user_id=current_user.id)
    session.add(playlist)
    session.commit()
    session.refresh(playlist)
    return {"success": True, "playlist": {"id": playlist.id, "name": playlist.name}}


@app.get("/api/playlists/{playlist_id}")
def api_get_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    playlist = session.get(Playlist, playlist_id)
    if not playlist or (playlist.user_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Playlist not found")
    pl_tracks = session.exec(
        select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id).order_by(PlaylistTrack.track_order.asc())
    ).all()
    track_list = []
    for item in pl_tracks:
        t = session.get(Track, item.track_id)
        if not t:
            continue
        mins = int((t.duration or 0) // 60)
        secs = int((t.duration or 0) % 60)
        track_list.append({
            "id": t.id,
            "playlist_track_id": item.id,
            "title": t.title,
            "artist_name": t.artist_name,
            "album_title": t.album_title,
            "album_id": t.album_id,
            "track_order": item.track_order,
            "duration": t.duration or 0,
            "duration_str": f"{mins}:{secs:02d}",
            "cover_url": f"/metadata/{t.cover_filename}" if t.cover_filename else None,
            "stream_url": f"/stream/music/{t.id}"
        })
    return {
        "id": playlist.id,
        "name": playlist.name,
        "tracks": track_list
    }


@app.delete("/api/playlists/{playlist_id}")
def api_delete_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    playlist = session.get(Playlist, playlist_id)
    if not playlist or (playlist.user_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Playlist not found")
    for pt in session.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id)).all():
        session.delete(pt)
    session.delete(playlist)
    session.commit()
    return {"success": True}


@app.post("/api/playlists/{playlist_id}/tracks")
async def api_add_track_to_playlist(
    playlist_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    playlist = session.get(Playlist, playlist_id)
    if not playlist or (playlist.user_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Playlist not found")

    track_id = None
    try:
        body = await request.json()
        track_id = int(body.get("track_id"))
    except Exception:
        form = await request.form()
        raw_tid = form.get("track_id")
        if raw_tid:
            track_id = int(raw_tid)

    if not track_id:
        raise HTTPException(status_code=400, detail="track_id is required")

    track = session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    max_order = session.exec(
        select(func.max(PlaylistTrack.track_order)).where(PlaylistTrack.playlist_id == playlist.id)
    ).first()
    next_order = (max_order + 1) if (max_order is not None) else 0

    item = PlaylistTrack(playlist_id=playlist.id, track_id=track.id, track_order=next_order)
    session.add(item)
    session.commit()
    session.refresh(item)
    return {"success": True, "playlist_track_id": item.id}


@app.delete("/api/playlists/{playlist_id}/tracks/{track_id}")
def api_remove_track_from_playlist(
    playlist_id: int,
    track_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.music_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    playlist = session.get(Playlist, playlist_id)
    if not playlist or (playlist.user_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Playlist not found")

    item = session.exec(
        select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id, PlaylistTrack.track_id == track_id)
    ).first()
    if not item:
        item = session.get(PlaylistTrack, track_id)
        if not item or item.playlist_id != playlist.id:
            raise HTTPException(status_code=404, detail="Track not found in playlist")

    session.delete(item)
    session.commit()
    return {"success": True}

@app.get("/play/{media_type}/{item_id}", response_class=HTMLResponse)
async def play_video(
    request: Request,
    media_type: str,
    item_id: int,
    sync: str = None,
    mobile: str = None,
    trailer: Optional[int] = None,
    restart: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if media_type == "movie" and not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    item = session.get(Movie if media_type == "movie" else TVShow, item_id)
    if not item: raise HTTPException(status_code=404, detail="Item not found")
    rating_level = get_profile_rating_level(current_user, current_profile)
    if rating_level > 0 and not is_rating_allowed(item.content_rating, rating_level):
        raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")

    is_trailer = bool(trailer)
    if is_trailer:
        trailer_source = None
        trailer_stream_url = None
        trailer_youtube_key = None

        # 1. Local trailer hoarder support (checked FIRST by default)
        local_trailer = find_local_trailer(item.file_path) if media_type == "movie" else None
        if local_trailer:
            trailer_source = "local"
            trailer_stream_url = f"/stream/trailer/movie/{item.id}"
            probe = probe_media_streams(local_trailer)
            duration = probe.get("duration") or 0.0
            audio_tracks = probe.get("audio_tracks", [])
        else:
            # 2. TMDB YouTube trailer
            trailer_key = item.trailer_key if hasattr(item, 'trailer_key') else None
            if not trailer_key:
                tmdb_config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
                tmdb_key = tmdb_config.value if tmdb_config else os.getenv("TMDB_API_KEY")
                if tmdb_key:
                    tmdb_id = getattr(item, 'tmdb_id', None)
                    if not tmdb_id and getattr(item, 'poster_filename', None):
                        m = re.search(r'movie_(\d+)_', item.poster_filename)
                        if m:
                            tmdb_id = int(m.group(1))
                    raw_client = httpx.AsyncClient(timeout=10.0) if httpx is not None else None
                    try:
                        client = SafeHTTPClient(raw_client)
                        if not tmdb_id:
                            s_res = await client.get("https://api.themoviedb.org/3/search/movie", params={"query": item.title, "api_key": tmdb_key})
                            if s_res.status_code == 200:
                                res_list = s_res.json().get("results", [])
                                if res_list:
                                    clean_title = re.sub(r'[^a-zA-Z0-9]', '', item.title).lower()
                                    match_item = next((r for r in res_list if re.sub(r'[^a-zA-Z0-9]', '', r.get("title", "")).lower() == clean_title), res_list[0])
                                    tmdb_id = match_item.get("id")
                        if tmdb_id:
                            item.tmdb_id = tmdb_id
                            v_res = await client.get(f"https://api.themoviedb.org/3/movie/{tmdb_id}/videos", params={"api_key": tmdb_key})
                            if v_res.status_code == 200:
                                videos = v_res.json().get("results", [])
                                trailer_key = extract_tmdb_trailer_key(videos)
                                if trailer_key:
                                    item.trailer_key = trailer_key
                                    session.add(item)
                                    session.commit()
                    except Exception as e:
                        print(f"[Player] TMDB trailer fetch error: {e}")

            if trailer_key:
                trailer_source = "youtube"
                trailer_youtube_key = trailer_key
                probe = {}
                duration = 0.0
                audio_tracks = []
            else:
                return RedirectResponse(url=f"/movie/{item.id}?no_trailer=1", status_code=303)

        ua = request.headers.get("user-agent", "")
        is_apple = any(k in ua for k in ["Macintosh", "Mac OS", "iPhone", "iPad", "iPod", "CFNetwork"])
        token = request.query_params.get("token") or (current_user.session_token if current_user else "")
        return templates.TemplateResponse(request=request, name="player.html", context={
            "item": item, "media_type": media_type, "history": None, "season_episode": "",
            "duration": duration, "next_episode_id": None, "prev_episode_id": None, "subtitle_url": None,
            "subtitle_tracks": [], "media_bitrate": probe.get("bitrate") if probe else None,
            "sync_session_id": None, "is_sync_host": False, "sync_initial_time": 0.0,
            "user": current_user, "current_profile": current_profile, "token": token, "is_mobile": mobile == "1",
            "is_apple": is_apple,
            "audio_tracks": audio_tracks,
            "default_audio_lang": (current_profile.default_audio_lang if (current_profile and current_profile.default_audio_lang) else (current_user.default_audio_lang if current_user else "eng")) or "eng",
            "default_subtitle_lang": (current_profile.default_subtitle_lang if (current_profile and current_profile.default_subtitle_lang) else (current_user.default_subtitle_lang if current_user else "eng")) or "eng",
            "is_trailer": True,
            "trailer_source": trailer_source,
            "trailer_stream_url": trailer_stream_url,
            "trailer_youtube_key": trailer_youtube_key
        })
        
    prof_id = current_profile.id if current_profile else None
    all_hist = session.exec(
        select(WatchHistory).where(
            WatchHistory.user_id == current_user.id,
            WatchHistory.item_id == item_id,
            WatchHistory.media_type == media_type
        ).order_by(WatchHistory.last_watched.desc())
    ).all()
    
    history_entry = None
    if all_hist:
        history_entry = next((h for h in all_hist if h.profile_id == prof_id), None)
        if not history_entry:
            history_entry = next((h for h in all_hist if h.profile_id is None), all_hist[0])
        # Purge any duplicate records for this item so only one persists
        for extra in all_hist:
            if extra.id != history_entry.id:
                session.delete(extra)
    
    if media_type == "movie":
        sibling_ids = get_sibling_movie_ids(session, item)
        sib_q = select(WatchHistory).where(
            WatchHistory.user_id == current_user.id,
            WatchHistory.media_type == "movie",
            WatchHistory.item_id.in_(sibling_ids),
            WatchHistory.progress > 0
        )
        if prof_id:
            sib_q = sib_q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
        latest_sib_hist = session.exec(sib_q.order_by(WatchHistory.last_watched.desc())).first()

        if not history_entry:
            history_entry = WatchHistory(user_id=current_user.id, profile_id=prof_id, media_type=media_type, item_id=item_id)
            if not restart and latest_sib_hist:
                history_entry.progress = latest_sib_hist.progress
            session.add(history_entry)
        else:
            if restart:
                history_entry.progress = 0.0
            elif latest_sib_hist and latest_sib_hist.progress > 0:
                history_entry.progress = latest_sib_hist.progress
            if prof_id and not history_entry.profile_id:
                history_entry.profile_id = prof_id
            history_entry.last_watched = datetime.now(timezone.utc)
            session.add(history_entry)
    else:
        if history_entry:
            if restart:
                history_entry.progress = 0.0
            if prof_id and not history_entry.profile_id:
                history_entry.profile_id = prof_id
            history_entry.last_watched = datetime.now(timezone.utc)
            session.add(history_entry)
        else:
            history_entry = WatchHistory(user_id=current_user.id, profile_id=prof_id, media_type=media_type, item_id=item_id)
            if restart:
                history_entry.progress = 0.0
            session.add(history_entry)
        
    for attempt in range(5):
        try:
            session.commit()
            break
        except Exception as e:
            if "locked" in str(e).lower() and attempt < 4:
                import time
                time.sleep(0.05 * (attempt + 1))
            else:
                session.rollback()
                break
    
    next_episode_id = None
    prev_episode_id = None

    season_episode = ""
    if media_type == "tv":
        m = re.search(r'S(\d+)E(\d+)', item.file_path, re.IGNORECASE)
        if m: 
            season_episode = f"S{int(m.group(1))} • E{int(m.group(2))}"
            filename = os.path.splitext(os.path.basename(item.file_path))[0]
            name_match = re.search(r'S\d+E\d+[ \-]*(.+)', filename, re.IGNORECASE)
            if name_match and name_match.group(1).strip():
                season_episode += f"<br>{name_match.group(1).strip()}"
                
        series_episodes = session.exec(select(TVShow).where(func.lower(TVShow.title) == item.title.strip().lower())).all()
        sorted_eps = sorted(series_episodes, key=get_tv_episode_sort_key)
        for i, ep in enumerate(sorted_eps):
            if ep.id == item_id:
                if i > 0:
                    prev_episode_id = sorted_eps[i-1].id
                if i + 1 < len(sorted_eps):
                    next_episode_id = sorted_eps[i+1].id
                break
        
    probe = probe_media_streams(item.file_path)
    audio_tracks = probe.get("audio_tracks", [])
    subtitle_tracks = probe.get("subtitle_tracks", [])

    subtitle_url = None
    base_path = os.path.splitext(item.file_path)[0]
    primary_sub = next((s for s in subtitle_tracks if s.get("is_text") and (s.get("is_default") or not s.get("is_forced"))), None)
    if not primary_sub:
        primary_sub = next((s for s in subtitle_tracks if s.get("is_text")), None)

    if primary_sub:
        subtitle_url = f"/subtitle/{media_type}/{item.id}?track_id={primary_sub['id']}"
    elif os.path.exists(base_path + ".srt"):
        subtitle_url = f"/subtitle/{media_type}/{item.id}?ext=srt"
    elif os.path.exists(base_path + ".vtt"):
        subtitle_url = f"/subtitle/{media_type}/{item.id}?ext=vtt"

    duration = probe.get("duration") or 0.0
    if not duration:
        try:
            cmd = [get_ffprobe_path(), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", item.file_path]
            out = subprocess.check_output(cmd).decode().strip()
            duration = float(out)
        except:
            pass

    if history_entry and duration > 0 and (not getattr(history_entry, 'duration', 0.0) or history_entry.duration <= 0):
        history_entry.duration = float(duration)
        session.add(history_entry)
        session.commit()

    # On-demand probe for chapters if not yet cached in DB
    if item.intro_start is None and item.credits_start is None:
        from .scanner import extract_chapter_markers
        intro_s, intro_e, cred_s, cred_e = extract_chapter_markers(item.file_path)
        if intro_s is not None or cred_s is not None:
            item.intro_start = intro_s
            item.intro_end = intro_e
            item.credits_start = cred_s
            item.credits_end = cred_e
            session.add(item)
            session.commit()

    token = request.query_params.get("token", "")
    is_sync_host = False
    sync_initial_time = 0.0
    rewind_buffer = (current_profile.rewind_seconds if current_profile and current_profile.rewind_seconds else current_user.rewind_seconds) or 0
    if sync:
        ws_session = session.get(WatchSession, sync)
        if not ws_session or not ws_session.is_active:
            return RedirectResponse(url="/?session_ended=1", status_code=303)
        if ws_session.host_profile_id and current_profile:
            is_sync_host = (ws_session.host_profile_id == current_profile.id)
        else:
            is_sync_host = (ws_session.host_id == current_user.id)
        if is_sync_host:
            host_start = 0.0
            if history_entry and duration and history_entry.progress > 0 and history_entry.progress < 100:
                host_start = (history_entry.progress / 100.0) * duration
                if rewind_buffer:
                    host_start = max(0.0, host_start - rewind_buffer)
            if sync not in manager.session_playback or manager.session_playback[sync].get("current_time", 0.0) == 0.0:
                manager.session_playback[sync] = {"current_time": host_start, "is_paused": True, "updated_at": time.time()}
            sync_initial_time = manager.session_playback[sync]["current_time"]
        else:
            sync_initial_time = manager.session_playback.get(sync, {}).get("current_time", 0.0)
            if sync_initial_time == 0.0:
                if ws_session.host_profile_id:
                    host_hist = session.exec(select(WatchHistory).where(WatchHistory.profile_id == ws_session.host_profile_id).where(WatchHistory.item_id == item_id).where(WatchHistory.media_type == media_type)).first()
                else:
                    host_hist = session.exec(select(WatchHistory).where(WatchHistory.user_id == ws_session.host_id).where(WatchHistory.item_id == item_id).where(WatchHistory.media_type == media_type)).first()
                if host_hist and duration and host_hist.progress > 0:
                    sync_initial_time = (host_hist.progress / 100.0) * duration
                    if sync not in manager.session_playback:
                        manager.session_playback[sync] = {"current_time": sync_initial_time, "is_paused": True, "updated_at": time.time()}

    # Register active stream telemetry immediately
    session_key = f"{current_user.id}_{prof_id or 0}_{media_type}_{item_id}"
    item_title = item.title
    item_poster = getattr(item, 'season_poster_filename', None) or getattr(item, 'poster_filename', None)
    season_ep_clean = season_episode.replace("<br>", " - ") if season_episode else ""
    client_ip = request.client.host if request.client else ""
    display_username = f"{current_user.username} ({current_profile.name})" if (current_profile and current_profile.name != current_user.username) else current_user.username
    active_stream_manager.register_or_update(
        session_key=session_key,
        user_id=current_user.id,
        username=display_username,
        media_type=media_type,
        item_id=item_id,
        title=item_title,
        poster_filename=item_poster,
        progress=history_entry.progress if history_entry and history_entry.progress else 0.0,
        current_time=0.0,
        duration=duration or 0.0,
        play_type="Direct Play",
        season_episode=season_ep_clean,
        ip=client_ip
    )

    ua = request.headers.get("user-agent", "")
    is_apple = any(k in ua for k in ["Macintosh", "Mac OS", "iPhone", "iPad", "iPod", "CFNetwork"])
    active_token = token or (current_user.session_token if current_user else "")

    hdr_cfg = session.exec(select(SystemConfig).where(SystemConfig.key == "hdr_tonemapping")).first()
    server_hdr_tonemapping = (hdr_cfg.value == "true") if hdr_cfg else True

    # Asynchronously queue trickplay scrubbing thumbnails if not yet generated
    if not is_trailer and item.file_path and os.path.exists(item.file_path):
        queue_trickplay_generation(item.file_path, media_type, item.id, duration)

    return templates.TemplateResponse(request=request, name="player.html", context={
        "item": item, "media_type": media_type, "history": history_entry, "season_episode": season_episode,
        "duration": duration, "next_episode_id": next_episode_id, "prev_episode_id": prev_episode_id, "subtitle_url": subtitle_url,
        "subtitle_tracks": subtitle_tracks, "media_bitrate": probe.get("bitrate"),
        "is_hdr": probe.get("is_hdr", False), "hdr_format": probe.get("hdr_format", "HDR"),
        "server_hdr_tonemapping": server_hdr_tonemapping,
        "sync_session_id": sync, "is_sync_host": is_sync_host, "sync_initial_time": sync_initial_time,
        "user": current_user, "current_profile": current_profile, "token": active_token, "is_mobile": mobile == "1",
        "is_apple": is_apple,
        "audio_tracks": audio_tracks, "is_restart": bool(restart),
        "default_audio_lang": (current_profile.default_audio_lang if (current_profile and current_profile.default_audio_lang) else (current_user.default_audio_lang if current_user else "eng")) or "eng",
        "default_subtitle_lang": (current_profile.default_subtitle_lang if (current_profile and current_profile.default_subtitle_lang) else (current_user.default_subtitle_lang if current_user else "eng")) or "eng"
    })

@app.get("/subtitle/{media_type}/{item_id}")
def get_subtitle(
    media_type: str,
    item_id: int,
    track_id: Optional[str] = Query(None),
    start: float = Query(0.0),
    res: str = Query("original"),
    ext: Optional[str] = Query(None),
    stream_idx: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
):
    if not current_user:
        return Response(status_code=403)
    with Session(engine) as session:
        item = session.get(Movie if media_type == "movie" else TVShow, item_id)
        if not item or not os.path.exists(item.file_path):
            return Response(status_code=404)
        file_path = item.file_path
        content_rating = item.content_rating

    rating_level = get_profile_rating_level(current_user, current_profile)
    if rating_level > 0 and not is_rating_allowed(content_rating, rating_level):
        return Response(status_code=403)

    if stream_idx is not None and not track_id:
        track_id = f"stream:{stream_idx}"
    if track_id == "legacy":
        track_id = None

    probe = probe_media_streams(file_path)

    # Frame-accurate sync:
    # When video is direct-streamed (-c:v copy), FFmpeg snaps to the keyframe at or before start.
    # Align the subtitle shift to that exact keyframe timestamp so subtitles sync perfectly with video & audio.
    if start > 0.0:
        can_direct_stream_video = (res == "original" and probe.get("can_direct_stream_video", False))
        if can_direct_stream_video:
            start = get_keyframe_before_time(file_path, start)

    # Legacy fallback: ext query parameter without track_id
    if not track_id and ext:
        sub_file = os.path.splitext(file_path)[0] + "." + ext
        if not os.path.exists(sub_file):
            return Response(status_code=404)
        if ext == "srt":
            try:
                with open(sub_file, "r", encoding="utf-8", errors="ignore") as f:
                    srt_content = f.read()
                srt_content = srt_content.replace("\r\n", "\n").replace("\r", "\n")
                vtt_content = "WEBVTT\n\n" + re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', srt_content)
                if start > 0.0:
                    vtt_content = shift_vtt_timestamps(vtt_content, start)
                return Response(content=vtt_content, media_type="text/vtt")
            except Exception:
                return Response(status_code=500)
        else:
            if start > 0.0:
                with open(sub_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                return Response(content=shift_vtt_timestamps(content, start), media_type="text/vtt")
            return FileResponse(sub_file, media_type="text/vtt")

    # If no track_id and no ext, fallback to first text track
    if not track_id:
        sub_tracks = probe.get("subtitle_tracks", [])
        first_text = next((s for s in sub_tracks if s.get("is_text")), None)
        if first_text:
            track_id = first_text["id"]
        else:
            return Response(status_code=404)

    # 1. External sidecar subtitle
    if track_id.startswith("sidecar:"):
        filename = track_id.split("sidecar:", 1)[1]
        # Prevent directory traversal
        if os.path.basename(filename) != filename:
            return Response(status_code=400)
        dir_name = os.path.dirname(file_path)
        sub_file = os.path.join(dir_name, filename)
        if not os.path.exists(sub_file):
            return Response(status_code=404)
        if sub_file.lower().endswith(".srt"):
            try:
                with open(sub_file, "r", encoding="utf-8", errors="ignore") as f:
                    srt_content = f.read()
                srt_content = srt_content.replace("\r\n", "\n").replace("\r", "\n")
                vtt_content = "WEBVTT\n\n" + re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', srt_content)
                vtt_content = sanitize_vtt_content(vtt_content)
                if start > 0.0:
                    vtt_content = shift_vtt_timestamps(vtt_content, start)
                return Response(content=vtt_content, media_type="text/vtt")
            except Exception:
                return Response(status_code=500)
        else:
            try:
                with open(sub_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                content = sanitize_vtt_content(content)
                if start > 0.0:
                    content = shift_vtt_timestamps(content, start)
                return Response(content=content, media_type="text/vtt")
            except Exception:
                return Response(status_code=500)

    # 2. Embedded subtitle stream
    if track_id.startswith("stream:") or track_id.isdigit():
        try:
            s_idx = int(track_id.replace("stream:", ""))
        except ValueError:
            return Response(status_code=400)

        cache_dir = get_subtitle_cache_dir()
        cached_vtt = os.path.join(cache_dir, f"{media_type}_{item_id}_stream{s_idx}.vtt")

        # Cache validation: Check if file exists, non-empty, and newer than media file
        is_cached_valid = False
        if os.path.exists(cached_vtt) and os.path.getsize(cached_vtt) > 0:
            try:
                media_mtime = os.path.getmtime(file_path)
                cached_mtime = os.path.getmtime(cached_vtt)
                if cached_mtime >= media_mtime:
                    is_cached_valid = True
            except Exception:
                is_cached_valid = True

        if is_cached_valid:
            if start > 0.0:
                with open(cached_vtt, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                return Response(content=shift_vtt_timestamps(content, start), media_type="text/vtt")
            return FileResponse(cached_vtt, media_type="text/vtt")

        # Extract on demand via FFmpeg
        success = extract_subtitle_to_vtt(file_path, s_idx, cached_vtt)
        if success and os.path.exists(cached_vtt) and os.path.getsize(cached_vtt) > 0:
            if start > 0.0:
                with open(cached_vtt, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                return Response(content=shift_vtt_timestamps(content, start), media_type="text/vtt")
            return FileResponse(cached_vtt, media_type="text/vtt")
        else:
            return Response(status_code=500, content="Failed to extract subtitle track")

    return Response(status_code=400, content="Invalid track identifier")

@app.post("/progress/{media_type}/{item_id}")
async def update_progress(
    request: Request,
    media_type: str,
    item_id: int,
    progress: float = Form(...),
    current_time: Optional[float] = Form(None),
    duration: Optional[float] = Form(None),
    play_type: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return {"status": "unauthorized"}
    
    prof_id = current_profile.id if current_profile else None
    session_key = f"{current_user.id}_{prof_id or 0}_{media_type}_{item_id}"
    
    sibling_ids = [item_id]
    if media_type == "movie":
        m_item = session.get(Movie, item_id)
        if m_item:
            sibling_ids = get_sibling_movie_ids(session, m_item)

    dur_val = float(duration) if duration is not None else 0.0
    cur_time_val = float(current_time) if current_time is not None else 0.0

    if progress >= 95.0:
        active_stream_manager.stop_stream(session_key)
        for t_id in sibling_ids:
            all_entries = session.exec(
                select(WatchHistory).where(
                    WatchHistory.user_id == current_user.id,
                    WatchHistory.item_id == t_id,
                    WatchHistory.media_type == media_type
                ).order_by(WatchHistory.last_watched.desc())
            ).all()
            entry = None
            if all_entries:
                entry = next((e for e in all_entries if e.profile_id == prof_id), None)
                if not entry:
                    entry = next((e for e in all_entries if e.profile_id is None), all_entries[0])
                for extra in all_entries:
                    if extra.id != entry.id:
                        session.delete(extra)

            t_dur = dur_val
            if media_type == "movie" and t_id != item_id:
                t_m = session.get(Movie, t_id)
                if t_m and t_m.runtime:
                    t_dur = float(t_m.runtime * 60)
            if entry:
                entry.progress = 100.0
                if t_dur > 0: entry.duration = t_dur
                entry.current_time = t_dur if t_dur > 0 else cur_time_val
                entry.last_watched = datetime.now(timezone.utc)
                if prof_id: entry.profile_id = prof_id
                session.add(entry)
            else:
                entry = WatchHistory(
                    user_id=current_user.id,
                    profile_id=prof_id,
                    media_type=media_type,
                    item_id=t_id,
                    progress=100.0,
                    current_time=t_dur if t_dur > 0 else cur_time_val,
                    duration=t_dur,
                    last_watched=datetime.now(timezone.utc)
                )
                session.add(entry)

        if media_type == "tv":
            cur_tv = session.get(TVShow, item_id)
            if cur_tv:
                next_ep = get_next_uncompleted_episode(session, cur_tv, current_user.id, prof_id)
                if next_ep:
                    q_next = select(WatchHistory).where(
                        WatchHistory.user_id == current_user.id,
                        WatchHistory.media_type == "tv",
                        WatchHistory.item_id == next_ep.id
                    )
                    if prof_id:
                        q_next = q_next.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
                    next_hist = session.exec(q_next.order_by(WatchHistory.last_watched.desc())).first()
                    now_ts = datetime.now(timezone.utc)
                    if next_hist:
                        if next_hist.progress < 95.0:
                            next_hist.last_watched = now_ts
                            session.add(next_hist)
                    else:
                        new_next = WatchHistory(
                            user_id=current_user.id,
                            profile_id=prof_id,
                            media_type="tv",
                            item_id=next_ep.id,
                            progress=0.0,
                            current_time=0.0,
                            duration=float(next_ep.runtime * 60) if next_ep.runtime else 0.0,
                            last_watched=now_ts
                        )
                        session.add(new_next)

        for attempt in range(5):
            try:
                session.commit()
                break
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 4:
                    import asyncio
                    await asyncio.sleep(0.05 * (attempt + 1))
                else:
                    session.rollback()
                    if "locked" in str(e).lower():
                        return {"status": "busy"}
                    raise
        return {"status": "completed"}

    # Progress < 95.0 (In Progress)
    for t_id in sibling_ids:
        all_entries = session.exec(
            select(WatchHistory).where(
                WatchHistory.user_id == current_user.id,
                WatchHistory.item_id == t_id,
                WatchHistory.media_type == media_type
            ).order_by(WatchHistory.last_watched.desc())
        ).all()
        entry = None
        if all_entries:
            entry = next((e for e in all_entries if e.profile_id == prof_id), None)
            if not entry:
                entry = next((e for e in all_entries if e.profile_id is None), all_entries[0])
            for extra in all_entries:
                if extra.id != entry.id:
                    session.delete(extra)

        t_dur = dur_val
        if media_type == "movie" and t_id != item_id:
            t_m = session.get(Movie, t_id)
            if t_m and t_m.runtime:
                t_dur = float(t_m.runtime * 60)
        t_time = cur_time_val
        if media_type == "movie" and t_id != item_id and t_dur > 0:
            t_time = (progress / 100.0) * t_dur

        if entry:
            entry.progress = progress
            if current_time is not None:
                entry.current_time = t_time
            if duration is not None and t_dur > 0:
                entry.duration = t_dur
            if prof_id and not entry.profile_id:
                entry.profile_id = prof_id
            entry.last_watched = datetime.now(timezone.utc)
            session.add(entry)
        else:
            entry = WatchHistory(
                user_id=current_user.id,
                profile_id=prof_id,
                media_type=media_type,
                item_id=t_id,
                progress=progress,
                current_time=t_time,
                duration=t_dur,
                last_watched=datetime.now(timezone.utc)
            )
            session.add(entry)

    for attempt in range(5):
        try:
            session.commit()
            break
        except Exception as e:
            if "locked" in str(e).lower() and attempt < 4:
                import asyncio
                await asyncio.sleep(0.05 * (attempt + 1))
            else:
                session.rollback()
                if "locked" in str(e).lower():
                    return {"status": "busy"}
                raise

    # Track active stream
    title = "Unknown"
    poster = None
    season_episode = ""
    if media_type == "movie":
        m = session.get(Movie, item_id)
        if m:
            title = m.title
            poster = m.poster_filename
    elif media_type == "tv":
        t = session.get(TVShow, item_id)
        if t:
            title = t.title
            poster = t.season_poster_filename or t.poster_filename
            match = re.search(r'S(\d+)E(\d+)', t.file_path, re.IGNORECASE)
            if match:
                season_episode = f"S{int(match.group(1))} • E{int(match.group(2))}"
                
    client_ip = request.client.host if request.client else ""
    display_username = f"{current_user.username} ({current_profile.name})" if (current_profile and current_profile.name != current_user.username) else current_user.username
    active_stream_manager.register_or_update(
        session_key=session_key,
        user_id=current_user.id,
        username=display_username,
        media_type=media_type,
        item_id=item_id,
        title=title,
        poster_filename=poster,
        progress=progress,
        current_time=current_time or 0.0,
        duration=duration or 0.0,
        play_type=play_type or "Direct Play",
        season_episode=season_episode,
        ip=client_ip
    )
    return {"status": "success"}

@app.post("/progress/{media_type}/{item_id}/stop")
async def stop_progress(
    media_type: str,
    item_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if current_user:
        prof_id = current_profile.id if current_profile else 0
        session_key = f"{current_user.id}_{prof_id}_{media_type}_{item_id}"
        active_stream_manager.stop_stream(session_key)
        hls_manager.stop_session(media_type, item_id)
    return {"status": "stopped"}

from fastapi.responses import StreamingResponse

@app.get("/stream/{media_type}/{item_id}")
def stream_video(
    media_type: str,
    item_id: int,
    res: str = "original",
    start: float = 0.0,
    audio_track: int = 0,
    burn_sub: Optional[str] = None,
    tonemap: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if media_type == "movie" and not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    with Session(engine) as session:
        item = session.get(Movie if media_type == "movie" else TVShow, item_id)
        if not item or not os.path.exists(item.file_path): raise HTTPException(status_code=404, detail="Video file missing")
        rating_level = get_profile_rating_level(current_user, current_profile)
        if rating_level > 0 and not is_rating_allowed(getattr(item, 'content_rating', ''), rating_level):
            raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")
        hw_config = session.exec(select(SystemConfig).where(SystemConfig.key == "hw_accel_profile")).first()
        hw_profile = hw_config.value if hw_config else "auto"
        hdr_cfg = session.exec(select(SystemConfig).where(SystemConfig.key == "hdr_tonemapping")).first()
        raw_hdr = str(hdr_cfg.value).strip().lower() if hdr_cfg else "hable"
        if raw_hdr in ("disabled", "false", "0", "off"):
            server_hdr_enabled = False
            server_tonemap_algo = "hable"
        else:
            server_hdr_enabled = True
            server_tonemap_algo = raw_hdr if raw_hdr in ("hable", "mobius", "reinhard", "linear", "gamma", "clip") else "hable"
        file_path = item.file_path

    file_ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    is_mp4 = (file_ext in ["mp4", "m4v"])
    probe = probe_media_streams(file_path)
    is_hdr = probe.get("is_hdr", False)
    enable_tonemap = (tonemap == 1) if tonemap is not None else server_hdr_enabled
    tonemap_active = bool(is_hdr and enable_tonemap)
    tonemap_algo = server_tonemap_algo

    audio_tracks = probe.get("audio_tracks", [])
    subtitle_tracks = probe.get("subtitle_tracks", [])

    sub_idx = parse_burn_sub_index(burn_sub, subtitle_tracks)
    burn_sub_active = (sub_idx is not None)
    burn_sub_track = next((s for s in subtitle_tracks if s.get("stream_index") == sub_idx), None) if burn_sub_active else None

    selected_track = None
    if 0 <= audio_track < len(audio_tracks):
        selected_track = audio_tracks[audio_track]
    elif audio_tracks:
        selected_track = audio_tracks[0]
        audio_track = 0

    effective_res = "original" if res in ("original", "auto") else res
    track_can_copy = selected_track.get("can_direct_stream", False) if selected_track else False
    track_label = selected_track.get("label", f"Track {audio_track + 1}") if selected_track else "None"

    # 1. Native Direct Play:
    # Requires original resolution, MP4 container, start at 0:00, primary audio track (audio_track == 0),
    # web-compatible video & audio codecs, NO subtitle burn-in requested, and not tone-mapping HDR.
    is_direct_play = (
        not burn_sub_active and
        not tonemap_active and
        effective_res == "original" and
        is_mp4 and
        start == 0.0 and
        audio_track == 0 and
        probe.get("can_direct_stream_video", True) and
        track_can_copy
    )
    if is_direct_play:
        _, smart_audio = get_smart_audio_pipeline(
            selected_track=selected_track,
            can_direct_stream_video=True,
            is_direct_play=True,
            video_codec=probe.get("video_codec"),
            video_encoder="none"
        )
        print(
            f"[Smart Audio Direct Stream] {smart_audio['state']} | "
            f"Media: {media_type} #{item_id} | "
            f"Video: {smart_audio['video_mode']} | "
            f"Audio: {smart_audio['source_spec']} -> {smart_audio['target_spec']}"
        )
        headers = {
            "X-Transcode-Active": "false",
            "X-Transcode-Method": "Direct Play",
            "X-Hardware-Accelerated": "false",
            "X-Encoder": "none",
            "X-Play-Type": "direct_play",
            "X-Audio-Track": str(audio_track),
            "X-Burn-Sub": "none",
            "X-Tone-Mapping": "off",
            "X-Smart-Audio-Active": "false",
            "X-Smart-Audio-State": smart_audio["state"],
        }
        return FileResponse(file_path, media_type="video/mp4", headers=headers)

    # 2. Smart Remuxing & Smart Audio Codec Direct Streaming:
    # When video codec is already web-compatible (H.264), native resolution is requested,
    # no subtitle burn-in is active, and HDR is not being tone mapped to SDR.
    can_direct_stream_video = (not burn_sub_active and not tonemap_active and effective_res == "original" and probe.get("can_direct_stream_video", False))

    actual_start = start
    if start > 0.0 and can_direct_stream_video:
        actual_start = get_keyframe_before_time(file_path, start)

    cmd = [get_ffmpeg_path()]
    if start > 0.0:
        cmd.extend(["-ss", str(start)])
    if start > 0.0 and can_direct_stream_video:
        cmd.append("-noaccurate_seek")
    cmd.extend(["-i", file_path])
    if start > 0.0 and can_direct_stream_video:
        cmd.extend(["-avoid_negative_ts", "make_zero"])

    if can_direct_stream_video:
        audio_args, smart_audio = get_smart_audio_pipeline(
            selected_track=selected_track,
            can_direct_stream_video=True,
            is_direct_play=False,
            video_codec=probe.get("video_codec"),
            video_encoder="copy"
        )
        cmd.extend(["-map", "0:v:0"])
        if selected_track:
            cmd.extend(["-map", f"0:a:{audio_track}"])
        cmd.extend(["-sn", "-map_chapters", "-1"])

        cmd.extend(["-c:v", "copy"])
        cmd.extend(audio_args)
        is_transcode = False
        hw_accel = False

        if not selected_track:
            transcode_method = "Direct Stream (Video Copy, No Audio)"
            encoder_type = "copy"
            play_type = "remux"
        elif track_can_copy:
            transcode_method = f"Direct Stream / Remux (Stream Copy, Audio: {track_label})"
            encoder_type = "copy"
            play_type = "remux"
        else:
            transcode_method = f"Smart Audio Direct Stream (Video Copy, Audio: {smart_audio['source_spec_short']} -> {smart_audio['target_spec_short']})"
            encoder_type = "copy (smart audio: aac)"
            play_type = "smart_audio_direct_stream"

        cmd.extend([
            "-max_muxing_queue_size", "1024",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1"
        ])
    else:
        # 3. Full Video Transcoding:
        # Required when downscaling resolution, when source video codec is incompatible,
        # when burning in bitmap/PGS subtitles, or when tone mapping HDR content to SDR.
        pipeline_args, v_map, encoder_type = get_video_transcode_pipeline(
            res=effective_res,
            profile=hw_profile,
            burn_sub_stream=sub_idx,
            is_hdr=is_hdr,
            enable_tonemap=enable_tonemap,
            color_transfer=probe.get("color_transfer"),
            tonemap_algo=tonemap_algo
        )
        audio_args, smart_audio = get_smart_audio_pipeline(
            selected_track=selected_track,
            can_direct_stream_video=False,
            is_direct_play=False,
            video_codec=probe.get("video_codec"),
            video_encoder=encoder_type
        )
        cmd.extend(pipeline_args)
        cmd.extend(["-map", v_map])
        if selected_track:
            cmd.extend(["-map", f"0:a:{audio_track}"])
        cmd.extend(audio_args)
        cmd.extend(["-sn", "-map_chapters", "-1"])
            
        cmd.extend([
            "-max_muxing_queue_size", "1024",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov",
            "pipe:1"
        ])
        is_transcode = True
        hw_accel = (encoder_type != "software")
        method_names = {
            "nvenc": "NVIDIA NVENC (h264_nvenc)",
            "qsv": "Intel QuickSync (h264_qsv)",
            "vaapi": "VA-API (h264_vaapi)",
            "videotoolbox": "Apple VideoToolbox (h264_videotoolbox)",
            "software": "Software CPU (libx264)"
        }
        base_method = method_names.get(encoder_type, encoder_type)
        if tonemap_active:
            base_method = f"{base_method} [HDR Tone Map ({tonemap_algo})]"
        if burn_sub_active:
            sub_label = burn_sub_track.get("label", f"Track #{sub_idx}") if burn_sub_track else f"Track #{sub_idx}"
            transcode_method = f"{base_method} [Burn-in: {sub_label}] (Audio: {track_label})"
        else:
            transcode_method = f"{base_method} (Audio: {track_label})"
        play_type = "transcode"

    print(
        f"[Smart Audio Direct Stream] {smart_audio['state']} | "
        f"Media: {media_type} #{item_id} | "
        f"Video: {smart_audio['video_mode']} | "
        f"Audio: {smart_audio['source_spec']} -> {smart_audio['target_spec']} | "
        f"Flags: {smart_audio['ffmpeg_flags']}"
    )

    headers = {
        "X-Transcode-Active": "true" if is_transcode else "false",
        "X-Transcode-Method": transcode_method,
        "X-Hardware-Accelerated": "true" if hw_accel else "false",
        "X-Encoder": encoder_type,
        "X-Play-Type": play_type,
        "X-Audio-Track": str(audio_track),
        "X-Burn-Sub": str(sub_idx) if burn_sub_active else "none",
        "X-Tone-Mapping": f"active ({tonemap_algo})" if tonemap_active else "off",
        "X-Actual-Start": str(actual_start),
        "X-Smart-Audio-Active": "true" if smart_audio["active"] else "false",
        "X-Smart-Audio-State": smart_audio["state"],
    }

    def generate():
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            process.kill()
            
    return StreamingResponse(generate(), media_type="video/mp4", headers=headers)

@app.get("/stream-info/{media_type}/{item_id}")
def get_stream_info(
    media_type: str,
    item_id: int,
    res: str = "original",
    start: float = 0.0,
    audio_track: int = 0,
    burn_sub: Optional[str] = None,
    tonemap: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if media_type == "movie" and not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    with Session(engine) as session:
        item = session.get(Movie if media_type == "movie" else TVShow, item_id)
        if not item or not os.path.exists(item.file_path): raise HTTPException(status_code=404, detail="Video file missing")
        rating_level = get_profile_rating_level(current_user, current_profile)
        if rating_level > 0 and not is_rating_allowed(getattr(item, 'content_rating', ''), rating_level):
            raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")
        hw_config = session.exec(select(SystemConfig).where(SystemConfig.key == "hw_accel_profile")).first()
        hw_profile = hw_config.value if hw_config else "auto"
        hdr_cfg = session.exec(select(SystemConfig).where(SystemConfig.key == "hdr_tonemapping")).first()
        raw_hdr = str(hdr_cfg.value).strip().lower() if hdr_cfg else "hable"
        if raw_hdr in ("disabled", "false", "0", "off"):
            server_hdr_enabled = False
            server_tonemap_algo = "hable"
        else:
            server_hdr_enabled = True
            server_tonemap_algo = raw_hdr if raw_hdr in ("hable", "mobius", "reinhard", "linear", "gamma", "clip") else "hable"
        file_path = item.file_path
        item_title = item.title

    file_ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    is_mp4 = (file_ext in ["mp4", "m4v"])
    probe = probe_media_streams(file_path)
    is_hdr = probe.get("is_hdr", False)
    enable_tonemap = (tonemap == 1) if tonemap is not None else server_hdr_enabled
    tonemap_active = bool(is_hdr and enable_tonemap)
    tonemap_algo = server_tonemap_algo

    audio_tracks = probe.get("audio_tracks", [])
    subtitle_tracks = probe.get("subtitle_tracks", [])

    sub_idx = parse_burn_sub_index(burn_sub, subtitle_tracks)
    burn_sub_active = (sub_idx is not None)
    burn_sub_track = next((s for s in subtitle_tracks if s.get("stream_index") == sub_idx), None) if burn_sub_active else None

    selected_track = None
    if 0 <= audio_track < len(audio_tracks):
        selected_track = audio_tracks[audio_track]
    elif audio_tracks:
        selected_track = audio_tracks[0]
        audio_track = 0

    effective_res = "original" if res in ("original", "auto") else res
    track_can_copy = selected_track.get("can_direct_stream", False) if selected_track else False
    track_label = selected_track.get("label", f"Track {audio_track + 1}") if selected_track else "None"

    can_direct_stream_video = (not burn_sub_active and not tonemap_active and effective_res == "original" and probe.get("can_direct_stream_video", False))
    is_direct_play = (
        not burn_sub_active and
        not tonemap_active and
        effective_res == "original" and
        is_mp4 and
        start == 0.0 and
        audio_track == 0 and
        probe.get("can_direct_stream_video", True) and
        track_can_copy
    )

    if is_direct_play:
        is_transcode = False
        method = "Direct Play"
        encoder = "none"
        hw_accel = False
        play_type = "direct_play"
        _, smart_audio = get_smart_audio_pipeline(
            selected_track=selected_track,
            can_direct_stream_video=True,
            is_direct_play=True,
            video_codec=probe.get("video_codec"),
            video_encoder="none"
        )
    elif can_direct_stream_video:
        is_transcode = False
        hw_accel = False
        _, smart_audio = get_smart_audio_pipeline(
            selected_track=selected_track,
            can_direct_stream_video=True,
            is_direct_play=False,
            video_codec=probe.get("video_codec"),
            video_encoder="copy"
        )
        if not selected_track:
            method = "Direct Stream (Video Copy, No Audio)"
            encoder = "copy"
            play_type = "remux"
        elif track_can_copy:
            method = f"Direct Stream / Remux (Stream Copy, Audio: {track_label})"
            encoder = "copy"
            play_type = "remux"
        else:
            method = f"Smart Audio Direct Stream (Video Copy, Audio: {smart_audio['source_spec_short']} -> {smart_audio['target_spec_short']})"
            encoder = "copy (smart audio: aac)"
            play_type = "smart_audio_direct_stream"
    else:
        encoder, _ = get_working_encoder(profile=hw_profile)
        is_transcode = True
        hw_accel = (encoder != "software")
        _, smart_audio = get_smart_audio_pipeline(
            selected_track=selected_track,
            can_direct_stream_video=False,
            is_direct_play=False,
            video_codec=probe.get("video_codec"),
            video_encoder=encoder
        )
        method_names = {
            "nvenc": "NVIDIA NVENC (h264_nvenc)",
            "qsv": "Intel QuickSync (h264_qsv)",
            "vaapi": "VA-API (h264_vaapi)",
            "videotoolbox": "Apple VideoToolbox (h264_videotoolbox)",
            "software": "Software CPU (libx264)"
        }
        base_method = method_names.get(encoder, encoder)
        if tonemap_active:
            base_method = f"{base_method} [HDR Tone Map ({tonemap_algo})]"
        if burn_sub_active:
            sub_label = burn_sub_track.get("label", f"Track #{sub_idx}") if burn_sub_track else f"Track #{sub_idx}"
            method = f"{base_method} [Burn-in: {sub_label}] (Audio: {track_label})"
        else:
            method = f"{base_method} (Audio: {track_label})"
        play_type = "transcode"

    actual_start = start
    if start > 0.0 and can_direct_stream_video:
        actual_start = get_keyframe_before_time(file_path, start)

    return {
        "media_type": media_type,
        "item_id": item_id,
        "title": item_title,
        "resolution": effective_res,
        "start": start,
        "actual_start": actual_start,
        "audio_track": audio_track,
        "audio_tracks": audio_tracks,
        "selected_audio_track": selected_track,
        "smart_audio": smart_audio,
        "burn_sub": sub_idx,
        "burn_sub_active": burn_sub_active,
        "is_transcoding": is_transcode,
        "transcode_method": method,
        "encoder": encoder,
        "hardware_accelerated": hw_accel,
        "play_type": play_type,
        "container": file_ext,
        "video_codec": probe.get("video_codec"),
        "is_hdr": is_hdr,
        "hdr_format": probe.get("hdr_format"),
        "tone_mapping_enabled": enable_tonemap,
        "tone_mapping_active": tonemap_active,
        "tone_mapping_algo": tonemap_algo,
        "bitrate": probe.get("bitrate"),
        "width": probe.get("width"),
        "height": probe.get("height"),
        "audio_codec": selected_track.get("codec") if selected_track else probe.get("audio_codec"),
        "audio_channels": selected_track.get("channels") if selected_track else probe.get("audio_channels"),
        "hw_profile": hw_profile,
        "server_time": time.time()
    }

# -------------------------------------------------------------
# HLS Adaptive Bitrate (ABR) Endpoints
# -------------------------------------------------------------
@app.get("/hls/{media_type}/{item_id}/master.m3u8")
def get_hls_master(
    request: Request,
    media_type: str,
    item_id: int,
    audio_track: int = 0,
    burn_sub: Optional[str] = None,
    tonemap: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if media_type == "movie" and not current_user.movies_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    with Session(engine) as session:
        item = session.get(Movie if media_type == "movie" else TVShow, item_id)
        if not item or not os.path.exists(item.file_path):
            raise HTTPException(status_code=404, detail="Media file missing")
        rating_level = get_profile_rating_level(current_user, current_profile)
        if rating_level > 0 and not is_rating_allowed(getattr(item, 'content_rating', ''), rating_level):
            raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")
        file_path = item.file_path
        if tonemap is None:
            hdr_cfg = session.exec(select(SystemConfig).where(SystemConfig.key == "hdr_tonemapping")).first()
            raw_hdr = str(hdr_cfg.value).strip().lower() if hdr_cfg else "hable"
            if raw_hdr in ("disabled", "false", "0", "off"):
                tonemap = 0
            else:
                tonemap = 1

    token = request.query_params.get("token")
    content = hls_manager.build_master_playlist(
        media_type=media_type,
        item_id=item_id,
        file_path=file_path,
        audio_track=audio_track,
        burn_sub=burn_sub,
        token=token,
        tonemap=tonemap
    )
    return Response(content=content, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache"})

@app.get("/hls/{media_type}/{item_id}/variant_{res}.m3u8")
def get_hls_variant(
    request: Request,
    media_type: str,
    item_id: int,
    res: str,
    audio_track: int = 0,
    burn_sub: Optional[str] = None,
    tonemap: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if media_type == "movie" and not current_user.movies_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    with Session(engine) as session:
        item = session.get(Movie if media_type == "movie" else TVShow, item_id)
        if not item or not os.path.exists(item.file_path):
            raise HTTPException(status_code=404, detail="Media file missing")
        rating_level = get_profile_rating_level(current_user, current_profile)
        if rating_level > 0 and not is_rating_allowed(getattr(item, 'content_rating', ''), rating_level):
            raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")
        file_path = item.file_path
        if tonemap is None:
            hdr_cfg = session.exec(select(SystemConfig).where(SystemConfig.key == "hdr_tonemapping")).first()
            raw_hdr = str(hdr_cfg.value).strip().lower() if hdr_cfg else "hable"
            if raw_hdr in ("disabled", "false", "0", "off"):
                tonemap = 0
            else:
                tonemap = 1

    probe = probe_media_streams(file_path)
    duration = probe.get("duration") or 0.0

    token = request.query_params.get("token")
    content = hls_manager.build_variant_playlist(
        media_type=media_type,
        item_id=item_id,
        res=res,
        duration=duration,
        audio_track=audio_track,
        burn_sub=burn_sub,
        token=token,
        tonemap=tonemap
    )
    return Response(content=content, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache"})

@app.get("/hls/{media_type}/{item_id}/segment_{res}_{segment_idx}.ts")
def get_hls_segment(
    media_type: str,
    item_id: int,
    res: str,
    segment_idx: int,
    audio_track: int = 0,
    burn_sub: Optional[str] = None,
    tonemap: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if media_type == "movie" and not current_user.movies_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    with Session(engine) as session:
        item = session.get(Movie if media_type == "movie" else TVShow, item_id)
        if not item or not os.path.exists(item.file_path):
            raise HTTPException(status_code=404, detail="Media file missing")
        rating_level = get_profile_rating_level(current_user, current_profile)
        if rating_level > 0 and not is_rating_allowed(getattr(item, 'content_rating', ''), rating_level):
            raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")
        file_path = item.file_path
        hw_config = session.exec(select(SystemConfig).where(SystemConfig.key == "hw_accel_profile")).first()
        hw_profile = hw_config.value if hw_config else "auto"
        hdr_cfg = session.exec(select(SystemConfig).where(SystemConfig.key == "hdr_tonemapping")).first()
        raw_hdr = str(hdr_cfg.value).strip().lower() if hdr_cfg else "hable"
        if raw_hdr in ("disabled", "false", "0", "off"):
            server_hdr_enabled = False
            server_tonemap_algo = "hable"
        else:
            server_hdr_enabled = True
            server_tonemap_algo = raw_hdr if raw_hdr in ("hable", "mobius", "reinhard", "linear", "gamma", "clip") else "hable"

    seg_path = hls_manager.get_segment(
        media_type=media_type,
        item_id=item_id,
        file_path=file_path,
        res=res,
        audio_track=audio_track,
        segment_idx=segment_idx,
        burn_sub=burn_sub,
        hw_profile=hw_profile,
        tonemap=tonemap,
        enable_tonemap=server_hdr_enabled,
        tonemap_algo=server_tonemap_algo
    )
    if not seg_path or not os.path.exists(seg_path):
        raise HTTPException(status_code=404, detail="Segment generation timed out or failed")

    return FileResponse(seg_path, media_type="video/MP2T", headers={"Cache-Control": "public, max-age=86400"})

@app.post("/hls/{media_type}/{item_id}/stop")
def stop_hls(
    media_type: str,
    item_id: int,
    current_user: User = Depends(get_current_user)
):
    if current_user:
        hls_manager.stop_session(media_type, item_id)
    return {"status": "stopped"}


# --- Trickplay (Scrubbing Preview Sprites) ---

@app.get("/trickplay/{media_type}/{item_id}/manifest.json")
def get_trickplay_manifest_endpoint(
    media_type: str,
    item_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if media_type == "movie" and not current_user.movies_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    with Session(engine) as session:
        item = session.get(Movie if media_type == "movie" else TVShow, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        file_path = item.file_path
        rating_level = get_profile_rating_level(current_user, current_profile)
        if rating_level > 0 and not is_rating_allowed(getattr(item, "content_rating", ""), rating_level):
            raise HTTPException(status_code=403, detail="Forbidden")

    manifest = get_trickplay_manifest(media_type, item_id)
    if manifest and manifest.get("status") == "ready":
        return JSONResponse(manifest)

    if is_trickplay_generating(media_type, item_id):
        return JSONResponse({"status": "generating"})

    if file_path and os.path.exists(file_path):
        queue_trickplay_generation(file_path, media_type, item_id)
        return JSONResponse({"status": "generating"})

    return JSONResponse({"status": "unavailable"})


@app.get("/trickplay/{media_type}/{item_id}/thumbnails.vtt")
def get_trickplay_vtt_endpoint(
    media_type: str,
    item_id: int,
    current_user: User = Depends(get_current_user)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    item_dir = get_trickplay_item_dir(media_type, item_id)
    vtt_path = os.path.join(item_dir, "thumbnails.vtt")
    if not os.path.exists(vtt_path):
        raise HTTPException(status_code=404, detail="VTT manifest not found")
    return FileResponse(vtt_path, media_type="text/vtt", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/trickplay/{media_type}/{item_id}/{filename}")
def get_trickplay_sprite_endpoint(
    media_type: str,
    item_id: int,
    filename: str,
    current_user: User = Depends(get_current_user)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if os.path.basename(filename) != filename or not (filename.startswith("sprite_") and filename.endswith(".jpg")):
        raise HTTPException(status_code=400, detail="Invalid filename")
    item_dir = get_trickplay_item_dir(media_type, item_id)
    file_path = os.path.join(item_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Sprite sheet not found")
    return FileResponse(file_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/thumbnail/tv/{item_id}")
def get_tv_episode_thumbnail(
    item_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    """
    Serves a crisp 16:9 landscape frame captured at ~40% (between 1/3 and 1/2) from the episode video.
    Extracts on-demand if not already generated on disk.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.tv_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    with Session(engine) as session:
        item = session.get(TVShow, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        file_path = item.file_path
        rating_level = get_profile_rating_level(current_user, current_profile)
        if rating_level > 0 and not is_rating_allowed(getattr(item, "content_rating", ""), rating_level):
            raise HTTPException(status_code=403, detail="Forbidden")

        metadata_dir = METADATA_DIR if os.path.exists(METADATA_DIR) else "config/metadata"
        thumb_name = item.thumbnail_filename or f"tv_thumb_{item.id}.jpg"
        thumb_path = os.path.join(metadata_dir, thumb_name)

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            if not item.thumbnail_filename:
                item.thumbnail_filename = thumb_name
                session.add(item)
                try:
                    session.commit()
                except Exception:
                    session.rollback()
            return FileResponse(thumb_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})

        # On-demand extraction: single frame at ~40% (between 1/3 and 1/2 of duration)
        if file_path and os.path.exists(file_path):
            success = extract_video_frame(file_path, thumb_path, target_fraction=0.40)
            if success and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                item.thumbnail_filename = thumb_name
                session.add(item)
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                return FileResponse(thumb_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})

        # Fallback to season poster or series poster
        fallback_fn = item.season_poster_filename or item.poster_filename
        if fallback_fn:
            fallback_path = os.path.join(metadata_dir, fallback_fn)
            if os.path.exists(fallback_path):
                return FileResponse(fallback_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

        raise HTTPException(status_code=404, detail="Thumbnail not available")

@app.post("/watchlist/remove/{watchlist_id}")
async def remove_watchlist(request: Request, watchlist_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    item = session.get(Watchlist, watchlist_id)
    if item and item.user_id == current_user.id:
        session.delete(item)
        session.commit()
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

@app.post("/watchlist/remove_by_item/{media_type}/{item_id}")
async def remove_watchlist_by_item(
    request: Request,
    media_type: str,
    item_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    prof_id = current_profile.id if current_profile else None
    q = select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.media_type == media_type).where(Watchlist.item_id == item_id)
    if prof_id:
        q = q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    items = session.exec(q).all()
    for item in items:
        session.delete(item)
    session.commit()
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

@app.post("/watchlist/add/{media_type}/{item_id}")
async def add_watchlist(
    request: Request,
    media_type: str,
    item_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    prof_id = current_profile.id if current_profile else None
    q = select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.media_type == media_type).where(Watchlist.item_id == item_id)
    if prof_id:
        q = q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    existing = session.exec(q).first()
    if not existing:
        session.add(Watchlist(user_id=current_user.id, profile_id=prof_id, media_type=media_type, item_id=item_id))
        session.commit()
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

@app.post("/watchlist/toggle")
async def toggle_watchlist(
    request: Request,
    media_type: Optional[str] = Form(None),
    item_id: Optional[int] = Form(None),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if not media_type or not item_id:
        try:
            body = await request.json()
            media_type = body.get("media_type")
            item_id = int(body.get("item_id"))
        except Exception:
            pass
    if not media_type or not item_id:
        media_type = request.query_params.get("media_type")
        iid_str = request.query_params.get("item_id")
        if iid_str and iid_str.isdigit():
            item_id = int(iid_str)
    if not media_type or not item_id:
        return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

    prof_id = current_profile.id if current_profile else None
    q = select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.media_type == media_type).where(Watchlist.item_id == item_id)
    if prof_id:
        q = q.where((Watchlist.profile_id == prof_id) | (Watchlist.profile_id == None))
    existing = session.exec(q).first()
    if existing:
        items = session.exec(q).all()
        for it in items:
            session.delete(it)
        session.commit()
        in_watchlist = False
    else:
        session.add(Watchlist(user_id=current_user.id, profile_id=prof_id, media_type=media_type, item_id=item_id))
        session.commit()
        in_watchlist = True

    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.headers.get("accept", "").startswith("application/json"):
        return {"status": "success", "in_watchlist": in_watchlist}
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_manifest():
    return {}

@app.post("/history/remove/{media_type}/{item_id}")
async def remove_history(
    request: Request,
    media_type: str,
    item_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.headers.get("accept", "").startswith("application/json"):
            return Response(content='{"status": "unauthorized"}', media_type="application/json", status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    prof_id = current_profile.id if current_profile else None
    q = select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == item_id).where(WatchHistory.media_type == media_type)
    if prof_id:
        q = q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
    history_entries = session.exec(q).all()
    for h in history_entries:
        session.delete(h)
    session.commit()
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.headers.get("accept", "").startswith("application/json"):
        return {"status": "success"}
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

@app.post("/history/mark_watched/{media_type}/{item_id}")
async def mark_watched(
    request: Request,
    media_type: str,
    item_id: int,
    scope: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.headers.get("accept", "").startswith("application/json"):
            return Response(content='{"status": "unauthorized"}', media_type="application/json", status_code=401)
        return RedirectResponse(url="/login", status_code=303)
        
    prof_id = current_profile.id if current_profile else None
    
    if media_type == "movie":
        movie = session.get(Movie, item_id)
        if movie:
            target_ids = get_sibling_movie_ids(session, movie)
            for m_id in target_ids:
                m_obj = session.get(Movie, m_id)
                dur = float(m_obj.runtime * 60) if (m_obj and m_obj.runtime) else 0.0
                hist = session.exec(
                    select(WatchHistory).where(
                        WatchHistory.user_id == current_user.id,
                        WatchHistory.media_type == "movie",
                        WatchHistory.item_id == m_id
                    ).where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None) if prof_id else True)
                ).first()
                if hist:
                    hist.progress = 100.0
                    hist.current_time = dur
                    hist.duration = dur
                    hist.last_watched = datetime.now(timezone.utc)
                    if prof_id: hist.profile_id = prof_id
                    session.add(hist)
                else:
                    hist = WatchHistory(
                        user_id=current_user.id,
                        profile_id=prof_id,
                        media_type="movie",
                        item_id=m_id,
                        progress=100.0,
                        current_time=dur,
                        duration=dur,
                        last_watched=datetime.now(timezone.utc)
                    )
                    session.add(hist)
            session.commit()
            
    elif media_type == "tv":
        tv_item = session.get(TVShow, item_id)
        if tv_item:
            if scope == "episode":
                targets = [tv_item]
            else:
                targets = session.exec(
                    select(TVShow).where(func.lower(TVShow.title) == tv_item.title.strip().lower())
                ).all()
                
            for ep in targets:
                dur = float(ep.runtime * 60) if ep.runtime else 0.0
                hist = session.exec(
                    select(WatchHistory).where(
                        WatchHistory.user_id == current_user.id,
                        WatchHistory.media_type == "tv",
                        WatchHistory.item_id == ep.id
                    ).where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None) if prof_id else True)
                ).first()
                if hist:
                    hist.progress = 100.0
                    hist.current_time = dur
                    hist.duration = dur
                    hist.last_watched = datetime.now(timezone.utc)
                    if prof_id: hist.profile_id = prof_id
                    session.add(hist)
                else:
                    hist = WatchHistory(
                        user_id=current_user.id,
                        profile_id=prof_id,
                        media_type="tv",
                        item_id=ep.id,
                        progress=100.0,
                        current_time=dur,
                        duration=dur,
                        last_watched=datetime.now(timezone.utc)
                    )
                    session.add(hist)

            if scope == "episode" and tv_item:
                next_ep = get_next_uncompleted_episode(session, tv_item, current_user.id, prof_id)
                if next_ep:
                    q_next = select(WatchHistory).where(
                        WatchHistory.user_id == current_user.id,
                        WatchHistory.media_type == "tv",
                        WatchHistory.item_id == next_ep.id
                    )
                    if prof_id:
                        q_next = q_next.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
                    next_hist = session.exec(q_next.order_by(WatchHistory.last_watched.desc())).first()
                    now_ts = datetime.now(timezone.utc)
                    if next_hist:
                        if next_hist.progress < 95.0:
                            next_hist.last_watched = now_ts
                            session.add(next_hist)
                    else:
                        new_next = WatchHistory(
                            user_id=current_user.id,
                            profile_id=prof_id,
                            media_type="tv",
                            item_id=next_ep.id,
                            progress=0.0,
                            current_time=0.0,
                            duration=float(next_ep.runtime * 60) if next_ep.runtime else 0.0,
                            last_watched=now_ts
                        )
                        session.add(new_next)

            session.commit()
            
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.headers.get("accept", "").startswith("application/json"):
        return {"status": "success", "watched": True}
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

@app.post("/history/mark_unwatched/{media_type}/{item_id}")
async def mark_unwatched(
    request: Request,
    media_type: str,
    item_id: int,
    scope: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user:
        if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.headers.get("accept", "").startswith("application/json"):
            return Response(content='{"status": "unauthorized"}', media_type="application/json", status_code=401)
        return RedirectResponse(url="/login", status_code=303)
        
    prof_id = current_profile.id if current_profile else None
    
    if media_type == "movie":
        movie = session.get(Movie, item_id)
        target_ids = get_sibling_movie_ids(session, movie) if movie else [item_id]
        q = select(WatchHistory).where(
            WatchHistory.user_id == current_user.id,
            WatchHistory.media_type == "movie",
            WatchHistory.item_id.in_(target_ids)
        )
        if prof_id:
            q = q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
        entries = session.exec(q).all()
        for h in entries:
            session.delete(h)
        session.commit()
        
    elif media_type == "tv":
        tv_item = session.get(TVShow, item_id)
        if tv_item:
            if scope == "episode":
                target_ids = [tv_item.id]
            else:
                episodes = session.exec(
                    select(TVShow.id).where(func.lower(TVShow.title) == tv_item.title.strip().lower())
                ).all()
                target_ids = list(episodes)
                
            q = select(WatchHistory).where(
                WatchHistory.user_id == current_user.id,
                WatchHistory.media_type == "tv",
                WatchHistory.item_id.in_(target_ids)
            )
            if prof_id:
                q = q.where((WatchHistory.profile_id == prof_id) | (WatchHistory.profile_id == None))
            entries = session.exec(q).all()
            for h in entries:
                session.delete(h)
            session.commit()
            
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.headers.get("accept", "").startswith("application/json"):
        return {"status": "success", "watched": False}
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

# ----------------- SETTINGS & USER MANAGEMENT -----------------

class ZipGeneratorStream:
    def __init__(self):
        self.offset = 0
        self.chunks = []

    def write(self, data):
        self.offset += len(data)
        self.chunks.append(bytes(data))
        return len(data)

    def tell(self):
        return self.offset

    def flush(self):
        pass


def iter_zip_archive(files: list[tuple[str, str]]):
    stream = ZipGeneratorStream()
    zf = zipfile.ZipFile(stream, mode='w', compression=zipfile.ZIP_STORED, allowZip64=True)
    for file_path, arcname in files:
        if not os.path.exists(file_path):
            continue
        file_size = os.path.getsize(file_path)
        mtime = time.localtime(os.path.getmtime(file_path))[:6]
        zinfo = zipfile.ZipInfo(arcname, date_time=mtime)
        zinfo.file_size = file_size
        zinfo.compress_type = zipfile.ZIP_STORED
        with zf.open(zinfo, mode='w') as dest, open(file_path, 'rb') as src:
            while True:
                chunk = src.read(512 * 1024)
                if not chunk:
                    break
                dest.write(chunk)
                while stream.chunks:
                    yield stream.chunks.pop(0)
    zf.close()
    while stream.chunks:
        yield stream.chunks.pop(0)


@app.get("/download/tv/season/{item_id}")
@app.get("/download/tv/season/{item_id}/{season}")
async def download_tv_season(
    item_id: int,
    season: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.downloads_access: raise HTTPException(status_code=403, detail="Forbidden")
    if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")

    with Session(engine) as session:
        ref_show = session.get(TVShow, item_id)
        if not ref_show:
            raise HTTPException(status_code=404, detail="TV Show not found")

        all_episodes = session.exec(select(TVShow).where(TVShow.title == ref_show.title)).all()
        if not all_episodes:
            raise HTTPException(status_code=404, detail="No episodes found")

        rating_level = get_profile_rating_level(current_user, current_profile)
        if rating_level > 0:
            all_episodes = [ep for ep in all_episodes if is_rating_allowed(getattr(ep, 'content_rating', ''), rating_level)]
            if not all_episodes:
                raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")

        def get_ep_season(ep: TVShow) -> int:
            if ep.season is not None:
                return ep.season
            m = re.search(r'S(\d+)E(\d+)', ep.file_path, re.IGNORECASE)
            return int(m.group(1)) if m else 0

        def get_ep_num(ep: TVShow) -> int:
            if ep.episode is not None:
                return ep.episode
            m = re.search(r'S(\d+)E(\d+)', ep.file_path, re.IGNORECASE)
            return int(m.group(2)) if m else 0

        target_season = season if season is not None else get_ep_season(ref_show)
        season_episodes = [ep for ep in all_episodes if get_ep_season(ep) == target_season]
        season_episodes.sort(key=get_ep_num)

        valid_files = [ep for ep in season_episodes if ep.file_path and os.path.exists(ep.file_path)]
        if not valid_files:
            raise HTTPException(status_code=404, detail="No video files found for this season")

        title = ref_show.title

    if len(valid_files) == 1:
        fpath = valid_files[0].file_path
        return FileResponse(fpath, media_type="application/octet-stream", filename=os.path.basename(fpath))

    used_names = set()
    zip_files = []
    for ep in valid_files:
        base_name = os.path.basename(ep.file_path)
        name = base_name
        counter = 1
        while name in used_names:
            p, ext = os.path.splitext(base_name)
            name = f"{p}_{counter}{ext}"
            counter += 1
        used_names.add(name)
        zip_files.append((ep.file_path, name))

    season_label = "Specials" if target_season == 0 else f"Season {target_season}"
    zip_filename = f"{title} - {season_label}.zip"
    clean_filename = re.sub(r'[/\\?%*:|"<>]', '_', zip_filename)
    headers = {"Content-Disposition": f'attachment; filename="{clean_filename}"'}
    return StreamingResponse(iter_zip_archive(zip_files), media_type="application/zip", headers=headers)


@app.get("/download/tv/show/{item_id}")
async def download_tv_show(
    item_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.downloads_access: raise HTTPException(status_code=403, detail="Forbidden")
    if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")

    with Session(engine) as session:
        ref_show = session.get(TVShow, item_id)
        if not ref_show:
            raise HTTPException(status_code=404, detail="TV Show not found")

        all_episodes = session.exec(select(TVShow).where(TVShow.title == ref_show.title)).all()
        if not all_episodes:
            raise HTTPException(status_code=404, detail="No episodes found")

        rating_level = get_profile_rating_level(current_user, current_profile)
        if rating_level > 0:
            all_episodes = [ep for ep in all_episodes if is_rating_allowed(getattr(ep, 'content_rating', ''), rating_level)]
            if not all_episodes:
                raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")

        def get_ep_season(ep: TVShow) -> int:
            if ep.season is not None:
                return ep.season
            m = re.search(r'S(\d+)E(\d+)', ep.file_path, re.IGNORECASE)
            return int(m.group(1)) if m else 0

        def get_ep_num(ep: TVShow) -> int:
            if ep.episode is not None:
                return ep.episode
            m = re.search(r'S(\d+)E(\d+)', ep.file_path, re.IGNORECASE)
            return int(m.group(2)) if m else 0

        all_episodes.sort(key=lambda ep: (get_ep_season(ep), get_ep_num(ep)))
        valid_files = [ep for ep in all_episodes if ep.file_path and os.path.exists(ep.file_path)]
        if not valid_files:
            raise HTTPException(status_code=404, detail="No video files found for this show")

        title = ref_show.title

    if len(valid_files) == 1:
        fpath = valid_files[0].file_path
        return FileResponse(fpath, media_type="application/octet-stream", filename=os.path.basename(fpath))

    distinct_seasons = set(get_ep_season(ep) for ep in valid_files)
    use_season_folders = len(distinct_seasons) > 1

    used_names = set()
    zip_files = []
    for ep in valid_files:
        base_name = os.path.basename(ep.file_path)
        if use_season_folders:
            s = get_ep_season(ep)
            folder = "Specials" if s == 0 else f"Season {s}"
            arcname = f"{folder}/{base_name}"
        else:
            arcname = base_name

        name = arcname
        counter = 1
        while name in used_names:
            p, ext = os.path.splitext(arcname)
            name = f"{p}_{counter}{ext}"
            counter += 1
        used_names.add(name)
        zip_files.append((ep.file_path, name))

    zip_filename = f"{title} - Entire Show.zip"
    clean_filename = re.sub(r'[/\\?%*:|"<>]', '_', zip_filename)
    headers = {"Content-Disposition": f'attachment; filename="{clean_filename}"'}
    return StreamingResponse(iter_zip_archive(zip_files), media_type="application/zip", headers=headers)


@app.get("/download/album/{album_id}")
async def download_album(
    album_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.downloads_access: raise HTTPException(status_code=403, detail="Forbidden")
    if not current_user.music_access: raise HTTPException(status_code=403, detail="Forbidden")

    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    tracks = session.exec(select(Track).where(Track.album_id == album.id).order_by(Track.disc_number.asc(), Track.track_number.asc())).all()
    valid_files = [t for t in tracks if t.file_path and os.path.exists(t.file_path)]
    if not valid_files:
        raise HTTPException(status_code=404, detail="No audio files found for this album")

    if len(valid_files) == 1:
        fpath = valid_files[0].file_path
        return FileResponse(fpath, media_type="application/octet-stream", filename=os.path.basename(fpath))

    used_names = set()
    zip_files = []
    distinct_discs = set(t.disc_number or 1 for t in valid_files)
    use_disc_folders = len(distinct_discs) > 1

    for t in valid_files:
        base_name = os.path.basename(t.file_path)
        if use_disc_folders:
            d = t.disc_number or 1
            folder = f"Disc {d}"
            arcname = f"{folder}/{base_name}"
        else:
            arcname = base_name

        name = arcname
        counter = 1
        while name in used_names:
            p, ext = os.path.splitext(arcname)
            name = f"{p}_{counter}{ext}"
            counter += 1
        used_names.add(name)
        zip_files.append((t.file_path, name))

    artist_str = album.artist_name or "Unknown Artist"
    title_str = album.title or "Unknown Album"
    zip_filename = f"{artist_str} - {title_str}.zip"
    clean_filename = re.sub(r'[/\\?%*:|"<>]', '_', zip_filename)
    headers = {"Content-Disposition": f'attachment; filename="{clean_filename}"'}
    return StreamingResponse(iter_zip_archive(zip_files), media_type="application/zip", headers=headers)


@app.get("/download/{media_type}/{item_id}")
async def download_media(
    media_type: str,
    item_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.downloads_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "movie" and not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "photo" and not current_user.photos_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type in ("track", "music") and not current_user.music_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "album":
        return await download_album(item_id, current_user, current_profile, session)
    
    if media_type == "photo":
        item = session.get(Photo, item_id)
    elif media_type == "movie":
        item = session.get(Movie, item_id)
    elif media_type in ("track", "music"):
        item = session.get(Track, item_id)
    else:
        item = session.get(TVShow, item_id)
    if not item or not os.path.exists(item.file_path): raise HTTPException(status_code=404, detail="File missing")
    rating_level = get_profile_rating_level(current_user, current_profile)
    if media_type not in ("photo", "track", "music") and rating_level > 0 and not is_rating_allowed(getattr(item, 'content_rating', ''), rating_level):
        raise HTTPException(status_code=403, detail="Not appropriate for current profile rating level")
    file_path = item.file_path
    
    filename = os.path.basename(file_path)
    return FileResponse(file_path, media_type="application/octet-stream", filename=filename)

def format_storage_size(gb_val: float) -> str:
    if gb_val >= 1000:
        return f"{gb_val / 1000:.1f} TB"
    return f"{gb_val:.1f} GB"

def get_server_metrics(session: Session) -> dict:
    cpu_percent = 0.0
    cpu_count = os.cpu_count() or 1
    try:
        import psutil
        cpu_percent = psutil.cpu_percent(interval=None)
        logical_count = psutil.cpu_count(logical=True)
        if logical_count:
            cpu_count = logical_count
    except Exception:
        pass

    ram_data = {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0, "free_gb": 0.0}
    try:
        import psutil
        mem = psutil.virtual_memory()
        ram_data = {
            "percent": round(mem.percent, 1),
            "used_gb": round(mem.used / (1024**3), 2),
            "total_gb": round(mem.total / (1024**3), 2),
            "free_gb": round(mem.available / (1024**3), 2)
        }
    except Exception:
        pass

    disks = []
    checked_paths = set()
    volumes = session.exec(select(VolumePath)).all()
    for vol in volumes:
        if vol.path and os.path.exists(vol.path):
            try:
                norm_p = os.path.abspath(vol.path).lower()
                if norm_p not in checked_paths:
                    checked_paths.add(norm_p)
                    du = shutil.disk_usage(vol.path)
                    pct = round((du.used / du.total) * 100, 1) if du.total > 0 else 0.0
                    display_type = {"movie": "Movies", "tv": "TV Shows", "photo": "Photos", "music": "Music"}.get(vol.media_type, vol.media_type.capitalize())
                    tot_gb = round(du.total / (1024**3), 1)
                    used_gb = round(du.used / (1024**3), 1)
                    free_gb = round(du.free / (1024**3), 1)
                    disks.append({
                        "name": f"{display_type} ({vol.path})",
                        "path": vol.path,
                        "total_gb": tot_gb,
                        "used_gb": used_gb,
                        "free_gb": free_gb,
                        "total_str": format_storage_size(tot_gb),
                        "used_str": format_storage_size(used_gb),
                        "free_str": format_storage_size(free_gb),
                        "percent": pct
                    })
            except Exception:
                pass

    transcode_count = 0
    try:
        import psutil
        for p in psutil.process_iter(['name']):
            if p.info['name'] and 'ffmpeg' in p.info['name'].lower():
                transcode_count += 1
    except Exception:
        pass

    uptime_seconds = int(time.time() - SERVER_START_TIME)
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    uptime_str = f"{days}d {hours}h {minutes}m" if days > 0 else f"{hours}h {minutes}m"

    active_streams = active_stream_manager.get_active_streams()

    return {
        "cpu": {"percent": round(cpu_percent, 1), "cores": cpu_count},
        "memory": ram_data,
        "disks": disks,
        "transcodes_count": transcode_count,
        "uptime_str": uptime_str,
        "active_streams": active_streams,
        "active_streams_count": len(active_streams)
    }

def cleanup_duplicate_watch_history(session: Session) -> int:
    """
    Finds and deletes duplicate WatchHistory records for the same user and media item,
    retaining only the record with the most recent last_watched timestamp.
    """
    try:
        dup_groups = session.exec(
            select(WatchHistory.user_id, WatchHistory.media_type, WatchHistory.item_id)
            .group_by(WatchHistory.user_id, WatchHistory.media_type, WatchHistory.item_id)
            .having(func.count(WatchHistory.id) > 1)
        ).all()

        deleted_count = 0
        for uid, mtype, iid in dup_groups:
            records = session.exec(
                select(WatchHistory)
                .where(
                    WatchHistory.user_id == uid,
                    WatchHistory.media_type == mtype,
                    WatchHistory.item_id == iid
                )
                .order_by(WatchHistory.last_watched.desc(), WatchHistory.id.desc())
            ).all()

            if len(records) > 1:
                # Keep the first (most recently watched) record and delete all duplicates
                for r in records[1:]:
                    session.delete(r)
                    deleted_count += 1

        if deleted_count > 0:
            session.commit()
        return deleted_count
    except Exception:
        session.rollback()
        return 0

def get_user_and_watch_stats(session: Session, limit: int = 25) -> dict:
    # First, proactively clean up any duplicate records stored in the database
    cleanup_duplicate_watch_history(session)

    # Fetch a wider pool to allow deduplication across series/sibling titles
    history_records = session.exec(
        select(WatchHistory).order_by(WatchHistory.last_watched.desc()).limit(limit * 3)
    ).all()

    user_ids = {h.user_id for h in history_records}
    users = {u.id: u for u in session.exec(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}

    recent_history = []
    seen_history_keys = set()
    now = datetime.now(timezone.utc)
    for h in history_records:
        u = users.get(h.user_id)
        username = u.username if u else "Unknown"

        title = "Unknown Item"
        poster = None
        season_episode = None
        year = ""
        cur_sec = getattr(h, 'current_time', 0.0) or 0.0
        dur_sec = getattr(h, 'duration', 0.0) or 0.0

        if h.media_type == "movie":
            m = session.get(Movie, h.item_id)
            if m:
                title = m.title
                poster = m.poster_filename
                year = m.year or ""
                if not dur_sec and m.runtime:
                    dur_sec = float(m.runtime)
                elif not dur_sec and m.file_path and os.path.exists(m.file_path):
                    try:
                        get_runtime_str(m, session)
                        if m.runtime:
                            dur_sec = float(m.runtime)
                    except Exception:
                        pass
        elif h.media_type == "tv":
            t = session.get(TVShow, h.item_id)
            if t:
                title = t.title
                poster = t.season_poster_filename or t.poster_filename
                year = t.year or ""
                match = re.search(r'S(\d+)E(\d+)', t.file_path, re.IGNORECASE)
                if match:
                    season_episode = f"S{int(match.group(1))} • E{int(match.group(2))}"
                if not dur_sec and t.runtime:
                    dur_sec = float(t.runtime)
                elif not dur_sec and t.file_path and os.path.exists(t.file_path):
                    try:
                        get_runtime_str(t, session)
                        if t.runtime:
                            dur_sec = float(t.runtime)
                    except Exception:
                        pass

        # Deduplicate: A user watching the same episode or movie should only appear once (the latest watch update)
        if h.media_type == "tv":
            dedup_key = (h.user_id, "tv", title.strip().lower(), (season_episode or str(h.item_id)).lower())
        elif h.media_type == "movie":
            clean_year = str(year).split(" • ")[0].strip()
            dedup_key = (h.user_id, "movie", title.strip().lower(), clean_year or str(h.item_id))
        else:
            dedup_key = (h.user_id, h.media_type, h.item_id)

        if dedup_key in seen_history_keys:
            continue
        seen_history_keys.add(dedup_key)

        if dur_sec > 0 and not cur_sec:
            cur_sec = dur_sec * (h.progress / 100.0)

        # Cache duration and current_time onto history entry if missing
        if (getattr(h, 'duration', 0.0) or 0.0) == 0.0 and dur_sec > 0:
            h.duration = dur_sec
            h.current_time = cur_sec
            session.add(h)
            try:
                session.commit()
            except Exception:
                session.rollback()

        if dur_sec > 0:
            actual_sec = int(round(cur_sec))
            dur_sec_int = int(round(dur_sec))
            time_left = max(0, dur_sec_int - actual_sec)
            time_display = f"{format_seconds(actual_sec)} / {format_seconds(dur_sec_int)} -{format_seconds(time_left)}"
        elif cur_sec > 0:
            time_display = format_seconds(cur_sec)
        else:
            time_display = "0:00"

        if h.last_watched:
            lw = h.last_watched.replace(tzinfo=timezone.utc) if h.last_watched.tzinfo is None else h.last_watched
            delta = now - lw
        else:
            delta = timedelta(0)
        secs = delta.total_seconds()
        if secs < 60:
            time_ago = "Just now"
        elif secs < 3600:
            time_ago = f"{int(secs // 60)}m ago"
        elif secs < 86400:
            time_ago = f"{int(secs // 3600)}h ago"
        else:
            time_ago = f"{delta.days}d ago"

        recent_history.append({
            "id": h.id,
            "username": username,
            "media_type": h.media_type,
            "item_id": h.item_id,
            "title": title,
            "poster_filename": poster,
            "season_episode": season_episode,
            "year": year,
            "progress": round(h.progress, 1),
            "current_time": round(cur_sec, 1),
            "duration": round(dur_sec, 1),
            "time_display": time_display,
            "is_completed": h.progress >= 90.0,
            "time_ago": time_ago,
            "last_watched": h.last_watched.strftime("%Y-%m-%d %H:%M") if h.last_watched else ""
        })

        if len(recent_history) >= limit:
            break

    all_users = session.exec(select(User)).all()
    users_list = []
    for u in all_users:
        watched_count = session.exec(select(func.count(WatchHistory.id)).where(WatchHistory.user_id == u.id)).one()
        users_list.append({
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
            "kids_mode": u.kids_mode,
            "items_watched": watched_count
        })

    total_movies = session.exec(select(func.count(Movie.id))).one()
    total_tv_episodes = session.exec(select(func.count(TVShow.id))).one()
    total_tv_shows = session.exec(select(func.count(func.distinct(TVShow.title)))).one()
    total_photos = session.exec(select(func.count(Photo.id))).one()

    return {
        "recent_history": recent_history,
        "users": users_list,
        "total_users": len(all_users),
        "library": {
            "movies": total_movies,
            "tv_shows": total_tv_shows,
            "tv_episodes": total_tv_episodes,
            "photos": total_photos
        }
    }

@app.get("/server-dashboard", response_class=HTMLResponse)
async def server_dashboard_view(
    request: Request,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if not is_actual_admin(current_user, current_profile):
        raise HTTPException(status_code=403, detail="Admin privileges required to view server dashboard.")
    metrics = get_server_metrics(session)
    user_stats = get_user_and_watch_stats(session)
    return templates.TemplateResponse(request=request, name="server_dashboard.html", context={
        "user": current_user, "metrics": metrics, "stats": user_stats
    })

@app.get("/server")
async def server_redirect(current_user: User = Depends(get_current_user)):
    return RedirectResponse(url="/server-dashboard", status_code=303)

@app.get("/activity")
async def activity_redirect(current_user: User = Depends(get_current_user)):
    return RedirectResponse(url="/server-dashboard", status_code=303)

@app.get("/api/server-dashboard/stats")
async def api_server_dashboard_stats(
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile):
        raise HTTPException(status_code=403, detail="Forbidden")
    metrics = get_server_metrics(session)
    user_stats = get_user_and_watch_stats(session, limit=15)
    return {
        "metrics": metrics,
        "stats": user_stats,
        "scanner": scanner_manager.get_status()
    }

@app.get("/api/scanner/status")
async def api_scanner_status(
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return scanner_manager.get_status()

@app.post("/api/scanner/start")
async def api_scanner_start(
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not is_actual_admin(current_user, current_profile):
        raise HTTPException(status_code=403, detail="Forbidden")
    started = scanner_manager.start_scan()
    return {"started": started, "status": scanner_manager.get_status()}

AVAILABLE_LANGUAGES = [
    ("eng", "English"),
    ("spa", "Spanish"),
    ("fra", "French"),
    ("deu", "German"),
    ("ita", "Italian"),
    ("por", "Portuguese"),
    ("jpn", "Japanese"),
    ("zho", "Chinese"),
    ("kor", "Korean"),
    ("ara", "Arabic"),
    ("hin", "Hindi"),
    ("rus", "Russian"),
    ("nld", "Dutch"),
    ("pol", "Polish"),
    ("swe", "Swedish"),
    ("nor", "Norwegian"),
    ("dan", "Danish"),
    ("fin", "Finnish"),
    ("tur", "Turkish"),
    ("vie", "Vietnamese"),
    ("tha", "Thai"),
    ("ukr", "Ukrainian"),
    ("ces", "Czech"),
    ("hun", "Hungarian"),
    ("ell", "Greek"),
]

@app.get("/settings", response_class=HTMLResponse)
async def view_settings(
    request: Request,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    
    is_admin = is_actual_admin(current_user, current_profile)
    is_primary = (current_profile is None or current_profile.is_primary)
    can_change_password = is_primary

    invites = []
    users = []
    volume_paths = []
    remote_url = ""
    tmdb_key = ""
    hw_accel_profile = "auto"
    host_url = str(request.base_url).rstrip("/")

    if is_admin:
        if scanner_manager.is_scanning:
            scanner_manager.check_scan_necessary()
        invites = session.exec(select(InviteCode).order_by(InviteCode.id.desc())).all()
        users = session.exec(select(User).order_by(User.id)).all()
        v_paths = session.exec(select(VolumePath).order_by(VolumePath.id)).all()
        for v in v_paths:
            display_type = {"movie": "Movies", "tv": "TV Shows", "photo": "Photos", "music": "Music"}.get(v.media_type, v.media_type)
            volume_paths.append({
                "id": v.id,
                "path": v.path,
                "display_path": v.path,
                "raw_media_type": v.media_type,
                "media_type": display_type,
                "exists": os.path.exists(v.path)
            })
        mounted_volumes = {
            "movie": any(v.media_type == "movie" and os.path.exists(v.path) for v in v_paths),
            "tv": any(v.media_type == "tv" and os.path.exists(v.path) for v in v_paths),
            "photo": any(v.media_type == "photo" and os.path.exists(v.path) for v in v_paths),
            "music": any(v.media_type == "music" and os.path.exists(v.path) for v in v_paths),
        }
        remote_url_config = session.exec(select(SystemConfig).where(SystemConfig.key == "remote_access_url")).first()
        remote_url = remote_url_config.value if remote_url_config else ""
        if remote_url:
            host_url = remote_url.rstrip("/")
        
        tmdb_key_config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
        tmdb_key = tmdb_key_config.value if tmdb_key_config else os.getenv("TMDB_API_KEY", "")

        adb_key_config = session.exec(select(SystemConfig).where(SystemConfig.key == "theaudiodb_api_key")).first()
        theaudiodb_key = adb_key_config.value if adb_key_config else os.getenv("THEAUDIODB_API_KEY", "")
        
        hw_accel_config = session.exec(select(SystemConfig).where(SystemConfig.key == "hw_accel_profile")).first()
        hw_accel_profile = hw_accel_config.value if hw_accel_config else "auto"

        hdr_config = session.exec(select(SystemConfig).where(SystemConfig.key == "hdr_tonemapping")).first()
        if hdr_config:
            raw_hdr = str(hdr_config.value).strip().lower()
            if raw_hdr in ("true", "1", "on"):
                hdr_tonemapping = "hable"
            elif raw_hdr in ("false", "0", "off", "disabled"):
                hdr_tonemapping = "disabled"
            elif raw_hdr in ("hable", "mobius", "reinhard", "linear", "gamma", "clip"):
                hdr_tonemapping = raw_hdr
            else:
                hdr_tonemapping = "hable"
        else:
            hdr_tonemapping = "hable"
    else:
        mounted_volumes = {"movie": False, "tv": False, "photo": False, "music": False}
        theaudiodb_key = ""
        hdr_tonemapping = "hable"
    
    rewind_seconds = current_profile.rewind_seconds if (current_profile and current_profile.rewind_seconds is not None) else (current_user.rewind_seconds or 0)
    default_audio_lang = (current_profile.default_audio_lang if (current_profile and current_profile.default_audio_lang) else (current_user.default_audio_lang or "eng")) or "eng"
    default_subtitle_lang = (current_profile.default_subtitle_lang if (current_profile and current_profile.default_subtitle_lang) else (current_user.default_subtitle_lang or "eng")) or "eng"
    
    return templates.TemplateResponse(request=request, name="settings.html", context={
        "user": current_user, "current_profile": current_profile,
        "is_admin": is_admin, "can_change_password": can_change_password,
        "invites": invites, "users": users, "volume_paths": volume_paths, "mounted_volumes": mounted_volumes,
        "host_url": host_url, "remote_url": remote_url, "tmdb_key": tmdb_key, "theaudiodb_key": theaudiodb_key,
        "hw_accel_profile": hw_accel_profile, "hdr_tonemapping": hdr_tonemapping, "rewind_seconds": rewind_seconds, "default_audio_lang": default_audio_lang, "default_subtitle_lang": default_subtitle_lang, "available_languages": AVAILABLE_LANGUAGES
    })

@app.post("/settings/transcoding")
async def save_transcoding_settings(
    request: Request,
    hw_accel_profile: str = Form("auto"),
    hdr_tonemapping: str = Form("hable"),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    config = session.exec(select(SystemConfig).where(SystemConfig.key == "hw_accel_profile")).first()
    if not config:
        config = SystemConfig(key="hw_accel_profile", value=hw_accel_profile.strip())
        session.add(config)
    else:
        config.value = hw_accel_profile.strip()
        session.add(config)

    valid_algos = {"disabled", "hable", "mobius", "reinhard", "linear", "gamma", "clip"}
    hdr_val = str(hdr_tonemapping).strip().lower()
    if hdr_val not in valid_algos:
        hdr_val = "hable" if hdr_val in ("true", "1", "on") else "disabled"

    hdr_cfg = session.exec(select(SystemConfig).where(SystemConfig.key == "hdr_tonemapping")).first()
    if not hdr_cfg:
        hdr_cfg = SystemConfig(key="hdr_tonemapping", value=hdr_val)
        session.add(hdr_cfg)
    else:
        hdr_cfg.value = hdr_val
        session.add(hdr_cfg)

    session.commit()
    return JSONResponse({"status": "ok", "hdr_tonemapping": hdr_val, "hw_accel_profile": hw_accel_profile.strip()})

@app.post("/settings/change_password")
async def change_password(
    new_password: str = Form(...),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if current_profile and not current_profile.is_primary:
        raise HTTPException(status_code=403, detail="Household users cannot change account password.")
    
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')
    current_user.password_hash = hashed
    
    session.add(current_user)
    session.commit()
    
    return RedirectResponse(url="/settings?pw_success=1", status_code=303)

@app.post("/settings/rewind")
async def save_rewind_settings(
    rewind_seconds: int = Form(0),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    val = max(0, min(300, rewind_seconds))
    if current_profile:
        current_profile.rewind_seconds = val
        session.add(current_profile)
    if current_profile is None or current_profile.is_primary:
        current_user.rewind_seconds = val
        session.add(current_user)
    session.commit()
    return RedirectResponse(url="/settings?rewind_success=1", status_code=303)

@app.post("/settings/language")
async def save_language_settings(
    default_audio_lang: str = Form("eng"),
    default_subtitle_lang: str = Form("eng"),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    clean_audio = (default_audio_lang or "eng").strip().lower()
    clean_sub = (default_subtitle_lang or "eng").strip().lower()
    if current_profile:
        current_profile.default_audio_lang = clean_audio
        current_profile.default_subtitle_lang = clean_sub
        session.add(current_profile)
    if current_profile is None or current_profile.is_primary:
        current_user.default_audio_lang = clean_audio
        current_user.default_subtitle_lang = clean_sub
        session.add(current_user)
    session.commit()
    return JSONResponse({"status": "ok", "default_audio_lang": clean_audio, "default_subtitle_lang": clean_sub})

@app.post("/settings/tmdb")
async def save_tmdb_key(
    tmdb_key: str = Form(""),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
    if not config:
        config = SystemConfig(key="tmdb_api_key", value=tmdb_key.strip())
        session.add(config)
    else:
        config.value = tmdb_key.strip()
        session.add(config)
    session.commit()
    scanner_debouncer.trigger_scan(delay=0.5)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/theaudiodb")
async def save_theaudiodb_key(
    theaudiodb_key: str = Form(""),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    config = session.exec(select(SystemConfig).where(SystemConfig.key == "theaudiodb_api_key")).first()
    if not config:
        config = SystemConfig(key="theaudiodb_api_key", value=theaudiodb_key.strip())
        session.add(config)
    else:
        config.value = theaudiodb_key.strip()
        session.add(config)
    session.commit()
    scanner_debouncer.trigger_scan(delay=0.5)
    return RedirectResponse(url="/settings", status_code=303)

class FixMatchRequest(BaseModel):
    media_type: str
    item_id: int
    tmdb_id: int

@app.get("/api/media/matches")
async def search_media_matches(
    media_type: str = Query(...),
    query: str = Query(...),
    year: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user or not is_actual_admin(current_user, current_profile):
        raise HTTPException(status_code=403, detail="Admin permissions required")

    config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
    tmdb_key = config.value if config else os.getenv("TMDB_API_KEY", "")
    if not tmdb_key:
        return JSONResponse(status_code=400, content={"error": "TMDB API key is not configured. Please add your key in Settings > Metadata Integration."})

    endpoint = "https://api.themoviedb.org/3/search/movie" if media_type == "movie" else "https://api.themoviedb.org/3/search/tv"
    params = {"query": query.strip(), "api_key": tmdb_key}
    if year and year.strip():
        if media_type == "movie":
            params["year"] = year.strip()
        else:
            params["first_air_date_year"] = year.strip()

    raw_client = httpx.AsyncClient(timeout=10.0) if httpx is not None else None
    try:
        client = SafeHTTPClient(raw_client)
        res = await client.get(endpoint, params=params)
        if res.status_code != 200:
            return JSONResponse(status_code=res.status_code, content={"error": f"TMDB search failed (HTTP {res.status_code})"})

        results_data = res.json().get("results", [])
        candidates = []
        for r in results_data[:10]:
            title = r.get("title") if media_type == "movie" else r.get("name")
            release_date = r.get("release_date") if media_type == "movie" else r.get("first_air_date")
            rel_year = release_date[:4] if release_date else ""
            poster_path = r.get("poster_path")
            poster_url = f"https://image.tmdb.org/t/p/w185{poster_path}" if poster_path else ""
            candidates.append({
                "tmdb_id": r.get("id"),
                "title": title or "Unknown",
                "year": rel_year,
                "overview": r.get("overview") or "",
                "poster_url": poster_url
            })
        return {"candidates": candidates}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to search matches: {str(e)}"})
    finally:
        if raw_client is not None:
            await raw_client.aclose()

@app.post("/api/media/fix_match")
async def fix_media_match(
    data: FixMatchRequest,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user or not is_actual_admin(current_user, current_profile):
        raise HTTPException(status_code=403, detail="Admin permissions required")

    config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
    tmdb_key = config.value if config else os.getenv("TMDB_API_KEY", "")
    if not tmdb_key:
        return JSONResponse(status_code=400, content={"error": "TMDB API key is not configured."})

    media_type = data.media_type
    item_id = data.item_id
    tmdb_id = data.tmdb_id

    raw_client = httpx.AsyncClient(timeout=15.0) if httpx is not None else None
    try:
        client = SafeHTTPClient(raw_client)
        if media_type == "movie":
            movie = session.get(Movie, item_id)
            if not movie:
                raise HTTPException(status_code=404, detail="Movie not found")

            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
            res = await client.get(url, params={"api_key": tmdb_key})
            if res.status_code != 200:
                return JSONResponse(status_code=400, content={"error": "Failed to fetch movie details from TMDB."})
            m_data = res.json()

            poster_filename = await async_download_image(client, m_data.get("poster_path"), "movie", tmdb_id, "poster")
            backdrop_filename = await async_download_image(client, m_data.get("backdrop_path"), "movie", tmdb_id, "backdrop")
            cast, director, content_rating, genres_str, trailer_key, col_id, col_name, col_poster, col_backdrop, logo_filename = await async_fetch_additional_details(client, tmdb_id, "movie", tmdb_key)

            movie.title = m_data.get("title") or movie.title
            movie.year = (m_data.get("release_date") or "")[:4] or movie.year
            movie.plot = m_data.get("overview") or movie.plot
            movie.rating = m_data.get("vote_average") or movie.rating
            movie.tmdb_id = tmdb_id
            movie.collection_id = col_id if col_id else 0
            movie.collection_name = col_name
            if col_poster:
                movie.collection_poster_filename = col_poster
            if col_backdrop:
                movie.collection_backdrop_filename = col_backdrop
            if logo_filename:
                movie.logo_filename = logo_filename
            if trailer_key:
                movie.trailer_key = trailer_key
            if poster_filename:
                movie.poster_filename = poster_filename
            if backdrop_filename:
                movie.backdrop_filename = backdrop_filename
            if cast:
                movie.cast = cast
            if director:
                movie.director = director
            if content_rating:
                movie.content_rating = content_rating
            if genres_str:
                movie.genres = genres_str

            session.add(movie)
            session.commit()
            return {"success": True, "title": movie.title}

        elif media_type == "tv":
            show = session.get(TVShow, item_id)
            if not show:
                raise HTTPException(status_code=404, detail="TV Show not found")

            old_title = show.title
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
            res = await client.get(url, params={"api_key": tmdb_key})
            if res.status_code != 200:
                return JSONResponse(status_code=400, content={"error": "Failed to fetch TV details from TMDB."})
            s_data = res.json()

            poster_filename = await async_download_image(client, s_data.get("poster_path"), "tv", tmdb_id, "poster")
            backdrop_filename = await async_download_image(client, s_data.get("backdrop_path"), "tv", tmdb_id, "backdrop")
            cast, director, content_rating, genres_str, _, _, _, _, _, logo_filename = await async_fetch_additional_details(client, tmdb_id, "tv", tmdb_key)

            new_title = s_data.get("name") or old_title
            new_year = (s_data.get("first_air_date") or "")[:4] or show.year

            episodes = session.exec(select(TVShow).where(TVShow.title == old_title)).all()
            if not episodes:
                episodes = [show]

            seasons = {ep.season for ep in episodes if ep.season is not None}
            season_posters = {}
            for s in seasons:
                try:
                    s_res = await client.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{s}", params={"api_key": tmdb_key})
                    if s_res.status_code == 200:
                        s_poster_path = s_res.json().get("poster_path")
                        if s_poster_path:
                            season_posters[s] = await async_download_image(client, s_poster_path, f"tv_s{s}", tmdb_id, "poster")
                except Exception:
                    pass

            for ep in episodes:
                ep.title = new_title
                ep.year = new_year
                ep.plot = s_data.get("overview") or ep.plot
                ep.rating = s_data.get("vote_average") or ep.rating
                if poster_filename:
                    ep.poster_filename = poster_filename
                if backdrop_filename:
                    ep.backdrop_filename = backdrop_filename
                if cast:
                    ep.cast = cast
                if director:
                    ep.director = director
                if logo_filename:
                    ep.logo_filename = logo_filename
                if content_rating:
                    ep.content_rating = content_rating
                if genres_str:
                    ep.genres = genres_str
                if ep.season in season_posters and season_posters[ep.season]:
                    ep.season_poster_filename = season_posters[ep.season]
                session.add(ep)

            session.commit()
            return {"success": True, "title": new_title}
        else:
            raise HTTPException(status_code=400, detail="Invalid media type")
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to fix match: {str(e)}"})
    finally:
        if raw_client is not None:
            await raw_client.aclose()

@app.post("/settings/remote_access")
async def save_remote_access(
    remote_url: str = Form(""),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    config = session.exec(select(SystemConfig).where(SystemConfig.key == "remote_access_url")).first()
    if not config:
        config = SystemConfig(key="remote_access_url", value=remote_url.strip())
        session.add(config)
    else:
        config.value = remote_url.strip()
        session.add(config)
    session.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/add_volume")
async def add_volume(
    path: str = Form(...),
    media_type: str = Form(...),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    clean_path = path.strip()
    if len(clean_path) > 1 and clean_path.endswith(('/', '\\')):
        clean_path = clean_path.rstrip('/\\')
    if clean_path and media_type in ("movie", "tv", "photo", "music"):
        existing = session.exec(select(VolumePath).where(VolumePath.path == clean_path)).first()
        if not existing:
            session.add(VolumePath(path=clean_path, media_type=media_type))
            session.commit()
            start_watcher()
            scanner_debouncer.trigger_scan(delay=0.5)
        else:
            if existing.media_type != media_type:
                existing.media_type = media_type
                session.add(existing)
                session.commit()
            start_watcher()
            scanner_debouncer.trigger_scan(delay=0.5)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/delete_volume/{volume_id}")
async def delete_volume(
    volume_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    vol = session.get(VolumePath, volume_id)
    if vol:
        session.delete(vol)
        session.commit()
        if scanner_manager.is_scanning:
            scanner_manager.check_scan_necessary()
        else:
            scanner_manager._cleanup_orphans()
        start_watcher()
        scanner_debouncer.trigger_scan(delay=0.5)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/rescan")
async def rescan_library(
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    scanner_manager.start_scan()
    return RedirectResponse(url="/settings", status_code=303)

@app.get("/api/suggest_volume_path")
async def suggest_volume_path(
    media_type: str,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")

    clean_type = media_type.strip().lower()
    if clean_type not in ("movie", "tv", "photo", "music"):
        return {"suggested_path": None}

    TYPE_ALIASES = {
        "tv": {"tv", "tv shows", "tvshows", "shows", "series", "television"},
        "movie": {"movies", "movie", "films", "film"},
        "photo": {"photos", "photo", "pictures", "pics", "images"},
        "music": {"music", "songs", "tracks", "audio", "tunes"}
    }
    targets = TYPE_ALIASES.get(clean_type, set())

    SYSTEM_DIRS = {
        "bin", "boot", "dev", "etc", "lib", "lib64", "proc", "root", "run",
        "sbin", "sys", "tmp", "usr", "var", "app", "config", "opt", "srv", "home",
        ".git", "node_modules", "__pycache__", ".agents", ".pytest_cache"
    }

    existing_volumes = session.exec(select(VolumePath)).all()
    if any(v.media_type == clean_type for v in existing_volumes):
        return {"suggested_path": None, "already_added": True}

    existing_paths = {v.path.strip().rstrip("/\\").replace("\\", "/").lower() for v in existing_volumes}

    candidate_roots = []
    # 1. Existing volume parents and roots
    for v in existing_volumes:
        v_clean = v.path.strip().rstrip("/\\")
        p = os.path.dirname(v_clean)
        if p and os.path.isdir(p):
            candidate_roots.append(p)
        if os.path.isdir(v_clean):
            candidate_roots.append(v_clean)

    # 2. Common media mount points in Docker / Linux
    for p in ["/media", "/mnt", "/data"]:
        if os.path.isdir(p):
            candidate_roots.append(p)

    # 3. Project-local media directory if exists
    local_media = os.path.abspath("media")
    if os.path.isdir(local_media):
        candidate_roots.append(local_media)

    # 4. Windows common drives
    if os.name == "nt":
        for d in ["C:", "D:", "E:", "F:"]:
            for sub in ["/media", "/mnt", "/data"]:
                win_path = f"{d}{sub}"
                if os.path.isdir(win_path):
                    candidate_roots.append(win_path)

    # 5. Root filesystem
    if os.path.isdir("/"):
        candidate_roots.append("/")

    # Deduplicate candidate roots
    unique_roots = []
    seen = set()
    for r in candidate_roots:
        norm = os.path.normpath(r).replace("\\", "/").lower()
        if norm not in seen and os.path.isdir(r):
            seen.add(norm)
            unique_roots.append(r)

    fallback_match = None
    inspected_count = 0
    max_inspected = 150

    for root_dir in unique_roots:
        max_depth = 3 if (root_dir == "/" or root_dir.replace("\\", "/") == "/") else 2
        queue = [(root_dir, 0)]
        visited_in_root = set()

        while queue and inspected_count < max_inspected:
            curr_dir, depth = queue.pop(0)
            norm_curr = os.path.normpath(curr_dir).replace("\\", "/").lower()
            if norm_curr in visited_in_root:
                continue
            visited_in_root.add(norm_curr)
            inspected_count += 1

            try:
                entries = os.listdir(curr_dir)
            except (OSError, PermissionError):
                continue

            for name in entries:
                if name.startswith("."):
                    continue
                if (curr_dir == "/" or curr_dir.replace("\\", "/") == "/") and name.lower() in SYSTEM_DIRS:
                    continue

                clean_name = re.sub(r'[\s\-_]+', ' ', name.lower().strip())
                full_path = os.path.join(curr_dir, name)

                try:
                    if not os.path.isdir(full_path):
                        continue
                except (OSError, PermissionError):
                    continue

                if clean_name in targets:
                    formatted_path = full_path.replace("\\", "/")
                    if formatted_path.lower() not in existing_paths:
                        return {"suggested_path": formatted_path}
                    elif not fallback_match:
                        fallback_match = formatted_path

                if depth + 1 < max_depth and clean_name not in SYSTEM_DIRS:
                    queue.append((full_path, depth + 1))

    return {"suggested_path": fallback_match}

@app.get("/api/browse")
async def browse_directory(
    path: str = "/",
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    SYSTEM_CONTAINER_DIRS = {
        "bin", "boot", "dev", "etc", "lib", "lib64", "proc", "root", "run",
        "sbin", "sys", "tmp", "usr", "var", "app", "config", "opt", "srv", "home"
    }
    try:
        abs_path = os.path.abspath(path)
        norm_path = abs_path.replace("\\", "/")

        # Prevent navigating into internal container system directories
        parts = [p for p in norm_path.split("/") if p]
        if parts and parts[0] in SYSTEM_CONTAINER_DIRS:
            return {"current": norm_path, "directories": [".."]}

        if not os.path.isdir(abs_path): return {"current": norm_path, "directories": []}
        dirs = []
        if abs_path != os.path.dirname(abs_path):
            dirs.append("..")
        for f in os.listdir(abs_path):
            if f.startswith("."):
                continue
            # When browsing root, hide Linux container system folders so only mounted volumes appear
            if abs_path == "/" or norm_path == "/":
                if f in SYSTEM_CONTAINER_DIRS:
                    continue
            try:
                if os.path.isdir(os.path.join(abs_path, f)):
                    dirs.append(f)
            except: pass
        return {"current": norm_path, "directories": sorted(dirs)}
    except:
        return {"current": path.replace("\\", "/"), "directories": []}

@app.post("/settings/generate_invite")
async def generate_invite(
    remote_url: str = Form(None),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    if remote_url is not None:
        config = session.exec(select(SystemConfig).where(SystemConfig.key == "remote_access_url")).first()
        if not config:
            config = SystemConfig(key="remote_access_url", value=remote_url.strip())
            session.add(config)
        else:
            config.value = remote_url.strip()
            session.add(config)
            
    session.add(InviteCode())
    session.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/delete_invite/{invite_id}")
async def delete_invite(
    invite_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    invite = session.get(InviteCode, invite_id)
    if invite:
        session.delete(invite)
        session.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/update_invite/{invite_id}")
async def update_invite(
    invite_id: int,
    movies_access: str = Form(None),
    tv_access: str = Form(None),
    photos_access: str = Form(None),
    music_access: str = Form(None),
    downloads_access: str = Form(None),
    watch_together_access: str = Form(None),
    kids_mode: str = Form(None),
    can_manage_profiles: str = Form(None),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    invite = session.get(InviteCode, invite_id)
    if invite:
        v_paths = session.exec(select(VolumePath)).all()
        has_movies = any(v.media_type == "movie" and os.path.exists(v.path) for v in v_paths)
        has_tv = any(v.media_type == "tv" and os.path.exists(v.path) for v in v_paths)
        has_photos = any(v.media_type == "photo" and os.path.exists(v.path) for v in v_paths)
        has_music = any(v.media_type == "music" and os.path.exists(v.path) for v in v_paths)

        if has_movies:
            invite.movies_access = movies_access == "on"
        if has_tv:
            invite.tv_access = tv_access == "on"
        if has_photos:
            invite.photos_access = photos_access == "on"
        if has_music:
            invite.music_access = music_access == "on"

        invite.downloads_access = downloads_access == "on"
        invite.watch_together_access = watch_together_access == "on"
        invite.can_manage_profiles = can_manage_profiles == "on"
        session.add(invite)
        session.commit()
    return {"success": True}

@app.post("/settings/update_user/{user_id}")
async def update_user(
    user_id: int,
    movies_access: str = Form(None),
    tv_access: str = Form(None),
    photos_access: str = Form(None),
    music_access: str = Form(None),
    downloads_access: str = Form(None),
    watch_together_access: str = Form(None),
    kids_mode: str = Form(None),
    can_manage_profiles: str = Form(None),
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    user = session.get(User, user_id)
    if user and not user.is_admin:
        v_paths = session.exec(select(VolumePath)).all()
        has_movies = any(v.media_type == "movie" and os.path.exists(v.path) for v in v_paths)
        has_tv = any(v.media_type == "tv" and os.path.exists(v.path) for v in v_paths)
        has_photos = any(v.media_type == "photo" and os.path.exists(v.path) for v in v_paths)
        has_music = any(v.media_type == "music" and os.path.exists(v.path) for v in v_paths)

        if has_movies:
            user.movies_access = movies_access == "on"
        if has_tv:
            user.tv_access = tv_access == "on"
        if has_photos:
            user.photos_access = photos_access == "on"
        if has_music:
            user.music_access = music_access == "on"

        user.downloads_access = downloads_access == "on"
        user.watch_together_access = watch_together_access == "on"
        user.can_manage_profiles = can_manage_profiles == "on"
        session.add(user)
        session.commit()
    return {"success": True}

@app.post("/settings/delete_user/{user_id}")
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not is_actual_admin(current_user, current_profile): raise HTTPException(status_code=403, detail="Forbidden")
    user = session.get(User, user_id)
    if user and not user.is_admin:
        for p in session.exec(select(Profile).where(Profile.user_id == user_id)).all(): session.delete(p)
        for w in session.exec(select(Watchlist).where(Watchlist.user_id == user_id)).all(): session.delete(w)
        for h in session.exec(select(WatchHistory).where(WatchHistory.user_id == user_id)).all(): session.delete(h)
        for i in session.exec(select(WatchInvite).where((WatchInvite.sender_id == user_id) | (WatchInvite.invited_user_id == user_id))).all(): session.delete(i)
        session.delete(user)
        session.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.get("/ping")
async def ping(response: Response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return {"status": "ok", "app": "heirloom", "server_time": time.time()}

_speedtest_chunk = os.urandom(65536)

@app.get("/speedtest")
async def speedtest(request: Request, size: int = 1048576):
    """
    Lightweight speedtest endpoint for measuring client download bandwidth.
    Streams non-cacheable binary data chunks of requested size (default 1MB, min 64KB, max 10MB).
    """
    client_ip = get_client_ip(request)
    rate_key = f"speedtest_{client_ip}"
    check_rate_limit(rate_key, max_attempts=15, window_seconds=60, error_message="Speedtest rate limit exceeded. Please wait a minute.")
    record_failed_attempt(rate_key)

    target_size = max(65536, min(10485760, size))
    def iter_bytes():
        bytes_sent = 0
        chunk_len = len(_speedtest_chunk)
        while bytes_sent < target_size:
            to_send = min(chunk_len, target_size - bytes_sent)
            yield _speedtest_chunk[:to_send]
            bytes_sent += to_send

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Content-Length": str(target_size),
        "Access-Control-Allow-Origin": "*"
    }
    return StreamingResponse(iter_bytes(), media_type="application/octet-stream", headers=headers)
# --- Watch Together Routes ---
@app.websocket("/ws/sync/{session_id}")
async def websocket_sync(websocket: WebSocket, session_id: str):
    token = websocket.cookies.get("session_token")
    active_pid_str = websocket.cookies.get("active_profile_id")
    current_user = None
    current_profile = None
    is_host = False
    with Session(engine) as db:
        if token:
            current_user = db.exec(select(User).where(User.session_token == token)).first()
        if current_user and active_pid_str:
            try:
                p = db.get(Profile, int(active_pid_str))
                if p and p.user_id == current_user.id:
                    current_profile = p
            except Exception:
                pass
        if current_user and not current_profile:
            current_profile = db.exec(select(Profile).where(Profile.user_id == current_user.id, Profile.is_primary == True)).first()

        ws_session = db.get(WatchSession, session_id)
        if not ws_session or not ws_session.is_active:
            await websocket.close(code=1000, reason="Session ended")
            return
        if current_user:
            if ws_session.host_profile_id and current_profile:
                is_host = (ws_session.host_profile_id == current_profile.id)
            else:
                is_host = (ws_session.host_id == current_user.id)

    display_name = current_profile.name if current_profile else (current_user.username if current_user else ("Host" if is_host else "Guest"))
    await manager.connect(
        websocket, session_id,
        user_id=current_user.id if current_user else None,
        username=display_name,
        is_host=is_host
    )

    # Send initial room state to newly connected client
    try:
        playback = manager.session_playback.get(session_id, {"current_time": 0.0, "is_paused": True})
        await websocket.send_text(json.dumps({
            "action": "room_state",
            "time": playback.get("current_time", 0.0),
            "paused": playback.get("is_paused", True),
            "all_ready": manager.check_all_ready(session_id),
            "waiting_users": manager.get_waiting_users(session_id),
            "is_host": is_host
        }))
        if not manager.check_all_ready(session_id):
            await manager.broadcast(json.dumps({
                "action": "waiting_for",
                "users": manager.get_waiting_users(session_id)
            }), session_id, sender=websocket)
    except Exception:
        pass

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                action = msg.get("action")

                if action == "ping":
                    await websocket.send_text(json.dumps({
                        "action": "pong",
                        "client_time": msg.get("client_time", 0),
                        "server_time": time.time() * 1000
                    }))
                    continue

                client_info = manager.session_clients.get(session_id, {}).get(websocket, {})

                if action == "ready":
                    client_info["is_ready"] = True
                    client_info["time"] = msg.get("time", 0.0)
                    if client_info.get("is_host"):
                        manager.session_playback[session_id]["current_time"] = msg.get("time", manager.session_playback[session_id]["current_time"])

                    if manager.check_all_ready(session_id):
                        playback = manager.session_playback.get(session_id, {"current_time": 0.0, "is_paused": True})
                        ready_msg = json.dumps({
                            "action": "all_ready",
                            "time": playback.get("current_time", 0.0),
                            "paused": playback.get("is_paused", True)
                        })
                        await manager.broadcast(ready_msg, session_id)
                        await websocket.send_text(ready_msg)
                    else:
                        await manager.broadcast(json.dumps({
                            "action": "waiting_for",
                            "users": manager.get_waiting_users(session_id)
                        }), session_id)
                    continue

                elif action == "loading":
                    client_info["is_ready"] = False
                    await manager.broadcast(json.dumps({
                        "action": "waiting_for",
                        "users": manager.get_waiting_users(session_id)
                    }), session_id)
                    continue

                elif action == "play":
                    if manager.check_all_ready(session_id):
                        manager.session_playback[session_id]["is_paused"] = False
                        if "time" in msg and isinstance(msg["time"], (int, float)):
                            manager.session_playback[session_id]["current_time"] = float(msg["time"])
                        if "username" not in msg or not msg["username"]:
                            msg["username"] = client_info.get("username", "Someone")
                        await manager.broadcast(json.dumps(msg), session_id, sender=websocket)
                    else:
                        await websocket.send_text(json.dumps({
                            "action": "waiting_for",
                            "users": manager.get_waiting_users(session_id)
                        }))
                    continue

                elif action == "pause":
                    manager.session_playback[session_id]["is_paused"] = True
                    if "time" in msg and isinstance(msg["time"], (int, float)):
                        manager.session_playback[session_id]["current_time"] = float(msg["time"])
                    if "username" not in msg or not msg["username"]:
                        msg["username"] = client_info.get("username", "Someone")
                    await manager.broadcast(json.dumps(msg), session_id, sender=websocket)
                    continue

                elif action == "seek":
                    if "time" in msg and isinstance(msg["time"], (int, float)):
                        manager.session_playback[session_id]["current_time"] = float(msg["time"])
                    # Seeking requires all participants to re-buffer the new position
                    for c in manager.session_clients.get(session_id, {}).values():
                        c["is_ready"] = False
                    await manager.broadcast(data, session_id, sender=websocket)
                    await manager.broadcast(json.dumps({
                        "action": "waiting_for",
                        "users": manager.get_waiting_users(session_id)
                    }), session_id)
                    continue

                elif action == "sync":
                    if client_info.get("is_host") and "time" in msg and isinstance(msg["time"], (int, float)):
                        manager.session_playback[session_id]["current_time"] = float(msg["time"])
                    await manager.broadcast(data, session_id, sender=websocket)
                    continue

            except Exception:
                pass
    except WebSocketDisconnect:
        was_host, remaining = manager.disconnect(websocket, session_id)
        if was_host:
            with Session(engine) as db:
                ws_session = db.get(WatchSession, session_id)
                if ws_session:
                    ws_session.is_active = False
                    db.add(ws_session)
                    for inv in db.exec(select(WatchInvite).where(WatchInvite.session_id == session_id)).all():
                        if inv.status == "pending":
                            inv.status = "expired"
                            db.add(inv)
                    db.commit()
            await manager.broadcast(json.dumps({
                "action": "session_ended",
                "reason": "The host has left or ended the watch session."
            }), session_id)
        else:
            if remaining > 0 and manager.check_all_ready(session_id):
                playback = manager.session_playback.get(session_id, {"current_time": 0.0, "is_paused": True})
                await manager.broadcast(json.dumps({
                    "action": "all_ready",
                    "time": playback.get("current_time", 0.0),
                    "paused": playback.get("is_paused", True)
                }), session_id)

@app.get("/api/inbox")
async def get_inbox(
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: return {"invites": [], "reset_requests": []}
    if not current_profile:
        current_profile = session.exec(select(Profile).where(Profile.user_id == current_user.id, Profile.is_primary == True)).first()

    raw_invites = session.exec(select(WatchInvite).where(WatchInvite.invited_user_id == current_user.id).where(WatchInvite.status == "pending")).all()
    results = []
    now = datetime.now(timezone.utc)
    for inv in raw_invites:
        # Check profile targeting:
        if inv.invited_profile_id is not None:
            if not current_profile or current_profile.id != inv.invited_profile_id:
                continue
        else:
            # General account-level invite: visible to primary profile (or if no profile)
            if current_profile and not current_profile.is_primary:
                continue

        # Prevent seeing invite sent by the current active profile to avoid self-invites
        if inv.sender_id == current_user.id and current_profile and inv.sender_profile_id == current_profile.id:
            continue

        sender = session.get(User, inv.sender_id)
        sender_profile = session.get(Profile, inv.sender_profile_id) if inv.sender_profile_id else None
        ws_session = session.get(WatchSession, inv.session_id)
        if not ws_session or not sender:
            inv.status = "expired"
            session.add(inv)
            session.commit()
            continue

        created = ws_session.created_at.replace(tzinfo=timezone.utc) if ws_session.created_at.tzinfo is None else ws_session.created_at
        grace_period = (now - created).total_seconds() < 90
        has_active_conns = inv.session_id in manager.active_connections and len(manager.active_connections[inv.session_id]) > 0

        # Discard and expire invites for sessions that have ended or have no active host
        if not ws_session.is_active or (not has_active_conns and not grace_period):
            inv.status = "expired"
            session.add(inv)
            session.commit()
            continue

        title = "Unknown Media"
        if ws_session.media_type == "movie":
            m = session.get(Movie, ws_session.item_id)
            if m: title = m.title
        elif ws_session.media_type == "tv":
            t = session.get(TVShow, ws_session.item_id)
            if t: title = t.title

        sender_display = sender_profile.name if (sender_profile and sender_profile.name) else (sender.username if sender else "Someone")

        results.append({
            "id": inv.id,
            "sender": sender_display,
            "media_type": ws_session.media_type,
            "title": title
        })

    reset_requests = []
    if is_actual_admin(current_user, current_profile):
        pending_resets = session.exec(select(PasswordResetRequest).where(PasswordResetRequest.status == "pending")).all()
        for pr in pending_resets:
            req_user = session.get(User, pr.user_id)
            if req_user:
                reset_requests.append({
                    "id": pr.id,
                    "username": req_user.username,
                    "created_at": pr.created_at.strftime("%m/%d/%y %I:%M %p")
                })

    return {"invites": results, "reset_requests": reset_requests}

class WatchTogetherRequest(BaseModel):
    media_type: str
    item_id: int
    invited_user_ids: list[Union[int, str]] = []
    invited_targets: Optional[list[str]] = None

@app.post("/watch_together/create")
async def create_watch_together(
    req: WatchTogetherRequest,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.watch_together_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    if not current_profile:
        current_profile = session.exec(select(Profile).where(Profile.user_id == current_user.id, Profile.is_primary == True)).first()

    ws_session = WatchSession(
        host_id=current_user.id,
        host_profile_id=current_profile.id if current_profile else None,
        media_type=req.media_type,
        item_id=req.item_id,
        is_active=True
    )
    session.add(ws_session)
    session.commit()

    raw_targets = req.invited_targets if req.invited_targets is not None else req.invited_user_ids
    for target in raw_targets:
        t_str = str(target).strip()
        if not t_str:
            continue

        target_user = None
        target_profile = None

        if t_str.startswith("profile:"):
            try:
                pid = int(t_str.split(":", 1)[1])
                target_profile = session.get(Profile, pid)
            except Exception:
                pass
        elif t_str.startswith("user:"):
            try:
                uid = int(t_str.split(":", 1)[1])
                target_user = session.get(User, uid)
            except Exception:
                pass
        else:
            try:
                val = int(t_str)
                prof = session.exec(select(Profile).where(Profile.id == val, Profile.user_id == current_user.id)).first()
                if prof:
                    target_profile = prof
                else:
                    target_user = session.get(User, val)
            except Exception:
                pass

        if target_profile:
            # Target profile must belong to the caller's household and not be caller's active profile
            if target_profile.user_id == current_user.id and (not current_profile or target_profile.id != current_profile.id):
                invite = WatchInvite(
                    session_id=ws_session.id,
                    sender_id=current_user.id,
                    sender_profile_id=current_profile.id if current_profile else None,
                    invited_user_id=current_user.id,
                    invited_profile_id=target_profile.id
                )
                session.add(invite)
        elif target_user:
            if not target_user.watch_together_access or target_user.id == current_user.id:
                continue
            # Non-admins can only invite server admins; admins can invite any user
            if not current_user.is_admin and not target_user.is_admin:
                continue
            if req.media_type == "movie" and not target_user.movies_access:
                continue
            if req.media_type == "tv" and not target_user.tv_access:
                continue

            invite = WatchInvite(
                session_id=ws_session.id,
                sender_id=current_user.id,
                sender_profile_id=current_profile.id if current_profile else None,
                invited_user_id=target_user.id,
                invited_profile_id=None
            )
            session.add(invite)

    session.commit()
    return {"session_id": ws_session.id}

@app.post("/watch_together/accept/{invite_id}")
async def accept_watch_together(
    invite_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=403, detail="Forbidden")
    invite = session.get(WatchInvite, invite_id)
    if invite and invite.invited_user_id == current_user.id:
        if invite.invited_profile_id is not None:
            if not current_profile:
                current_profile = session.exec(select(Profile).where(Profile.user_id == current_user.id, Profile.is_primary == True)).first()
            if current_profile and current_profile.id != invite.invited_profile_id:
                return {"success": False, "error": "This invite was sent to a different profile."}

        ws_session = session.get(WatchSession, invite.session_id)
        now = datetime.now(timezone.utc)
        created = ws_session.created_at.replace(tzinfo=timezone.utc) if ws_session and ws_session.created_at.tzinfo is None else (ws_session.created_at if ws_session else now)
        grace_period = (now - created).total_seconds() < 90
        has_active_conns = invite.session_id in manager.active_connections and len(manager.active_connections[invite.session_id]) > 0

        if not ws_session or not ws_session.is_active or (not has_active_conns and not grace_period):
            invite.status = "expired"
            session.add(invite)
            session.commit()
            return {"success": False, "error": "This watch party has ended."}

        invite.status = "accepted"
        session.commit()
        return {"success": True, "session_id": invite.session_id}
    return {"success": False}

@app.post("/watch_together/decline/{invite_id}")
async def decline_watch_together(
    invite_id: int,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=403, detail="Forbidden")
    invite = session.get(WatchInvite, invite_id)
    if invite and invite.invited_user_id == current_user.id:
        if invite.invited_profile_id is not None:
            if not current_profile:
                current_profile = session.exec(select(Profile).where(Profile.user_id == current_user.id, Profile.is_primary == True)).first()
            if current_profile and current_profile.id != invite.invited_profile_id:
                return {"success": False, "error": "This invite was sent to a different profile."}
        invite.status = "declined"
        session.commit()
        return {"success": True}
    return {"success": False}

@app.post("/watch_together/end/{session_id}")
async def end_watch_together(
    session_id: str,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=403, detail="Forbidden")
    if not current_profile:
        current_profile = session.exec(select(Profile).where(Profile.user_id == current_user.id, Profile.is_primary == True)).first()

    ws_session = session.get(WatchSession, session_id)
    if not ws_session:
        return {"success": False}

    is_host = False
    if ws_session.host_profile_id and current_profile:
        is_host = (ws_session.host_profile_id == current_profile.id)
    else:
        is_host = (ws_session.host_id == current_user.id)

    if is_host:
        ws_session.is_active = False
        session.add(ws_session)
        for inv in session.exec(select(WatchInvite).where(WatchInvite.session_id == session_id)).all():
            if inv.status == "pending":
                inv.status = "expired"
                session.add(inv)
        session.commit()
        await manager.broadcast(json.dumps({
            "action": "session_ended",
            "reason": "The host has ended this watch session."
        }), session_id)
        return {"success": True}
    return {"success": False}

@app.get("/play_sync/{session_id}", response_class=HTMLResponse)
async def play_sync(request: Request, session_id: str, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.watch_together_access:
        return RedirectResponse(url="/", status_code=303)

    ws_session = session.get(WatchSession, session_id)
    if not ws_session or not ws_session.is_active:
        return RedirectResponse(url="/?session_ended=1", status_code=303)

    return RedirectResponse(url=f"/play/{ws_session.media_type}/{ws_session.item_id}?sync={session_id}", status_code=303)

@app.get("/api/users/eligible", response_model=list[dict])
async def get_eligible_users(
    media_type: str,
    current_user: User = Depends(get_current_user),
    current_profile: Optional[Profile] = Depends(get_current_profile),
    session: Session = Depends(get_session)
):
    if not current_user or not current_user.watch_together_access:
        return []

    if media_type == "movie" and not current_user.movies_access:
        return []
    if media_type == "tv" and not current_user.tv_access:
        return []

    if not current_profile:
        current_profile = session.exec(select(Profile).where(Profile.user_id == current_user.id, Profile.is_primary == True)).first()

    eligible = []

    # 1. Household profiles: the rest of the people in their household
    household_profiles = session.exec(select(Profile).where(Profile.user_id == current_user.id)).all()
    for p in household_profiles:
        if current_profile and p.id == current_profile.id:
            continue
        eligible.append({
            "id": f"profile:{p.id}",
            "username": p.name,
            "type": "profile",
            "category": "Household",
            "tag": "Household"
        })

    # 2. Shared users
    if current_user.is_admin:
        other_users = session.exec(
            select(User).where(User.id != current_user.id).where(User.watch_together_access == True)
        ).all()
        for u in other_users:
            if media_type == "movie" and not u.movies_access:
                continue
            if media_type == "tv" and not u.tv_access:
                continue
            eligible.append({
                "id": f"user:{u.id}",
                "username": u.username,
                "type": "user",
                "category": "Shared",
                "tag": "Shared"
            })
    else:
        admin_users = session.exec(
            select(User).where(User.is_admin == True).where(User.watch_together_access == True)
        ).all()
        for admin in admin_users:
            if admin.id == current_user.id:
                continue
            if media_type == "movie" and not admin.movies_access:
                continue
            if media_type == "tv" and not admin.tv_access:
                continue
            eligible.append({
                "id": f"user:{admin.id}",
                "username": admin.username,
                "type": "user",
                "category": "Shared",
                "tag": "Shared"
            })

    return eligible
