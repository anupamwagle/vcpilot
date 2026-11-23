"""Explicit, one-shot fresh setup. Refuses existing business/user data.

Run with no web/workers and ASTRA_FRESH_BOOTSTRAP=confirmed on a NEW database.
Never use this to reset an existing deployment or repaired legacy database.
"""
import os
import secrets

from sqlalchemy import inspect, text

from app.database import engine


def main():
    if os.getenv("ASTRA_FRESH_BOOTSTRAP") != "confirmed":
        raise RuntimeError("Explicit fresh bootstrap confirmation required")
    with engine.connect() as conn:
        if inspect(conn).get_table_names(schema="public"):
            raise RuntimeError("Fresh bootstrap requires an empty public schema")

    # The existing migrations are retained; all are run before ingress starts.
    from scripts.init_db import main as initialise
    from scripts.migrate_saas import migrate
    initialise()
    migrate()

    with engine.begin() as conn:
        for table in ("positions", "trades", "orders", "signals", "watchlist"):
            if conn.execute(text(f"SELECT count(*) FROM {table}")).scalar():
                raise RuntimeError(f"Unexpected business rows in {table}")
        # Disable the legacy migration's publicly known bootstrap account.
        # No default password remains usable, even if activation is later changed.
        from app.models.auth import hash_password
        conn.execute(text("UPDATE users SET is_active=false, password_hash=:password"),
                     {"password": hash_password(secrets.token_urlsafe(48))})
        conn.execute(text("UPDATE organizations SET name='AstraTrade' WHERE name='Default Org'"))
        conn.execute(text("UPDATE accounts SET name='Unconfigured — no funding', capital_aud=0, is_active=false, is_paper=true, ibkr_account_id=NULL"))
        flags = {
            "trading_paused": "true", "trading_kill_switch": "true",
            "telegram_enabled": "false", "working_capital_aud": "0",
            "ibkr_simulate": "false", "mock_time_enabled": "false",
            "mcp_base_url": "https://trade.astradigital.com.au",
        }
        for key, value in flags.items():
            conn.execute(text("UPDATE system_configs SET value=:value WHERE key=:key"), {"key": key, "value": value})
        # Imported market data will be loaded separately, without old users,
        # accounts, strategy overrides, signals, balances or provider secrets.
        assert conn.execute(text("SELECT count(*) FROM users WHERE is_active")).scalar() == 0
    print("Fresh bootstrap complete; default login disabled; no funding or trading enabled.")


if __name__ == "__main__":
    main()
