"""
VCPilot — Database Initialisation
Creates all tables, sets up TimescaleDB hypertables, and seeds default config.
Run once on first startup: python -m scripts.init_db
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
from app.database import init_db, engine, check_timescaledb
from sqlalchemy import text


def setup_hypertables():
    """Call the TimescaleDB setup function defined in 001_init.sql."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT setup_timescaledb_hypertables()"))
            conn.commit()
        logger.info("TimescaleDB hypertables configured.")
    except Exception as e:
        logger.warning(f"Hypertable setup skipped (may not be TimescaleDB): {e}")


def main():
    logger.info("Initialising VCPilot database...")

    # 1. Create all SQLAlchemy tables
    init_db()

    # 2. Check TimescaleDB
    check_timescaledb()

    # 3. Set up hypertables
    setup_hypertables()

    # 4. Seed default configuration
    from scripts.seed_config import seed_all
    seed_all()

    logger.info("Database initialisation complete.")


if __name__ == "__main__":
    main()
