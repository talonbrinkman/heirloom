from sqlmodel import SQLModel, create_engine, Session, select, text
from .models import User, VolumePath  # We import models so SQLModel knows the tables exist

# This is critical: We are saving the database inside the /config folder.
# Because of your Docker volume mount, this will actually be saved to your desktop!
from sqlalchemy import event

sqlite_file_name = "/config/heirloom.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

# The engine connects to the file with a timeout to wait for locks
engine = create_engine(sqlite_url, echo=True, connect_args={"timeout": 15})

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
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

        # Migration to fix malformed TV Show titles (e.g. 'Better Call Saul S03 01' or 'Invincible (2021)')
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

def get_session():
    with Session(engine) as session:
        yield session
