from sqlmodel import SQLModel, create_engine, Session, select, text
from .models import User, VolumePath, Profile, WatchHistory, Watchlist, Artist, Album, Track, Playlist, PlaylistTrack

# This is critical: We are saving the database inside the /config folder.
# Because of your Docker volume mount, this will actually be saved to your desktop!
from sqlalchemy import event
from sqlalchemy.pool import NullPool

sqlite_file_name = "/config/heirloom.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

# Use NullPool for SQLite to prevent QueuePool exhaustion during concurrent streams and downloads.
# In WAL mode, SQLite handles concurrent read operations without pool contention.
engine = create_engine(
    sqlite_url,
    echo=False,
    connect_args={"timeout": 60, "check_same_thread": False},
    poolclass=NullPool
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    try:
        cursor.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    try:
        cursor.execute("PRAGMA busy_timeout=60000")
    except Exception:
        pass
    cursor.close()

def create_db_and_tables():
    # This checks the file and creates the tables if they don't exist yet
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        if not session.exec(select(VolumePath)).first():
            session.add(VolumePath(path="/media/movies", media_type="movie"))
            session.add(VolumePath(path="/media/tv", media_type="tv"))
            session.commit()
            
        # Migration to add new columns for ratings and TV show seasons seamlessly
        try:
            session.exec(text("ALTER TABLE movie ADD COLUMN rating FLOAT"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE tvshow ADD COLUMN rating FLOAT"))
            session.exec(text("ALTER TABLE tvshow ADD COLUMN season INTEGER"))
            session.exec(text("ALTER TABLE tvshow ADD COLUMN episode INTEGER"))
            session.exec(text("ALTER TABLE tvshow ADD COLUMN season_poster_filename VARCHAR"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE movie ADD COLUMN runtime INTEGER"))
            session.exec(text("ALTER TABLE tvshow ADD COLUMN runtime INTEGER"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE movie ADD COLUMN content_rating VARCHAR"))
            session.exec(text("ALTER TABLE tvshow ADD COLUMN content_rating VARCHAR"))
            session.commit()
        except Exception:
            session.rollback()
            
        try:
            session.exec(text("ALTER TABLE user ADD COLUMN kids_mode BOOLEAN"))
            session.exec(text("UPDATE user SET kids_mode = 0"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE user ADD COLUMN rewind_seconds INTEGER DEFAULT 0"))
            session.exec(text("UPDATE user SET rewind_seconds = 0 WHERE rewind_seconds IS NULL"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE movie ADD COLUMN genres VARCHAR"))
            session.exec(text("ALTER TABLE tvshow ADD COLUMN genres VARCHAR"))
            session.commit()
        except Exception:
            session.rollback()

        # Marker column migrations for Skip Intro and Skip Credits
        marker_cols = [
            ("movie", "intro_start", "FLOAT"),
            ("movie", "intro_end", "FLOAT"),
            ("movie", "credits_start", "FLOAT"),
            ("movie", "credits_end", "FLOAT"),
            ("tvshow", "intro_start", "FLOAT"),
            ("tvshow", "intro_end", "FLOAT"),
            ("tvshow", "credits_start", "FLOAT"),
            ("tvshow", "credits_end", "FLOAT"),
        ]
        for tbl, col, dt in marker_cols:
            try:
                session.exec(text(f"ALTER TABLE {tbl} ADD COLUMN {col} {dt}"))
                session.commit()
            except Exception:
                session.rollback()

        # Migrations for Movie trailer_key and tmdb_id
        for col, dt in [("trailer_key", "VARCHAR"), ("tmdb_id", "INTEGER")]:
            try:
                session.exec(text(f"ALTER TABLE movie ADD COLUMN {col} {dt}"))
                session.commit()
            except Exception:
                session.rollback()

        # Migrations for WatchHistory current_time and duration
        for col in ["current_time", "duration"]:
            try:
                session.exec(text(f"ALTER TABLE watchhistory ADD COLUMN {col} FLOAT DEFAULT 0.0"))
                session.commit()
            except Exception:
                session.rollback()

        # Migration for Photo date_taken
        try:
            session.exec(text("ALTER TABLE photo ADD COLUMN date_taken DATETIME"))
            session.commit()
        except Exception:
            session.rollback()

        # Migration for User and InviteCode music_access
        try:
            session.exec(text("ALTER TABLE user ADD COLUMN music_access BOOLEAN DEFAULT 0"))
            session.exec(text("UPDATE user SET music_access = 0 WHERE music_access IS NULL"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE invitecode ADD COLUMN music_access BOOLEAN DEFAULT 0"))
            session.exec(text("UPDATE invitecode SET music_access = 0 WHERE music_access IS NULL"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("UPDATE user SET music_access = 1 WHERE is_admin = 1 AND (music_access IS NULL OR music_access = 0)"))
            session.commit()
        except Exception:
            session.rollback()

        # Migrations for Music Tables (ensures existing databases get all columns)
        music_table_cols = [
            ("album", [
                ("title", "VARCHAR"),
                ("artist_id", "INTEGER"),
                ("artist_name", "VARCHAR"),
                ("year", "VARCHAR"),
                ("genre", "VARCHAR"),
                ("cover_filename", "VARCHAR"),
                ("theaudiodb_id", "INTEGER"),
            ]),
            ("artist", [
                ("name", "VARCHAR"),
                ("bio", "TEXT"),
                ("thumbnail_filename", "VARCHAR"),
                ("banner_filename", "VARCHAR"),
                ("theaudiodb_id", "INTEGER"),
            ]),
            ("track", [
                ("title", "VARCHAR"),
                ("file_path", "VARCHAR"),
                ("artist_id", "INTEGER"),
                ("artist_name", "VARCHAR"),
                ("album_id", "INTEGER"),
                ("album_title", "VARCHAR"),
                ("track_number", "INTEGER DEFAULT 1"),
                ("disc_number", "INTEGER DEFAULT 1"),
                ("duration", "FLOAT DEFAULT 0.0"),
                ("bitrate", "INTEGER"),
                ("codec", "VARCHAR"),
                ("year", "VARCHAR"),
                ("genre", "VARCHAR"),
                ("cover_filename", "VARCHAR"),
            ]),
            ("playlist", [
                ("name", "VARCHAR"),
                ("user_id", "INTEGER"),
                ("profile_id", "INTEGER"),
                ("created_at", "DATETIME"),
            ]),
            ("playlisttrack", [
                ("playlist_id", "INTEGER"),
                ("track_id", "INTEGER"),
                ("track_order", "INTEGER DEFAULT 0"),
            ]),
        ]
        for tbl, cols in music_table_cols:
            try:
                res = session.exec(text(f"PRAGMA table_info({tbl})")).all()
                existing_cols = {row[1] for row in res}
                if existing_cols:
                    for col_name, col_def in cols:
                        if col_name not in existing_cols:
                            try:
                                session.exec(text(f"ALTER TABLE {tbl} ADD COLUMN {col_name} {col_def}"))
                                session.commit()
                            except Exception:
                                session.rollback()
            except Exception:
                session.rollback()

        # Migration to fix malformed TV Show titles (e.g. 'Better Call Saul S03 01' or 'Invincible (2021)')
        try:
            from .models import TVShow
            import re
            shows = session.exec(select(TVShow)).all()
            for show in shows:
                new_title = show.title
                title_match = re.search(r'^(.*?)\s*[-_]?\s*(?:[Ss]\d+\s*[-_]?\s*[Ee]?\d+|\d+x\d+)', new_title, re.IGNORECASE)
                if title_match:
                    new_title = title_match.group(1).strip()
                
                # Strip trailing year in parentheses, e.g. " (2021)"
                new_title = re.sub(r'\s*\(\d{4}\)$', '', new_title).strip()
                
                if show.title != new_title:
                    show.title = new_title
                    session.add(show)
            session.commit()
        except Exception:
            session.rollback()

        # Multi-Profile Migrations
        try:
            session.exec(text("ALTER TABLE user ADD COLUMN can_manage_profiles BOOLEAN DEFAULT 1"))
            session.exec(text("UPDATE user SET can_manage_profiles = 1 WHERE can_manage_profiles IS NULL"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE profile ADD COLUMN pin VARCHAR"))
            session.commit()
        except Exception:
            session.rollback()

        # InviteCode permissions migration
        for col, dt, default in [
            ("movies_access", "BOOLEAN", 0),
            ("tv_access", "BOOLEAN", 0),
            ("photos_access", "BOOLEAN", 0),
            ("downloads_access", "BOOLEAN", 0),
            ("watch_together_access", "BOOLEAN", 0),
            ("kids_mode", "BOOLEAN", 0),
            ("can_manage_profiles", "BOOLEAN", 1),
        ]:
            try:
                session.exec(text(f"ALTER TABLE invitecode ADD COLUMN {col} {dt} DEFAULT {default}"))
                session.commit()
            except Exception:
                session.rollback()

        try:
            session.exec(text("ALTER TABLE watchhistory ADD COLUMN profile_id INTEGER"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE watchlist ADD COLUMN profile_id INTEGER"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE watchsession ADD COLUMN host_profile_id INTEGER"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE watchinvite ADD COLUMN sender_profile_id INTEGER"))
            session.commit()
        except Exception:
            session.rollback()

        try:
            session.exec(text("ALTER TABLE watchinvite ADD COLUMN invited_profile_id INTEGER"))
            session.commit()
        except Exception:
            session.rollback()

        # Seed Primary Profiles for existing Users if missing
        try:
            users = session.exec(select(User)).all()
            for u in users:
                existing_profile = session.exec(select(Profile).where(Profile.user_id == u.id, Profile.is_primary == True)).first()
                if not existing_profile:
                    primary_p = Profile(
                        user_id=u.id,
                        name=u.username,
                        avatar_color="#e50914",
                        avatar_icon="user",
                        is_primary=True,
                        is_kids=u.kids_mode,
                        rewind_seconds=u.rewind_seconds or 0
                    )
                    session.add(primary_p)
                    session.commit()
                    session.refresh(primary_p)
                    existing_profile = primary_p

                # Backfill historical watchhistory and watchlist
                if existing_profile:
                    session.exec(text(f"UPDATE watchhistory SET profile_id = {existing_profile.id} WHERE user_id = {u.id} AND profile_id IS NULL"))
                    session.exec(text(f"UPDATE watchlist SET profile_id = {existing_profile.id} WHERE user_id = {u.id} AND profile_id IS NULL"))
                    session.commit()
        except Exception:
            session.rollback()

def get_session():
    with Session(engine) as session:
        yield session
