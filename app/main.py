from fastapi import FastAPI, Depends, Form, Request, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Optional
from sqlalchemy import text
from sqlmodel import Session, select, func
import bcrypt
import os
import uuid
import json
import time
from datetime import datetime, timezone, timedelta
from .database import create_db_and_tables, get_session, engine
from .models import User, Movie, TVShow, InviteCode, Watchlist, WatchHistory, VolumePath, SystemConfig, Photo, WatchSession, WatchInvite, PasswordResetRequest
from .scanner import scan_media_library
from .watcher import start_watcher, stop_watcher, scanner_debouncer
from .ffmpeg_setup import ensure_ffmpeg, get_ffmpeg_path, get_ffprobe_path, get_video_transcode_args, get_working_encoder, probe_media_streams
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
            session.commit()
        except Exception:
            session.rollback()
                
        # Ensure federation key exists
        fed_key = session.exec(select(SystemConfig).where(SystemConfig.key == "federation_api_key")).first()
        if not fed_key:
            fed_key = SystemConfig(key="federation_api_key", value=str(uuid.uuid4()))
            session.add(fed_key)
            session.commit()
            
    import threading
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

def get_current_user(request: Request, session: Session = Depends(get_session)):
    token = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    if not token:
        token = request.query_params.get("token")
    if not token:
        return None
    return session.exec(select(User).where(User.session_token == token)).first()

# Rate limiting for auth
failed_attempts = {}

def check_rate_limit(request: Request):
    ip = request.client.host
    now = datetime.now(timezone.utc)
    # Clean up old entries
    for k in list(failed_attempts.keys()):
        if (now - failed_attempts[k]["last_attempt"]).total_seconds() > 60:
            del failed_attempts[k]
            
    if ip in failed_attempts:
        if failed_attempts[ip]["count"] >= 5:
            if (now - failed_attempts[ip]["last_attempt"]).total_seconds() < 60:
                raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a minute.")
            else:
                del failed_attempts[ip]

def record_failed_attempt(request: Request):
    ip = request.client.host
    now = datetime.now(timezone.utc)
    if ip not in failed_attempts:
        failed_attempts[ip] = {"count": 1, "last_attempt": now}
    else:
        failed_attempts[ip]["count"] += 1
        failed_attempts[ip]["last_attempt"] = now

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
async def process_login(request: Request, username: str = Form(...), password: str = Form(...), session: Session = Depends(get_session)):
    check_rate_limit(request)
    user = session.exec(select(User).where(User.username == username)).first()
    if user and bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8')):
        user.session_token = str(uuid.uuid4())
        session.add(user)
        session.commit()
        redirect = RedirectResponse(url="/", status_code=303)
        redirect.set_cookie(key="session_token", value=user.session_token, httponly=True)
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
        user.session_token = str(uuid.uuid4())
        session.add(user)
        session.commit()
        return {"success": True, "token": user.session_token, "user": {"id": user.id, "username": user.username}}
    record_failed_attempt(request)
    return JSONResponse(status_code=401, content={"success": False, "error": "Invalid credentials"})


@app.post("/logout")
async def process_logout(response: Response, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if current_user:
        current_user.session_token = None
        session.add(current_user)
        session.commit()
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie("session_token")
    return redirect

@app.post("/forgot_password")
async def request_password_reset(request: Request, username: str = Form(...), session: Session = Depends(get_session)):
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
async def process_reset_password(token: str, password: str = Form(...), confirm_password: Optional[str] = Form(None), session: Session = Depends(get_session)):
    if confirm_password is not None and password != confirm_password:
        return RedirectResponse(url=f"/reset_password/{token}?error=passwords_mismatch", status_code=303)
    reset_req = session.exec(select(PasswordResetRequest).where(PasswordResetRequest.token == token, PasswordResetRequest.status == "approved")).first()
    if not reset_req:
        return RedirectResponse(url="/login?reset_error=1", status_code=303)
    user = session.get(User, reset_req.user_id)
    if user:
        user.password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        user.session_token = None
        session.add(user)
        reset_req.status = "used"
        session.add(reset_req)
        session.commit()
    return RedirectResponse(url="/login?reset_success=1", status_code=303)

@app.post("/password_reset/approve/{request_id}")
async def approve_password_reset(request_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    reset_req = session.get(PasswordResetRequest, request_id)
    if reset_req and reset_req.status == "pending":
        reset_req.status = "approved"
        session.add(reset_req)
        session.commit()
    return {"success": True}

@app.post("/password_reset/deny/{request_id}")
async def deny_password_reset(request_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    reset_req = session.get(PasswordResetRequest, request_id)
    if reset_req and reset_req.status == "pending":
        reset_req.status = "denied"
        session.add(reset_req)
        session.commit()
    return {"success": True}


@app.post("/setup")
async def process_setup(username: str = Form(...), password: str = Form(...), confirm_password: Optional[str] = Form(None), session: Session = Depends(get_session)):
    if session.exec(select(User)).first(): 
        return RedirectResponse(url="/", status_code=303)
    if confirm_password is not None and password != confirm_password:
        return RedirectResponse(url="/login?error=passwords_mismatch", status_code=303)
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    new_user = User(username=username, password_hash=hashed, is_admin=True, session_token=str(uuid.uuid4()))
    session.add(new_user)
    session.commit()
    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(key="session_token", value=new_user.session_token, httponly=True)
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
    if session.exec(select(User).where(User.username == username)).first():
        raise HTTPException(status_code=400, detail="Username taken")
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    new_user = User(username=username, password_hash=hashed, is_admin=False, session_token=str(uuid.uuid4()), kids_mode=False)
    session.add(new_user)
    session.delete(invite)
    session.commit()
    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(key="session_token", value=new_user.session_token, httponly=True)
    return redirect

# ----------------- DASHBOARD & MEDIA -----------------

import re
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

def is_kids_safe(rating: str) -> bool:
    if not rating: return False
    return rating.strip().upper() in {"G", "PG", "TV-Y", "TV-Y7", "TV-G", "TV-PG"}

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not session.exec(select(User)).first():
        return templates.TemplateResponse(request=request, name="setup.html")
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    movies = session.exec(select(Movie).order_by(Movie.id.desc())).all() if current_user.movies_access else []
    tv_shows_raw = session.exec(select(TVShow).order_by(TVShow.id.desc())).all() if current_user.tv_access else []    
    if current_user.kids_mode:
        movies = [m for m in movies if is_kids_safe(m.content_rating)]
        tv_shows_raw = [t for t in tv_shows_raw if is_kids_safe(t.content_rating)]
    photos = session.exec(select(Photo).order_by(Photo.id.desc())).all() if current_user.photos_access else []
    recent_movies = movies[:15]
    recent_photos = photos[:15]
    
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
            "episode_name": ""
        }
        if count > 1:
            t['year'] = f"{count} Episodes"
        else:
            m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', t['file_path'])
            if m:
                base = f"S{int(m.group(1))} • E{int(m.group(2))}"
                filename = os.path.splitext(os.path.basename(t['file_path']))[0]
                name_match = re.search(r'[Ss]\d+\s*[-_]?\s*[Ee]?\d+[ \-]*(.+)', filename, re.IGNORECASE)
                if name_match and name_match.group(1).strip():
                    t['year'] = f"{base}<br>{name_match.group(1).strip()}"
                else:
                    t['year'] = base
        recent_tv.append(t)
    
    watchlist_db = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id)).all()
    watchlist = []
    for w in watchlist_db:
        if w.media_type == 'movie' and not current_user.movies_access: continue
        if w.media_type == 'tv' and not current_user.tv_access: continue
        item = session.get(Movie if w.media_type == 'movie' else TVShow, w.item_id)
        if item: watchlist.append({"db_id": w.id, "item": item, "media_type": w.media_type})

    history_db = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.progress < 95.0).order_by(WatchHistory.last_watched.desc()).limit(15)).all()
    history = []
    for h in history_db:
        if h.media_type == 'movie' and not current_user.movies_access: continue
        if h.media_type == 'tv' and not current_user.tv_access: continue
        item_obj = session.get(Movie if h.media_type == 'movie' else TVShow, h.item_id)
        if item_obj:
            item = {
                "id": item_obj.id,
                "title": item_obj.title,
                "poster_filename": item_obj.poster_filename,
                "file_path": item_obj.file_path,
                "year": getattr(item_obj, "year", "")
            }
            if h.media_type == 'tv':
                m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', item['file_path'])
                if m:
                    base = f"S{int(m.group(1))} • E{int(m.group(2))}"
                    filename = os.path.splitext(os.path.basename(item['file_path']))[0]
                    name_match = re.search(r'[Ss]\d+\s*[-_]?\s*[Ee]?\d+[ \-]*(.+)', filename, re.IGNORECASE)
                    if name_match and name_match.group(1).strip():
                        item['year'] = f"{base}<br>{name_match.group(1).strip()}"
                    else:
                        item['year'] = base
            
            history.append({"item": item, "media_type": h.media_type, "progress": h.progress})
    
    user_watchlist_movies = [w.item_id for w in watchlist_db if w.media_type == 'movie']
    user_watchlist_tv = [w.item_id for w in watchlist_db if w.media_type == 'tv']
    
    import random
    from collections import Counter
    history_db_full = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).order_by(WatchHistory.last_watched.desc()).limit(100)).all()
    watched_movie_ids = set()
    watched_tv_titles = set()
    genre_counts = Counter()
    for h in history_db_full:
        item_obj = session.get(Movie if h.media_type == 'movie' else TVShow, h.item_id)
        if item_obj:
            if h.media_type == 'movie': watched_movie_ids.add(h.item_id)
            else: watched_tv_titles.add(item_obj.title.strip().lower())
            if item_obj.genres:
                for g in item_obj.genres.split(','): genre_counts[g.strip()] += 1
                
    top_genres = [g for g, _ in genre_counts.most_common(3)]
    recommended = []
    if top_genres:
        candidate_movies = [m for m in movies if m.id not in watched_movie_ids and m.genres and any(g in m.genres for g in top_genres)]
        unique_shows = process_tv_shows(tv_shows_raw)
        candidate_shows = [s for s in unique_shows if s.title.strip().lower() not in watched_tv_titles and s.genres and any(g in s.genres for g in top_genres)]
        
        for m in candidate_movies:
            recommended.append({"item": {"id": m.id, "title": m.title, "poster_filename": m.poster_filename, "year": m.year}, "media_type": "movie"})
        for s in candidate_shows:
            recommended.append({"item": {"id": s.id, "title": s.title, "poster_filename": s.poster_filename, "year": getattr(s, 'year', '')}, "media_type": "tv"})
            
        random.shuffle(recommended)
        recommended = recommended[:15]

    if "application/json" in request.headers.get("Accept", ""):
        return {
            "user": {"id": current_user.id, "username": current_user.username, "is_admin": current_user.is_admin, "watch_together_access": current_user.watch_together_access, "downloads_access": current_user.downloads_access},
            "recent_movies": recent_movies,
            "recent_tv": recent_tv,
            "recent_photos": recent_photos,
            "watchlist": watchlist,
            "history": history,
            "recommended": recommended,
            "user_watchlist_movies": user_watchlist_movies,
            "user_watchlist_tv": user_watchlist_tv
        }

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "user": current_user, "recent_movies": recent_movies, "recent_tv": recent_tv, "recent_photos": recent_photos, "watchlist": watchlist, "history": history,
        "recommended": recommended,
        "user_watchlist_movies": user_watchlist_movies, "user_watchlist_tv": user_watchlist_tv
    })

