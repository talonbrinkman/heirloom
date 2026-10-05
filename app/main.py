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
from datetime import datetime
from .database import create_db_and_tables, get_session, engine
from .models import User, Movie, TVShow, InviteCode, Watchlist, WatchHistory, VolumePath, SystemConfig, Photo, WatchSession, WatchInvite, PasswordResetRequest
from .scanner import scan_media_library
from .watcher import start_watcher, stop_watcher
from .ffmpeg_setup import ensure_ffmpeg, get_ffmpeg_path, get_ffprobe_path
import subprocess

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

    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append(websocket)

    def disconnect(self, websocket: WebSocket, session_id: str):
        if session_id in self.active_connections:
            if websocket in self.active_connections[session_id]:
                self.active_connections[session_id].remove(websocket)
            if not self.active_connections[session_id]:
                del self.active_connections[session_id]

    async def broadcast(self, message: str, session_id: str, sender: WebSocket = None):
        if session_id in self.active_connections:
            for connection in self.active_connections[session_id]:
                if connection != sender:
                    try:
                        await connection.send_text(message)
                    except Exception:
                        pass

manager = ConnectionManager()

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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
    if ip not in failed_attempts:
        failed_attempts[ip] = {"count": 1, "last_attempt": now}
    else:
        failed_attempts[ip]["count"] += 1
        failed_attempts[ip]["last_attempt"] = now

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
async def process_reset_password(token: str, password: str = Form(...), session: Session = Depends(get_session)):
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
async def process_setup(username: str = Form(...), password: str = Form(...), session: Session = Depends(get_session)):
    if session.exec(select(User)).first(): 
        return RedirectResponse(url="/", status_code=303)
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
async def process_register(request: Request, username: str = Form(...), password: str = Form(...), invite_code: str = Form(...), session: Session = Depends(get_session)):
    check_rate_limit(request)
    invite = session.exec(select(InviteCode).where(InviteCode.code == invite_code)).first()
    if not invite or invite.used or invite.expires_at < datetime.utcnow():
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

    history_db = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).order_by(WatchHistory.last_watched.desc()).limit(15)).all()
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
        
    history_db = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).order_by(WatchHistory.last_watched.desc()).limit(15)).all()
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
        history_entry.last_watched = datetime.utcnow()
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
        
    duration = 0.0
    try:
        cmd = [get_ffprobe_path(), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", item.file_path]
        out = subprocess.check_output(cmd).decode().strip()
        duration = float(out)
    except:
        pass
        
    token = request.query_params.get("token", "")
    return templates.TemplateResponse(request=request, name="player.html", context={"item": item, "media_type": media_type, "history": history_entry, "season_episode": season_episode, "duration": duration, "next_episode_id": next_episode_id, "subtitle_url": subtitle_url, "sync_session_id": sync, "user": current_user, "token": token, "is_mobile": mobile == "1"})

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
async def update_progress(media_type: str, item_id: int, progress: float = Form(...), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return {"status": "unauthorized"}
    
    history_entry = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == item_id).where(WatchHistory.media_type == media_type)).first()
    if history_entry:
        history_entry.progress = progress
        history_entry.last_watched = datetime.utcnow()
        session.add(history_entry)
        session.commit()
    return {"status": "success"}

from fastapi.responses import StreamingResponse

@app.get("/stream/{media_type}/{item_id}")
async def stream_video(media_type: str, item_id: int, res: str = "original", start: float = 0.0, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    if media_type == "movie" and not current_user.movies_access: raise HTTPException(status_code=403, detail="Forbidden")
    if media_type == "tv" and not current_user.tv_access: raise HTTPException(status_code=403, detail="Forbidden")
    
    item = session.get(Movie if media_type == "movie" else TVShow, item_id)
    if not item or not os.path.exists(item.file_path): raise HTTPException(status_code=404, detail="Video file missing")
    
    if res == "original" and item.file_path.lower().endswith(".mp4") and start == 0.0:
        return FileResponse(item.file_path, media_type="video/mp4")
        
    cmd = [get_ffmpeg_path(), "-ss", str(start), "-i", item.file_path]
    if res == "original" and start == 0.0:
        cmd.extend(["-c:v", "copy"])
    elif res == "original":
        cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"])
    else:
        cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-vf", f"scale=-2:{res}"])
    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-af", "aresample=async=1", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov", "pipe:1"])

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
    if not current_user: return RedirectResponse(url="/login", status_code=303)
    history_entry = session.exec(select(WatchHistory).where(WatchHistory.user_id == current_user.id).where(WatchHistory.item_id == item_id).where(WatchHistory.media_type == media_type)).first()
    if history_entry:
        session.delete(history_entry)
        session.commit()
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
    
    return templates.TemplateResponse(request=request, name="settings.html", context={
        "user": current_user, "invites": invites, "users": users, "volume_paths": volume_paths, 
        "host_url": host_url, "remote_url": remote_url_config.value if remote_url_config else "",
        "tmdb_key": tmdb_key
    })

@app.post("/settings/change_password")
async def change_password(new_password: str = Form(...), current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: raise HTTPException(status_code=401, detail="Unauthorized")
    
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')
    current_user.password_hash = hashed
    
    session.add(current_user)
    session.commit()
    
    return RedirectResponse(url="/settings?pw_success=1", status_code=303)

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
    from .scanner import scan_media_library
    import threading
    threading.Thread(target=scan_media_library, args=(session,), daemon=True).start()
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
            scan_media_library(session)
            start_watcher()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/delete_volume/{volume_id}")
async def delete_volume(volume_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    vol = session.get(VolumePath, volume_id)
    if vol:
        session.delete(vol)
        session.commit()
        scan_media_library(session)
        start_watcher()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/rescan")
async def rescan_library(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=403, detail="Forbidden")
    scan_media_library(session)
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
    return {"status": "ok", "app": "heirloom"}
# --- Watch Together Routes ---
@app.websocket("/ws/sync/{session_id}")
async def websocket_sync(websocket: WebSocket, session_id: str):
    await manager.connect(websocket, session_id)
    try:
        while True:
            data = await websocket.receive_text()
            await manager.broadcast(data, session_id, sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket, session_id)

@app.get("/api/inbox")
async def get_inbox(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user: return {"invites": [], "reset_requests": []}
    # Get pending invites
    invites = session.exec(select(WatchInvite).where(WatchInvite.invited_user_id == current_user.id).where(WatchInvite.status == "pending")).all()
    results = []
    for inv in invites:
        sender = session.get(User, inv.sender_id)
        ws_session = session.get(WatchSession, inv.session_id)
        if ws_session and sender:
            # Get media title
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
    
    # Get pending password reset requests (admin only)
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
        
    ws_session = WatchSession(host_id=current_user.id, media_type=req.media_type, item_id=req.item_id)
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

@app.get("/play_sync/{session_id}", response_class=HTMLResponse)
async def play_sync(request: Request, session_id: str, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not current_user or not current_user.watch_together_access:
        return RedirectResponse(url="/", status_code=303)
        
    ws_session = session.get(WatchSession, session_id)
    if not ws_session:
        return RedirectResponse(url="/", status_code=303)
        
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
