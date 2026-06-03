"""Migration: add SCREENER_TICKER to auditaction enum."""
from sqlalchemy import text
from app.database import engine

with engine.connect() as conn:
    conn.execute(text("COMMIT"))  # DDL needs auto-commit
    try:
        conn.execute(text("ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'SCREENER_TICKER'"))
        print("OK: SCREENER_TICKER added to auditaction enum")
    except Exception as e:
        print(f"Note: {e}")
    conn.execute(text("COMMIT"))
