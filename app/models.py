from sqlmodel import SQLModel, Field
from typing import Optional
import uuid
from datetime import datetime, timedelta

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    is_admin: bool = Field(default=False)
    movies_access: bool = Field(default=True)
    tv_access: bool = Field(default=True)
    photos_access: bool = Field(default=True)
    downloads_access: bool = Field(default=True)
    watch_together_access: bool = Field(default=True)
    kids_mode: bool = Field(default=False)
    session_token: Optional[str] = Field(default=None, index=True)

class Movie(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    file_path: str = Field(unique=True)
    year: Optional[str] = None
    plot: Optional[str] = None
    poster_filename: Optional[str] = None
    backdrop_filename: Optional[str] = None
    cast: Optional[str] = None
    director: Optional[str] = None
    rating: Optional[float] = None
    genres: Optional[str] = None
    runtime: Optional[int] = None
    content_rating: Optional[str] = None

class TVShow(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    file_path: str = Field(unique=True)
    year: Optional[str] = None
    plot: Optional[str] = None
    poster_filename: Optional[str] = None
    backdrop_filename: Optional[str] = None
    cast: Optional[str] = None
    director: Optional[str] = None
    rating: Optional[float] = None
    genres: Optional[str] = None
    runtime: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    season_poster_filename: Optional[str] = None
    content_rating: Optional[str] = None

class Photo(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    file_path: str = Field(unique=True)
    date_added: datetime = Field(default_factory=datetime.utcnow)
    album: Optional[str] = None
    poster_filename: Optional[str] = None

import random
import string

def generate_short_code():
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=9))

class InviteCode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(default_factory=generate_short_code, unique=True, index=True)
    used: bool = Field(default=False)
    expires_at: datetime = Field(default_factory=lambda: datetime.utcnow() + timedelta(days=7))

class Watchlist(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    media_type: str 
    item_id: int

class WatchHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    media_type: str 
    item_id: int
    progress: float = Field(default=0.0) # Percentage from 0 to 100
    last_watched: datetime = Field(default_factory=datetime.utcnow)

class VolumePath(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    path: str = Field(unique=True, index=True)
    media_type: str # "movie" or "tv"

class SystemConfig(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str

class WatchSession(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    host_id: int = Field(foreign_key="user.id")
    media_type: str
    item_id: int
    created_at: datetime = Field(default_factory=datetime.utcnow)

class WatchInvite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="watchsession.id")
    sender_id: int = Field(foreign_key="user.id")
    invited_user_id: int = Field(foreign_key="user.id")
    status: str = Field(default="pending")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class PasswordResetRequest(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    status: str = Field(default="pending")  # pending, approved, denied, used
    token: Optional[str] = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)