@app.get("/api/library/{media_type}")
async def api_library(
    request: Request, media_type: str, page: int = 1, limit: int = 50, 
    search: str = "", sort: str = "title_asc",
    current_user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    
    if media_type == "movie":
        if not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
        query = select(Movie)
        if current_user.kids_mode:
            query = query.where(Movie.content_rating.in_(["G", "PG", "TV-Y", "TV-Y7", "TV-G", "TV-PG"]))
        if search:
            query = query.where(Movie.title.ilike(f"%{search}%"))
            
        if sort == "title_asc": query = query.order_by(Movie.title.asc())
        elif sort == "title_desc": query = query.order_by(Movie.title.desc())
        elif sort == "year_desc": query = query.order_by(Movie.year.desc())
        elif sort == "year_asc": query = query.order_by(Movie.year.asc())
        elif sort == "date_desc": query = query.order_by(Movie.id.desc())
        elif sort == "date_asc": query = query.order_by(Movie.id.asc())
        
        items = session.exec(query.offset((page - 1) * limit).limit(limit)).all()
    elif media_type == "tv":
        if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
        query = select(TVShow)
        if current_user.kids_mode:
            query = query.where(TVShow.content_rating.in_(["G", "PG", "TV-Y", "TV-Y7", "TV-G", "TV-PG"]))
        if search:
            query = query.where(TVShow.title.ilike(f"%{search}%"))
        
        raw_items = session.exec(query).all()
        processed = process_tv_shows(raw_items)
        
        if sort == "title_asc": processed.sort(key=lambda x: x.title.lower())
        elif sort == "title_desc": processed.sort(key=lambda x: x.title.lower(), reverse=True)
        elif sort == "year_desc": processed.sort(key=lambda x: str(x.year) if x.year else "", reverse=True)
        elif sort == "year_asc": processed.sort(key=lambda x: str(x.year) if x.year else "")
        elif sort == "date_desc": processed.sort(key=lambda x: x.id, reverse=True)
        elif sort == "date_asc": processed.sort(key=lambda x: x.id)
        
        items = processed[(page - 1) * limit : page * limit]
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
    else:
        raise HTTPException(status_code=404, detail="Not Found")
        
    watchlist_db = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.media_type == media_type)).all()
    user_watchlist = {w.item_id for w in watchlist_db}
    
    import urllib.parse
    result = []
    
    if media_type == "photo":
        album_filter = request.query_params.get("album")
        if album_filter is not None:
            query = select(Photo).where(Photo.album == album_filter)
            if search:
                query = query.where(Photo.title.ilike(f"%{search}%"))
            if sort == "title_asc": query = query.order_by(Photo.title.asc())
            elif sort == "title_desc": query = query.order_by(Photo.title.desc())
            elif sort == "date_desc": query = query.order_by(Photo.date_added.desc())
            elif sort == "date_asc": query = query.order_by(Photo.date_added.asc())
            
            items = session.exec(query.offset((page - 1) * limit).limit(limit)).all()
            for item in items:
                result.append({
                    "id": item.id,
                    "title": item.title,
                    "type": "photo",
                    "year": item.date_added.strftime("%b %d, %Y") if hasattr(item, 'date_added') else None,
                    "poster_filename": item.poster_filename,
                    "album": item.album,
                    "url": f"/photo/{item.id}",
                    "in_watchlist": False
                })
        else:
            # Group by album and list root photos
            all_items = []
            albums = session.exec(select(Photo.album).where(Photo.album != None).distinct()).all()
            for alb in albums:
                if search and search.lower() not in alb.lower(): continue
                first_photo = session.exec(select(Photo).where(Photo.album == alb).order_by(Photo.date_added.desc())).first()
                if first_photo:
                    all_items.append({
                        "id": alb,
                        "title": alb,
                        "type": "album",
                        "year": "",
                        "poster_filename": first_photo.poster_filename,
                        "album": alb,
                        "url": f"/library/photo?album={urllib.parse.quote(alb)}",
                        "in_watchlist": False,
                        "date": first_photo.date_added
                    })
            
            query = select(Photo).where(Photo.album == None)
            if search: query = query.where(Photo.title.ilike(f"%{search}%"))
            root_photos = session.exec(query).all()
            for item in root_photos:
                all_items.append({
                    "id": item.id,
                    "title": item.title,
                    "type": "photo",
                    "year": item.date_added.strftime("%b %d, %Y") if hasattr(item, 'date_added') else None,
                    "poster_filename": item.poster_filename,
                    "album": None,
                    "url": f"/photo/{item.id}",
                    "in_watchlist": False,
                    "date": item.date_added
                })
                
            if sort == "title_asc": all_items.sort(key=lambda x: x["title"].lower())
            elif sort == "title_desc": all_items.sort(key=lambda x: x["title"].lower(), reverse=True)
            elif sort == "date_desc": all_items.sort(key=lambda x: x["date"], reverse=True)
            elif sort == "date_asc": all_items.sort(key=lambda x: x["date"])
            
            result = all_items[(page - 1) * limit : page * limit]
    else:
        for item in items:
            if media_type == "tv":
                url_path = f"/series/{urllib.parse.quote(item.title)}"
            else:
                url_path = f"/movie/{item.id}"
            
            result.append({
                "id": item.id,
                "title": item.title,
                "year": item.year if hasattr(item, 'year') else None,
                "poster_filename": item.poster_filename if hasattr(item, 'poster_filename') else None,
                "album": None,
                "url": url_path,
                "in_watchlist": item.id in user_watchlist if hasattr(item, 'id') else False
            })
        
    return {"items": result}

