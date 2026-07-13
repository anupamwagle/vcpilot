"""
AstraTrade — Database Setup
SQLAlchemy engine, session factory, and Base for all models.
"""
import os
from contextlib import contextmanager
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from loguru import logger

from app.config import settings


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    # Opt-in only — SQL echo prints every statement including system_configs
    # values (IBKR/Telegram/crypto secrets), so it must never be tied to
    # app_env (whose default is "development").
    echo=os.getenv("SQL_ECHO", "").lower() in ("1", "true", "yes"),
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@contextmanager
def get_db():
    """Context manager for database sessions."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_timescaledb():
    """Verify TimescaleDB extension is available."""
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'timescaledb'")
        )
        if result.fetchone():
            logger.info("TimescaleDB extension confirmed.")
        else:
            logger.warning("TimescaleDB extension NOT found. Using plain PostgreSQL.")


def init_db():
    """Create all tables. Called on startup."""
    from app.models import all_models  # noqa: F401 — import to register models
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created/verified.")
