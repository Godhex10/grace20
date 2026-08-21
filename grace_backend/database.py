import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Load .env HERE, before reading DATABASE_URL — this module is imported first
# (main.py line 1-ish), before router_pipeline's own load_dotenv() runs, so
# without this the .env DATABASE_URL would be missed and we'd fall back to SQLite.
load_dotenv()

# Cloud Postgres (e.g. Supabase) when DATABASE_URL is set; fast local SQLite
# otherwise. This lets local dev stay instant on SQLite while the hosted backend
# points at Supabase — same code, just an env var.
_raw = os.environ.get("DATABASE_URL", "").strip()

if _raw:
    # Normalize to the psycopg (v3) driver we ship with.
    if _raw.startswith("postgresql://"):
        _raw = "postgresql+psycopg://" + _raw[len("postgresql://"):]
    elif _raw.startswith("postgres://"):
        _raw = "postgresql+psycopg://" + _raw[len("postgres://"):]
    DATABASE_URL = _raw
    engine = create_engine(
        DATABASE_URL,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,     # drop dead connections (pooler may recycle them)
        pool_recycle=300,       # recycle before Supabase's pooler idle timeout
    )
else:
    DATABASE_URL = "sqlite:///./grace_core.db"
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