@app.get("/api/me")
async def api_me(current_user: User = Depends(get_current_user)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    return {"id": current_user.id, "username": current_user.username}

@app.get("/api/dashboard")
async def api_dashboard(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    
    movies = session.exec(select(Movie).order_by(Movie.id.desc())).all() if current_user.movies_access else []
    tv_shows_raw = session.exec(select(TVShow).order_by(TVShow.id.desc())).all() if current_user.tv_access else []    
    if current_user.kids_mode:
        movies = [m for m in movies if is_kids_safe(m.content_rating)]
        tv_shows_raw = [t for t in tv_shows_raw if is_kids_safe(t.content_rating)]
        
    recent_movies = [{"id": m.id, "title": m.title, "poster_filename": m.poster_filename, "year": m.year} for m in movies[:15]]
    
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
        
    watchlist_db = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id)).all()
    watchlist = []
    for w in watchlist_db:
        if w.media_type == 'movie' and not current_user.movies_access: continue
        if w.media_type == 'tv' and not current_user.tv_access: continue
        item_obj = session.get(Movie if w.media_type == 'movie' else TVShow, w.item_id)
        if item_obj:
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
        
    history_db = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.progress < 95.0).order_by(WatchHistory.last_watched.desc()).limit(15)).all()
    history = []
    for h in history_db:
        if h.media_type == 'movie' and not current_user.movies_access: continue
        if h.media_type == 'tv' and not current_user.tv_access: continue
        item_obj = session.get(Movie if h.media_type == 'movie' else TVShow, h.item_id)
        if item_obj:
            item = {
                "id": item_obj.id,
                "title": item_obj.title,
                "poster_filename": item_obj.poster_filename,
                "file_path": item_obj.file_path,
                "year": getattr(item_obj, "year", "")
            }
            if h.media_type == 'tv':
                m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', item['file_path'])
                if m:
                    base = f"S{int(m.group(1))} E{int(m.group(2))}"
                    item['year'] = base
            
            history.append({"item": item, "media_type": h.media_type, "progress": h.progress})

    import random
    from collections import Counter
    history_db_full = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).order_by(WatchHistory.last_watched.desc()).limit(100)).all()
    watched_movie_ids = set()
    watched_tv_titles = set()
    genre_counts = Counter()
    for h in history_db_full:
        item_obj = session.get(Movie if h.media_type == 'movie' else TVShow, h.item_id)
        if item_obj:
            if h.media_type == 'movie': watched_movie_ids.add(h.item_id)
            else: watched_tv_titles.add(item_obj.title.strip().lower())
            if item_obj.genres:
                for g in item_obj.genres.split(','): genre_counts[g.strip()] += 1
                
    top_genres = [g for g, _ in genre_counts.most_common(3)]
    recommended = []
    if top_genres:
        candidate_movies = [m for m in movies if m.id not in watched_movie_ids and m.genres and any(g in m.genres for g in top_genres)]
        
        # Unique shows
        seen_titles = set()
        unique_shows = []
        for t in tv_shows_raw:
            if t.title not in seen_titles:
                seen_titles.add(t.title)
                unique_shows.append(t)
                
        candidate_shows = [s for s in unique_shows if s.title.strip().lower() not in watched_tv_titles and s.genres and any(g in s.genres for g in top_genres)]
        
        for m in candidate_movies:
            recommended.append({"item": {"id": m.id, "title": m.title, "poster_filename": m.poster_filename, "year": m.year}, "media_type": "movie"})
        for s in candidate_shows:
            recommended.append({"item": {"id": s.id, "title": s.title, "poster_filename": s.poster_filename, "year": getattr(s, 'year', '')}, "media_type": "tv"})
            
        random.shuffle(recommended)
        recommended = recommended[:15]

    photos = session.exec(select(Photo).order_by(Photo.date_added.desc())).all() if current_user.photos_access else []
    recent_photos = [{"id": p.id, "title": p.title, "poster_filename": p.poster_filename, "album": p.album} for p in photos[:15]]

    return {
        "recent_movies": recent_movies,
        "recent_tv": recent_tv,
        "history": history,
        "watchlist": watchlist,
        "recommended": recommended,
        "recent_photos": recent_photos
    }

