from sqlmodel import SQLModel, Field
from typing import Optional
import uuid
from datetime import datetime, timedelta, timezone

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    is_admin: bool = Field(default=False)
    movies_access: bool = Field(default=False)
    tv_access: bool = Field(default=False)
    photos_access: bool = Field(default=False)
    music_access: bool = Field(default=False)
    downloads_access: bool = Field(default=False)
    watch_together_access: bool = Field(default=False)
    kids_mode: bool = Field(default=False)
    rewind_seconds: int = Field(default=0)
    can_manage_profiles: bool = Field(default=True)
    session_token: Optional[str] = Field(default=None, index=True)

class Profile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    name: str = Field(index=True)
    avatar_color: str = Field(default="#e50914")
    avatar_icon: Optional[str] = Field(default="user")
    is_primary: bool = Field(default=False)
    is_kids: bool = Field(default=False)
    rating_level: int = Field(default=0)  # 0: Adult (Unrestricted), 1: Teen (<=PG-13/TV-14), 2: Child (<=PG/TV-PG)
    pin: Optional[str] = Field(default=None)
    pin_hash: Optional[str] = Field(default=None)
    rewind_seconds: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
    intro_start: Optional[float] = None
    intro_end: Optional[float] = None
    credits_start: Optional[float] = None
    credits_end: Optional[float] = None
    trailer_key: Optional[str] = None
    tmdb_id: Optional[int] = None
    collection_id: Optional[int] = None
    collection_name: Optional[str] = None
    collection_poster_filename: Optional[str] = None
    collection_backdrop_filename: Optional[str] = None
    logo_filename: Optional[str] = None

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
    intro_start: Optional[float] = None
    intro_end: Optional[float] = None
    credits_start: Optional[float] = None
    credits_end: Optional[float] = None
    collection_name: Optional[str] = None
    logo_filename: Optional[str] = None

class Photo(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    file_path: str = Field(unique=True)
    date_added: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    date_taken: Optional[datetime] = None
    album: Optional[str] = None
    poster_filename: Optional[str] = None

class Artist(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    bio: Optional[str] = None
    thumbnail_filename: Optional[str] = None
    banner_filename: Optional[str] = None
    theaudiodb_id: Optional[int] = None

class Album(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    artist_id: Optional[int] = Field(default=None, foreign_key="artist.id", index=True)
    artist_name: str = Field(index=True)
    year: Optional[str] = None
    genre: Optional[str] = None
    cover_filename: Optional[str] = None
    theaudiodb_id: Optional[int] = None

class Track(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    file_path: str = Field(unique=True, index=True)
    artist_id: Optional[int] = Field(default=None, foreign_key="artist.id", index=True)
    artist_name: str = Field(index=True)
    album_id: Optional[int] = Field(default=None, foreign_key="album.id", index=True)
    album_title: Optional[str] = Field(default=None, index=True)
    track_number: Optional[int] = Field(default=1)
    disc_number: Optional[int] = Field(default=1)
    duration: Optional[float] = Field(default=0.0)
    bitrate: Optional[int] = None
    codec: Optional[str] = None
    format: str = Field(default="mp3")
    year: Optional[str] = None
    genre: Optional[str] = None
    cover_filename: Optional[str] = None
    date_added: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class Playlist(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    profile_id: Optional[int] = Field(default=None, foreign_key="profile.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PlaylistTrack(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    playlist_id: int = Field(foreign_key="playlist.id", index=True)
    track_id: int = Field(foreign_key="track.id", index=True)
    track_order: int = Field(default=0)

import random
import string

def generate_short_code():
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=9))

class InviteCode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(default_factory=generate_short_code, unique=True, index=True)
    used: bool = Field(default=False)
    expires_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(days=7))
    movies_access: bool = Field(default=False)
    tv_access: bool = Field(default=False)
    photos_access: bool = Field(default=False)
    music_access: bool = Field(default=False)
    downloads_access: bool = Field(default=False)
    watch_together_access: bool = Field(default=False)
    kids_mode: bool = Field(default=False)
    can_manage_profiles: bool = Field(default=True)

class Watchlist(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    profile_id: Optional[int] = Field(default=None, foreign_key="profile.id", index=True)
    media_type: str 
    item_id: int

class WatchHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    profile_id: Optional[int] = Field(default=None, foreign_key="profile.id", index=True)
    media_type: str 
    item_id: int
    progress: float = Field(default=0.0) # Percentage from 0 to 100
    current_time: Optional[float] = Field(default=0.0)
    duration: Optional[float] = Field(default=0.0)
    last_watched: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
    host_profile_id: Optional[int] = Field(default=None, foreign_key="profile.id")
    media_type: str
    item_id: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = Field(default=True)

class WatchInvite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="watchsession.id")
    sender_id: int = Field(foreign_key="user.id")
    sender_profile_id: Optional[int] = Field(default=None, foreign_key="profile.id")
    invited_user_id: int = Field(foreign_key="user.id")
    invited_profile_id: Optional[int] = Field(default=None, foreign_key="profile.id")
    status: str = Field(default="pending")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PasswordResetRequest(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    status: str = Field(default="pending")  # pending, approved, denied, used
    token: Optional[str] = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