@app.get("/api/movie/{movie_id}")
async def api_movie(movie_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    movie = session.get(Movie, movie_id)
    if not movie: raise HTTPException(status_code=404, detail="Movie not found")
    if current_user.kids_mode and not is_kids_safe(movie.content_rating):
        raise HTTPException(status_code=403, detail="Not appropriate for Kids Mode")
        
    cast_list = []
    if movie.cast and movie.cast.startswith("["):
        try:
            import json
            cast_list = json.loads(movie.cast)
        except: pass
        
    history = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == movie.id).where(WatchHistory.media_type == 'movie')).first()
    has_progress = history.progress > 0 if history and history.progress else False
    
    in_watchlist = bool(session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.item_id == movie.id).where(Watchlist.media_type == 'movie')).first())
    
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
async def api_series(series_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    show = session.get(TVShow, series_id)
    if not show: raise HTTPException(status_code=404, detail="Show not found")
    
    if current_user.kids_mode and not is_kids_safe(show.content_rating):
        raise HTTPException(status_code=403, detail="Not appropriate for Kids Mode")
        
    cast_list = []
    if show.cast and show.cast.startswith("["):
        try:
            import json
            cast_list = json.loads(show.cast)
        except: pass

    # Get all episodes with the same title
    episodes = session.exec(select(TVShow).where(TVShow.title == show.title).order_by(TVShow.file_path.asc())).all()
    
    import re, os
    seasons = {}
    for ep in episodes:
        m = re.search(r'[Ss](\d+)\s*[-_]?\s*[Ee]?(\d+)', ep.file_path)
        season_num = int(m.group(1)) if m else 1
        ep_num = int(m.group(2)) if m else ep.id
        
        if season_num not in seasons:
            seasons[season_num] = []
            
        history = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == ep.id).where(WatchHistory.media_type == 'tv')).first()
        
        seasons[season_num].append({
            "id": ep.id,
            "season": season_num,
            "episode": ep_num,
            "file_path": ep.file_path,
            "has_progress": history.progress > 0 if history and history.progress else False,
            "progress": history.progress if history else 0
        })
        
    in_watchlist = bool(session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.item_id == show.id).where(Watchlist.media_type == 'tv')).first())
    
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
async def api_search(
    request: Request, q: str = "",
    current_user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not q: return {"items": []}
    
    import urllib.parse
    results = []
    if current_user.movies_access:
        query = select(Movie).where(Movie.title.ilike(f"%{q}%"))
        if current_user.kids_mode:
            query = query.where(Movie.content_rating.in_(["G", "PG", "TV-Y", "TV-Y7", "TV-G", "TV-PG"]))
        movies = session.exec(query.limit(10)).all()
        for m in movies:
            results.append({
                "id": m.id, "title": m.title, "type": "movie", "url": f"/movie/{m.id}",
                "poster": m.poster_filename, "year": m.year
            })
            
    if current_user.tv_access:
        query = select(TVShow).where(TVShow.title.ilike(f"%{q}%"))
        if current_user.kids_mode:
            query = query.where(TVShow.content_rating.in_(["G", "PG", "TV-Y", "TV-Y7", "TV-G", "TV-PG"]))
        shows = session.exec(query).all()
        processed = process_tv_shows(shows)
        for t in processed[:10]:
            results.append({
                "id": t.id, "title": t.title, "type": "tv", "url": f"/series/{urllib.parse.quote(t.title)}",
                "poster": t.poster_filename, "year": t.year
            })
            
    if current_user.photos_access:
        photos = session.exec(select(Photo).where(Photo.title.ilike(f"%{q}%")).limit(10)).all()
        for p in photos:
            results.append({
                "id": p.id, "title": p.title, "type": "photo", "url": f"/photo/{p.id}",
                "poster": p.poster_filename, "year": p.date_added.strftime("%Y") if hasattr(p, 'date_added') else ""
            })
            
    # Sort results by title roughly
    results.sort(key=lambda x: x["title"].lower())
            
    return {"items": results}

@app.get("/photo/{item_id}", response_class=HTMLResponse)
async def photo_view(request: Request, item_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if not current_user.photos_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    photo = session.get(Photo, item_id)
    if not photo: raise HTTPException(status_code=404, detail="Photo not found")
        
    ext = os.path.splitext(photo.file_path)[1].lower()
    is_video = ext in (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm")
    
    prev_photo = session.exec(select(Photo).where(Photo.id < item_id).order_by(Photo.id.desc())).first()
    next_photo = session.exec(select(Photo).where(Photo.id > item_id).order_by(Photo.id.asc())).first()

    return templates.TemplateResponse(request=request, name="photo.html", context={
        "user": current_user, "photo": photo, "is_video": is_video,
        "prev_photo_id": prev_photo.id if prev_photo else None,
        "next_photo_id": next_photo.id if next_photo else None
    })

@app.get("/photo_file/{item_id}")
async def get_photo_file(item_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.photos_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    photo = session.get(Photo, item_id)
    if not photo or not os.path.exists(photo.file_path): raise HTTPException(status_code=404, detail="File missing")
    
    return FileResponse(photo.file_path)

@app.get("/library/{media_type}", response_class=HTMLResponse)
async def library_view(request: Request, media_type: str, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    
    if media_type == "movie":
        if not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
        total_count = session.exec(select(func.count(Movie.id))).one()
    elif media_type == "tv":
        if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
        total_count = session.exec(select(func.count(func.distinct(TVShow.title)))).one()
    elif media_type == "photo":
        if not current_user.photos_access: raise HTTPException(status_code=403, detail="Forbidden")
        total_count = session.exec(select(func.count(Photo.id))).one()
    else:
        raise HTTPException(status_code=404, detail="Not Found")
        
    album = request.query_params.get("album")
    return templates.TemplateResponse(request=request, name="library.html", context={
        "user": current_user, "media_type": media_type, "total_count": total_count, "album": album
    })
@app.get("/movie/{movie_id}", response_class=HTMLResponse)
async def movie_view(request: Request, movie_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    movie = session.get(Movie, movie_id)
    if not movie: raise HTTPException(status_code=404, detail="Movie not found")
    if current_user.kids_mode and not is_kids_safe(movie.content_rating):
        raise HTTPException(status_code=403, detail="Not appropriate for Kids Mode")
        
    cast_list = []
    if movie.cast and movie.cast.startswith("["):
        try:
            import json
            cast_list = json.loads(movie.cast)
        except: pass
        
    history = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == movie.id).where(WatchHistory.media_type == 'movie')).first()
    has_progress = history.progress > 0 if history and history.progress else False
    
    in_watchlist = bool(session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.item_id == movie.id).where(Watchlist.media_type == 'movie')).first())

    rt_str = get_runtime_str(movie, session)
    if rt_str:
        movie.year = f"{movie.year} • {rt_str}" if movie.year else rt_str

    return templates.TemplateResponse(request=request, name="movie.html", context={
        "user": current_user, "movie": movie, "cast_list": cast_list, "has_progress": has_progress, "in_watchlist": in_watchlist
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

@app.get("/series/{title}", response_class=HTMLResponse)
async def series_view(request: Request, title: str, ep: Optional[int] = None, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    shows = session.exec(select(TVShow).where(TVShow.title == title)).all()
    if not shows:
        raise HTTPException(status_code=404, detail="Series not found")
    if current_user.kids_mode and not is_kids_safe(shows[0].content_rating):
        raise HTTPException(status_code=403, detail="Not appropriate for Kids Mode")
        
    cast_list = []
    if shows[0].cast and shows[0].cast.startswith("["):
        try:
            import json
            cast_list = json.loads(shows[0].cast)
        except: pass
        
    series_ep_ids = [e.id for e in shows]
    history = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id.in_(series_ep_ids)).where(WatchHistory.media_type == 'tv').order_by(WatchHistory.last_watched.desc())).first()
    
    has_progress = False
    resume_link = ""
    selected_episode = None
    
    if ep:
        # User requested a specific episode
        selected_episode = next((e for e in shows if e.id == ep), None)
        if selected_episode:
            resume_link = f"/play/tv/{selected_episode.id}"
            
    if not selected_episode:
        # Default behavior: use first episode or history
        selected_episode = shows[0]
        if history:
            has_progress = True
            resume_link = f"/play/tv/{history.item_id}"
        else:
            resume_link = f"/play/tv/{selected_episode.id}"
        
    in_watchlist = bool(session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.item_id == selected_episode.id).where(Watchlist.media_type == 'tv')).first())
        
    def get_s_e(t):
        m = re.search(r'S(\d+)E(\d+)', t.file_path, re.IGNORECASE)
        if m: return (int(m.group(1)), int(m.group(2)))
        return (0, 0)
        
    episodes = []
    seasons = set()
    for ep_obj in sorted(shows, key=get_s_e):
        ep = {
            "id": ep_obj.id,
            "title": ep_obj.title,
            "poster_filename": ep_obj.poster_filename,
            "season_poster_filename": ep_obj.season_poster_filename,
            "file_path": ep_obj.file_path,
            "season_episode": "",
            "episode_name": "",
            "season": 0
        }
        m = re.search(r'S(\d+)E(\d+)', ep['file_path'], re.IGNORECASE)
        if m:
            s_num = int(m.group(1))
            ep["season"] = s_num
            seasons.add(s_num)
            base = f"Season {int(m.group(1))} Episode {int(m.group(2))}"
        else:
            seasons.add(0)
            base = "Special"
        
        rt_str = get_runtime_str(ep_obj, session)
        rt_suffix = f" • {rt_str}" if rt_str else ""
        
        filename = os.path.splitext(os.path.basename(ep['file_path']))[0]
        name_match = re.search(r'S\d+E\d+[ \-]*(.+)', filename, re.IGNORECASE)
        
        if name_match and name_match.group(1).strip():
            ep['episode_name'] = name_match.group(1).strip()
            ep['season_episode'] = f"{ep['episode_name']} • {base}{rt_suffix}"
        else:
            ep['season_episode'] = f"{base}{rt_suffix}"
            
        episodes.append(ep)
    
    sorted_seasons = sorted(list(seasons))
    
    selected_ep_dict = next((e for e in episodes if e['id'] == selected_episode.id), None)
    
    if not resume_link and episodes:
        resume_link = f"/play/tv/{episodes[0]['id']}"

    return templates.TemplateResponse(request=request, name="series.html", context={
        "user": current_user, "title": title, "episodes": episodes, "seasons": sorted_seasons, "series": selected_episode, "cast_list": cast_list,
        "has_progress": has_progress, "in_watchlist": in_watchlist, "resume_link": resume_link, "selected_ep_id": selected_episode.id,
        "selected_ep_dict": selected_ep_dict
    })

@app.get("/play/{media_type}/{item_id}", response_class=HTMLResponse)
async def play_video(request: Request, media_type: str, item_id: int, sync: str = None, mobile: str = None, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if media_type == "movie" and not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    item = session.get(Movie if media_type == "movie" else TVShow, item_id)
    if not item: raise HTTPException(status_code=404, detail="Item not found")
    if current_user.kids_mode and not is_kids_safe(item.content_rating):
        raise HTTPException(status_code=403, detail="Not appropriate for Kids Mode")
        
    history_entry = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == item_id).where(WatchHistory.media_type == media_type)).first()
    if history_entry:
        history_entry.last_watched = datetime.now(timezone.utc)
    else:
        history_entry = WatchHistory(user_id=current_user.id, media_type=media_type, item_id=item_id)
        session.add(history_entry)
        
    session.commit()
    
    next_episode_id = None
    subtitle_url = None
    
    base_path = os.path.splitext(item.file_path)[0]
    if os.path.exists(base_path + ".srt"):
        subtitle_url = f"/subtitle/{media_type}/{item.id}?ext=srt"
    elif os.path.exists(base_path + ".vtt"):
        subtitle_url = f"/subtitle/{media_type}/{item.id}?ext=vtt"

    season_episode = ""
    if media_type == "tv":
        m = re.search(r'S(\d+)E(\d+)', item.file_path, re.IGNORECASE)
        if m: 
            season_episode = f"S{int(m.group(1))} • E{int(m.group(2))}"
            filename = os.path.splitext(os.path.basename(item.file_path))[0]
            name_match = re.search(r'S\d+E\d+[ \-]*(.+)', filename, re.IGNORECASE)
            if name_match and name_match.group(1).strip():
                season_episode += f"<br>{name_match.group(1).strip()}"
                
        # Clean up older episodes from continue watching
        series_episodes = session.exec(select(TVShow).where(TVShow.title == item.title)).all()
        series_ep_ids = [ep.id for ep in series_episodes]
        old_history = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id.in_(series_ep_ids)).where(WatchHistory.id != history_entry.id)).all()
        for old_h in old_history:
            session.delete(old_h)
        session.commit()
        
        def get_s_e(t):
            m = re.search(r'S(\d+)E(\d+)', t.file_path, re.IGNORECASE)
            if m: return (int(m.group(1)), int(m.group(2)))
            return (0, 0)
            
        sorted_eps = sorted(series_episodes, key=get_s_e)
        for i, ep in enumerate(sorted_eps):
            if ep.id == item_id:
                if i + 1 < len(sorted_eps):
                    next_episode_id = sorted_eps[i+1].id
                break
        
    probe = probe_media_streams(item.file_path)
    audio_tracks = probe.get("audio_tracks", [])
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
    if sync:
        ws_session = session.get(WatchSession, sync)
        if not ws_session or not ws_session.is_active:
            return RedirectResponse(url="/?session_ended=1", status_code=303)
        is_sync_host = (ws_session.host_id == current_user.id)
        if is_sync_host:
            host_start = 0.0
            if history_entry and duration and history_entry.progress > 0 and history_entry.progress < 100:
                host_start = (history_entry.progress / 100.0) * duration
                if current_user.rewind_seconds:
                    host_start = max(0.0, host_start - current_user.rewind_seconds)
            if sync not in manager.session_playback or manager.session_playback[sync].get("current_time", 0.0) == 0.0:
                manager.session_playback[sync] = {"current_time": host_start, "is_paused": True, "updated_at": time.time()}
            sync_initial_time = manager.session_playback[sync]["current_time"]
        else:
            sync_initial_time = manager.session_playback.get(sync, {}).get("current_time", 0.0)
            if sync_initial_time == 0.0:
                host_hist = session.exec(select(WatchHistory).where(WatchHistory.user_id == ws_session.host_id).where(WatchHistory.item_id == item_id).where(WatchHistory.media_type == media_type)).first()
                if host_hist and duration and host_hist.progress > 0:
                    sync_initial_time = (host_hist.progress / 100.0) * duration
                    if sync not in manager.session_playback:
                        manager.session_playback[sync] = {"current_time": sync_initial_time, "is_paused": True, "updated_at": time.time()}

    # Register active stream telemetry immediately
    session_key = f"{current_user.id}_{media_type}_{item_id}"
    item_title = item.title
    item_poster = getattr(item, 'season_poster_filename', None) or getattr(item, 'poster_filename', None)
    season_ep_clean = season_episode.replace("<br>", " - ") if season_episode else ""
    client_ip = request.client.host if request.client else ""
    active_stream_manager.register_or_update(
        session_key=session_key,
        user_id=current_user.id,
        username=current_user.username,
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

    return templates.TemplateResponse(request=request, name="player.html", context={
        "item": item, "media_type": media_type, "history": history_entry, "season_episode": season_episode,
        "duration": duration, "next_episode_id": next_episode_id, "subtitle_url": subtitle_url,
        "sync_session_id": sync, "is_sync_host": is_sync_host, "sync_initial_time": sync_initial_time,
        "user": current_user, "token": token, "is_mobile": mobile == "1", "audio_tracks": audio_tracks
    })

@app.get("/subtitle/{media_type}/{item_id}")
async def get_subtitle(media_type: str, item_id: int, ext: str, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return Response(status_code=403)
    item = session.get(Movie if media_type == "movie" else TVShow, item_id)
    if not item: return Response(status_code=404)
    
    sub_file = os.path.splitext(item.file_path)[0] + "." + ext
    if not os.path.exists(sub_file):
        return Response(status_code=404)
        
    if ext == "srt":
        try:
            with open(sub_file, "r", encoding="utf-8", errors="ignore") as f:
                srt_content = f.read()
            # Convert SRT timestamps to VTT format
            vtt_content = "WEBVTT\n\n" + re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', srt_content)
            return Response(content=vtt_content, media_type="text/vtt")
        except:
            return Response(status_code=500)
            
    return FileResponse(sub_file, media_type="text/vtt")

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
    session: Session = Depends(get_session)
):
    if not current_user: return {"status": "unauthorized"}
    
    session_key = f"{current_user.id}_{media_type}_{item_id}"
    history_entry = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == item_id).where(WatchHistory.media_type == media_type)).first()
    
    if progress >= 95.0:
        active_stream_manager.stop_stream(session_key)
        if history_entry:
            session.delete(history_entry)
            session.commit()
        return {"status": "completed"}

    cur_time_val = float(current_time) if current_time is not None else 0.0
    dur_val = float(duration) if duration is not None else 0.0

    if history_entry:
        history_entry.progress = progress
        if current_time is not None:
            history_entry.current_time = cur_time_val
        if duration is not None and dur_val > 0:
            history_entry.duration = dur_val
        history_entry.last_watched = datetime.now(timezone.utc)
        session.add(history_entry)
        session.commit()
    else:
        history_entry = WatchHistory(
            user_id=current_user.id,
            media_type=media_type,
            item_id=item_id,
            progress=progress,
            current_time=cur_time_val,
            duration=dur_val,
            last_watched=datetime.now(timezone.utc)
        )
        session.add(history_entry)
        session.commit()

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
    active_stream_manager.register_or_update(
        session_key=session_key,
        user_id=current_user.id,
        username=current_user.username,
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
async def stop_progress(media_type: str, item_id: int, current_user: User = Depends(get_current_user)):
    if current_user:
        session_key = f"{current_user.id}_{media_type}_{item_id}"
        active_stream_manager.stop_stream(session_key)
    return {"status": "stopped"}

from fastapi.responses import StreamingResponse

@app.get("/stream/{media_type}/{item_id}")
async def stream_video(media_type: str, item_id: int, res: str = "original", start: float = 0.0, audio_track: int = 0, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if media_type == "movie" and not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    item = session.get(Movie if media_type == "movie" else TVShow, item_id)
    if not item or not os.path.exists(item.file_path): raise HTTPException(status_code=404, detail="Video file missing")
    
    hw_config = session.exec(select(SystemConfig).where(SystemConfig.key == "hw_accel_profile")).first()
    hw_profile = hw_config.value if hw_config else "auto"

    file_ext = os.path.splitext(item.file_path)[1].lower().lstrip(".")
    is_mp4 = (file_ext in ["mp4", "m4v"])
    probe = probe_media_streams(item.file_path)
    audio_tracks = probe.get("audio_tracks", [])

    selected_track = None
    if 0 <= audio_track < len(audio_tracks):
        selected_track = audio_tracks[audio_track]
    elif audio_tracks:
        selected_track = audio_tracks[0]
        audio_track = 0

    track_can_copy = selected_track.get("can_direct_stream", False) if selected_track else False
    track_label = selected_track.get("label", f"Track {audio_track + 1}") if selected_track else "None"

    # 1. Native Direct Play:
    # Requires original resolution, MP4 container, start at 0:00, primary audio track (audio_track == 0),
    # and web-compatible video & audio codecs.
    is_direct_play = (
        res == "original" and
        is_mp4 and
        start == 0.0 and
        audio_track == 0 and
        probe.get("can_direct_stream_video", True) and
        track_can_copy
    )
    if is_direct_play:
        headers = {
            "X-Transcode-Active": "false",
            "X-Transcode-Method": "Direct Play",
            "X-Hardware-Accelerated": "false",
            "X-Encoder": "none",
            "X-Play-Type": "direct_play",
            "X-Audio-Track": str(audio_track),
        }
        return FileResponse(item.file_path, media_type="video/mp4", headers=headers)

    # 2. Smart Remuxing / Direct Stream:
    # When video codec is already web-compatible (H.264) and native resolution is requested,
    # stream-copy the video (-c:v copy). Map the specific chosen audio track (-map 0:a:N).
    can_direct_stream_video = (res == "original" and probe.get("can_direct_stream_video", False))

    cmd = [get_ffmpeg_path()]
    if start > 0.0:
        cmd.extend(["-ss", str(start)])
    cmd.extend(["-i", item.file_path])
    if start > 0.0 and can_direct_stream_video:
        cmd.extend(["-avoid_negative_ts", "make_zero"])

    # Stream mapping: map primary video and chosen audio track
    cmd.extend(["-map", "0:v:0"])
    if selected_track:
        cmd.extend(["-map", f"0:a:{audio_track}"])
    else:
        cmd.append("-an")

    if can_direct_stream_video:
        cmd.extend(["-c:v", "copy"])
        is_transcode = False
        encoder_type = "copy"
        hw_accel = False
        
        if not selected_track:
            transcode_method = "Direct Stream (Video Copy, No Audio)"
            play_type = "remux"
        elif track_can_copy:
            cmd.extend(["-c:a", "copy"])
            transcode_method = f"Direct Stream / Remux (Stream Copy, Audio: {track_label})"
            play_type = "remux"
        else:
            # Video is copied directly; chosen audio track is downmixed/transcoded to stereo AAC
            cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-af", "aresample=async=1"])
            transcode_method = f"Direct Stream (Video Copy, Audio: {track_label} -> AAC)"
            encoder_type = "copy (audio: aac)"
            play_type = "direct_stream"

        cmd.extend([
            "-max_muxing_queue_size", "1024",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1"
        ])
    else:
        # 3. Full Video Transcoding:
        # Required when downscaling resolution (e.g. 720p/480p) or when source video codec
        # is incompatible with direct browser playback (e.g. MPEG2, VC-1, VP8).
        encoder_type, _ = get_working_encoder(profile=hw_profile)
        transcode_args = get_video_transcode_args(res=res, profile=hw_profile)
        cmd.extend(transcode_args)
        
        if selected_track:
            cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-af", "aresample=async=1"])
            
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
        transcode_method = f"{base_method} (Audio: {track_label})"
        play_type = "transcode"

    headers = {
        "X-Transcode-Active": "true" if is_transcode else "false",
        "X-Transcode-Method": transcode_method,
        "X-Hardware-Accelerated": "true" if hw_accel else "false",
        "X-Encoder": encoder_type,
        "X-Play-Type": play_type,
        "X-Audio-Track": str(audio_track),
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
async def get_stream_info(media_type: str, item_id: int, res: str = "original", start: float = 0.0, audio_track: int = 0, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if media_type == "movie" and not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    item = session.get(Movie if media_type == "movie" else TVShow, item_id)
    if not item or not os.path.exists(item.file_path): raise HTTPException(status_code=404, detail="Video file missing")
    
    hw_config = session.exec(select(SystemConfig).where(SystemConfig.key == "hw_accel_profile")).first()
    hw_profile = hw_config.value if hw_config else "auto"

    file_ext = os.path.splitext(item.file_path)[1].lower().lstrip(".")
    is_mp4 = (file_ext in ["mp4", "m4v"])
    probe = probe_media_streams(item.file_path)
    audio_tracks = probe.get("audio_tracks", [])

    selected_track = None
    if 0 <= audio_track < len(audio_tracks):
        selected_track = audio_tracks[audio_track]
    elif audio_tracks:
        selected_track = audio_tracks[0]
        audio_track = 0

    track_can_copy = selected_track.get("can_direct_stream", False) if selected_track else False
    track_label = selected_track.get("label", f"Track {audio_track + 1}") if selected_track else "None"

    if res == "original" and is_mp4 and start == 0.0 and audio_track == 0 and probe.get("can_direct_stream_video", True) and track_can_copy:
        is_transcode = False
        method = "Direct Play"
        encoder = "none"
        hw_accel = False
        play_type = "direct_play"
    elif res == "original" and probe.get("can_direct_stream_video", False):
        is_transcode = False
        hw_accel = False
        if not selected_track:
            method = "Direct Stream (Video Copy, No Audio)"
            encoder = "copy"
            play_type = "remux"
        elif track_can_copy:
            method = f"Direct Stream / Remux (Stream Copy, Audio: {track_label})"
            encoder = "copy"
            play_type = "remux"
        else:
            method = f"Direct Stream (Video Copy, Audio: {track_label} -> AAC)"
            encoder = "copy (audio: aac)"
            play_type = "direct_stream"
    else:
        encoder, _ = get_working_encoder(profile=hw_profile)
        is_transcode = True
        hw_accel = (encoder != "software")
        method_names = {
            "nvenc": "NVIDIA NVENC (h264_nvenc)",
            "qsv": "Intel QuickSync (h264_qsv)",
            "vaapi": "VA-API (h264_vaapi)",
            "videotoolbox": "Apple VideoToolbox (h264_videotoolbox)",
            "software": "Software CPU (libx264)"
        }
        base_method = method_names.get(encoder, encoder)
        method = f"{base_method} (Audio: {track_label})"
        play_type = "transcode"

    return {
        "media_type": media_type,
        "item_id": item_id,
        "title": item.title,
        "resolution": res,
        "start": start,
        "audio_track": audio_track,
        "audio_tracks": audio_tracks,
        "selected_audio_track": selected_track,
        "is_transcoding": is_transcode,
        "transcode_method": method,
        "encoder": encoder,
        "hardware_accelerated": hw_accel,
        "play_type": play_type,
        "container": file_ext,
        "video_codec": probe.get("video_codec"),
        "audio_codec": selected_track.get("codec") if selected_track else probe.get("audio_codec"),
        "audio_channels": selected_track.get("channels") if selected_track else probe.get("audio_channels"),
        "hw_profile": hw_profile,
        "server_time": time.time()
    }

@app.post("/watchlist/remove/{watchlist_id}")
async def remove_watchlist(request: Request, watchlist_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    item = session.get(Watchlist, watchlist_id)
    if item and item.user_id == current_user.id:
        session.delete(item)
        session.commit()
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

@app.post("/watchlist/remove_by_item/{media_type}/{item_id}")
async def remove_watchlist_by_item(request: Request, media_type: str, item_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    item = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.media_type == media_type).where(Watchlist.item_id == item_id)).first()
    if item:
        session.delete(item)
        session.commit()
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)
@app.post("/watchlist/add/{media_type}/{item_id}")
async def add_watchlist(request: Request, media_type: str, item_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    existing = session.exec(select(Watchlist).where(Watchlist.user_id == current_user.id).where(Watchlist.media_type == media_type).where(Watchlist.item_id == item_id)).first()
    if not existing:
        session.add(Watchlist(user_id=current_user.id, media_type=media_type, item_id=item_id))
        session.commit()
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

@app.post("/history/remove/{media_type}/{item_id}")
async def remove_history(request: Request, media_type: str, item_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user:
        if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.headers.get("accept", "").startswith("application/json"):
            return Response(content='{"status": "unauthorized"}', media_type="application/json", status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    history_entry = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == item_id).where(WatchHistory.media_type == media_type)).first()
    if history_entry:
        session.delete(history_entry)
        session.commit()
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.headers.get("accept", "").startswith("application/json"):
        return {"status": "success"}
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

# ----------------- SETTINGS & USER MANAGEMENT -----------------

@app.get("/download/{media_type}/{item_id}")
async def download_media(media_type: str, item_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if not current_user.downloads_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "movie" and not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    item = session.get(Movie if media_type == "movie" else TVShow, item_id)
    if not item or not os.path.exists(item.file_path): raise HTTPException(status_code=404, detail="Video file missing")
    
    filename = os.path.basename(item.file_path)
    return FileResponse(item.file_path, media_type="application/octet-stream", filename=filename)

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
                    display_type = {"movie": "Movies", "tv": "TV Shows", "photo": "Photos"}.get(vol.media_type, vol.media_type.capitalize())
                    disks.append({
                        "name": f"{display_type} ({vol.path})",
                        "path": vol.path,
                        "total_gb": round(du.total / (1024**3), 1),
                        "used_gb": round(du.used / (1024**3), 1),
                        "free_gb": round(du.free / (1024**3), 1),
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

def get_user_and_watch_stats(session: Session, limit: int = 25) -> dict:
    history_records = session.exec(
        select(WatchHistory).order_by(WatchHistory.last_watched.desc()).limit(limit)
    ).all()

    user_ids = {h.user_id for h in history_records}
    users = {u.id: u for u in session.exec(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}

    recent_history = []
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

        delta = now - h.last_watched if h.last_watched else timedelta(0)
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
async def server_dashboard_view(request: Request, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    if not current_user.is_admin:
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
async def api_server_dashboard_stats(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    metrics = get_server_metrics(session)
    user_stats = get_user_and_watch_stats(session, limit=15)
    return {
        "metrics": metrics,
        "stats": user_stats
    }

@app.get("/settings", response_class=HTMLResponse)
async def view_settings(request: Request, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    invites = session.exec(select(InviteCode).order_by(InviteCode.id.desc())).all()
    users = session.exec(select(User).order_by(User.id)).all()
    v_paths = session.exec(select(VolumePath).order_by(VolumePath.id)).all()
    volume_paths = []
    for v in v_paths:
        display_type = {"movie": "Movies", "tv": "TV Shows", "photo": "Photos"}.get(v.media_type, v.media_type)
        volume_paths.append({
            "id": v.id,
            "path": v.path,
            "display_path": v.path,
            "media_type": display_type,
            "exists": os.path.exists(v.path)
        })
    remote_url_config = session.exec(select(SystemConfig).where(SystemConfig.key == "remote_access_url")).first()
    host_url = remote_url_config.value.rstrip("/") if remote_url_config and remote_url_config.value else str(request.base_url).rstrip("/")
    
    tmdb_key_config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
    tmdb_key = tmdb_key_config.value if tmdb_key_config else os.getenv("TMDB_API_KEY", "")
    
    hw_accel_config = session.exec(select(SystemConfig).where(SystemConfig.key == "hw_accel_profile")).first()
    hw_accel_profile = hw_accel_config.value if hw_accel_config else "auto"
    
    return templates.TemplateResponse(request=request, name="settings.html", context={
        "user": current_user, "invites": invites, "users": users, "volume_paths": volume_paths, 
        "host_url": host_url, "remote_url": remote_url_config.value if remote_url_config else "",
        "tmdb_key": tmdb_key, "hw_accel_profile": hw_accel_profile
    })

@app.post("/settings/transcoding")
async def save_transcoding_settings(hw_accel_profile: str = Form("auto"), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    config = session.exec(select(SystemConfig).where(SystemConfig.key == "hw_accel_profile")).first()
    if not config:
        config = SystemConfig(key="hw_accel_profile", value=hw_accel_profile.strip())
        session.add(config)
    else:
        config.value = hw_accel_profile.strip()
        session.add(config)
    session.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/change_password")
async def change_password(new_password: str = Form(...), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')
    current_user.password_hash = hashed
    
    session.add(current_user)
    session.commit()
    
    return RedirectResponse(url="/settings?pw_success=1", status_code=303)

@app.post("/settings/rewind")
async def save_rewind_settings(rewind_seconds: int = Form(0), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    current_user.rewind_seconds = max(0, min(300, rewind_seconds))
    session.add(current_user)
    session.commit()
    return RedirectResponse(url="/settings?rewind_success=1", status_code=303)

@app.post("/settings/tmdb")
async def save_tmdb_key(tmdb_key: str = Form(""), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    config = session.exec(select(SystemConfig).where(SystemConfig.key == "tmdb_api_key")).first()
    if not config:
        config = SystemConfig(key="tmdb_api_key", value=tmdb_key.strip())
        session.add(config)
    else:
        config.value = tmdb_key.strip()
        session.add(config)
    session.commit()
    # Trigger a rescan to fetch metadata for existing items
    scanner_debouncer.trigger_scan(delay=0.5)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/remote_access")
async def save_remote_access(remote_url: str = Form(""), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
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
async def add_volume(path: str = Form(...), media_type: str = Form(...), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    clean_path = path.strip()
    if clean_path and media_type in ("movie", "tv", "photo"):
        existing = session.exec(select(VolumePath).where(VolumePath.path == clean_path)).first()
        if not existing:
            session.add(VolumePath(path=clean_path, media_type=media_type))
            session.commit()
            start_watcher()
            scanner_debouncer.trigger_scan(delay=0.5)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/delete_volume/{volume_id}")
async def delete_volume(volume_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    vol = session.get(VolumePath, volume_id)
    if vol:
        session.delete(vol)
        session.commit()
        start_watcher()
        scanner_debouncer.trigger_scan(delay=0.5)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/rescan")
async def rescan_library(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    scanner_debouncer.trigger_scan(delay=0.1)
    return RedirectResponse(url="/settings", status_code=303)

@app.get("/api/browse")
async def browse_directory(path: str = "/", current_user: User = Depends(get_current_user)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    try:
        abs_path = os.path.abspath(path)
        if not os.path.isdir(abs_path): return {"current": path, "directories": []}
        dirs = []
        if abs_path != os.path.dirname(abs_path):
            dirs.append("..")
        for f in os.listdir(abs_path):
            try:
                if os.path.isdir(os.path.join(abs_path, f)):
                    dirs.append(f)
            except: pass
        return {"current": abs_path.replace("\\", "/"), "directories": sorted(dirs)}
    except:
        return {"current": path.replace("\\", "/"), "directories": []}

@app.post("/settings/generate_invite")
async def generate_invite(remote_url: str = Form(None), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
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
async def delete_invite(invite_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    invite = session.get(InviteCode, invite_id)
    if invite:
        session.delete(invite)
        session.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/update_user/{user_id}")
async def update_user(user_id: int, movies_access: str = Form(None), tv_access: str = Form(None), photos_access: str = Form(None), downloads_access: str = Form(None), watch_together_access: str = Form(None), kids_mode: str = Form(None), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    user = session.get(User, user_id)
    if user and not user.is_admin:
        user.movies_access = movies_access == "on"
        user.tv_access = tv_access == "on"
        user.photos_access = photos_access == "on"
        user.downloads_access = downloads_access == "on"
        user.watch_together_access = watch_together_access == "on"
        user.kids_mode = kids_mode == "on"
        session.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/delete_user/{user_id}")
async def delete_user(user_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    user = session.get(User, user_id)
    if user and not user.is_admin:
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
# --- Watch Together Routes ---
@app.websocket("/ws/sync/{session_id}")
async def websocket_sync(websocket: WebSocket, session_id: str):
    token = websocket.cookies.get("session_token")
    current_user = None
    is_host = False
    with Session(engine) as db:
        if token:
            current_user = db.exec(select(User).where(User.session_token == token)).first()
        ws_session = db.get(WatchSession, session_id)
        if not ws_session or not ws_session.is_active:
            await websocket.close(code=1000, reason="Session ended")
            return
        if current_user:
            is_host = (ws_session.host_id == current_user.id)

    await manager.connect(
        websocket, session_id,
        user_id=current_user.id if current_user else None,
        username=current_user.username if current_user else ("Host" if is_host else "Guest"),
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
async def get_inbox(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return {"invites": [], "reset_requests": []}
    invites = session.exec(select(WatchInvite).where(WatchInvite.invited_user_id == current_user.id).where(WatchInvite.status == "pending")).all()
    results = []
    now = datetime.now(timezone.utc)
    for inv in invites:
        sender = session.get(User, inv.sender_id)
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

        results.append({
            "id": inv.id,
            "sender": sender.username,
            "media_type": ws_session.media_type,
            "title": title
        })

    reset_requests = []
    if current_user.is_admin:
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

from pydantic import BaseModel
class WatchTogetherRequest(BaseModel):
    media_type: str
    item_id: int
    invited_user_ids: list[int]

@app.post("/watch_together/create")
async def create_watch_together(req: WatchTogetherRequest, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.watch_together_access:
        raise HTTPException(status_code=403, detail="Forbidden")

    ws_session = WatchSession(host_id=current_user.id, media_type=req.media_type, item_id=req.item_id, is_active=True)
    session.add(ws_session)
    session.commit()

    for uid in req.invited_user_ids:
        invite = WatchInvite(session_id=ws_session.id, sender_id=current_user.id, invited_user_id=uid)
        session.add(invite)
    session.commit()

    return {"session_id": ws_session.id}

@app.post("/watch_together/accept/{invite_id}")
async def accept_watch_together(invite_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=403, detail="Forbidden")
    invite = session.get(WatchInvite, invite_id)
    if invite and invite.invited_user_id == current_user.id:
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
async def decline_watch_together(invite_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=403, detail="Forbidden")
    invite = session.get(WatchInvite, invite_id)
    if invite and invite.invited_user_id == current_user.id:
        invite.status = "declined"
        session.commit()
        return {"success": True}
    return {"success": False}

@app.post("/watch_together/end/{session_id}")
async def end_watch_together(session_id: str, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=403, detail="Forbidden")
    ws_session = session.get(WatchSession, session_id)
    if ws_session and ws_session.host_id == current_user.id:
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
async def get_eligible_users(media_type: str, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.watch_together_access: return []
    # Get users with access to media_type and watch_together_access
    query = select(User).where(User.id != current_user.id).where(User.watch_together_access == True)
    users = session.exec(query).all()
    
    eligible = []
    for u in users:
        if media_type == "movie" and u.movies_access:
            eligible.append({"id": u.id, "username": u.username})
        elif media_type == "tv" and u.tv_access:
            eligible.append({"id": u.id, "username": u.username})
            
    return eligible
