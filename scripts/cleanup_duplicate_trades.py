#!/usr/bin/env python3
"""
cleanup_duplicate_trades.py
===========================
Identifies and removes duplicate rows in the `trades` table that were created
by the race condition between check_exit_rules_task, sync_ibkr_positions_task,
and sync_order_status.

Strategy
--------
Duplicates are rows sharing the same:
    (ticker, account_id, organization_id, entry_date, exit_date)

For each duplicate group, the row with the LOWEST id is kept (first inserted).
All higher-id rows in the group are deleted.

Usage
-----
    # Dry run — prints report, makes no changes
    python scripts/cleanup_duplicate_trades.py

    # Execute — commits deletions after printing report
    python scripts/cleanup_duplicate_trades.py --execute
"""

import sys
import os

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import defaultdict
from sqlalchemy import text

# Bootstrap app config / DB
os.environ.setdefault("ENV", "production")

from app.database import SessionLocal
from app.models.trade import Trade


def find_duplicates(session):
    """
    Return a dict mapping (ticker, account_id, org_id, entry_date, exit_date)
    to the list of Trade rows in that group (sorted ascending by id).
    Only groups with >1 row are returned.
    """
    rows = (
        session.query(
            Trade.id,
            Trade.ticker,
            Trade.account_id,
            Trade.organization_id,
            Trade.entry_date,
            Trade.exit_date,
            Trade.exit_reason,
            Trade.net_pnl_aud,
        )
        .order_by(Trade.id)
        .all()
    )

    groups: dict = defaultdict(list)
    for row in rows:
        key = (row.ticker, row.account_id, row.organization_id, row.entry_date, row.exit_date)
        groups[key].append(row)

    return {k: v for k, v in groups.items() if len(v) > 1}


def main():
    execute = "--execute" in sys.argv

    print("=" * 70)
    print("AstraTrade — Duplicate Trade Cleanup Script")
    print("=" * 70)
    if not execute:
        print("MODE: DRY RUN (pass --execute to commit deletions)\n")
    else:
        print("MODE: *** EXECUTE — deletions will be committed ***\n")

    session = SessionLocal()
    try:
        duplicates = find_duplicates(session)

        if not duplicates:
            print("✅  No duplicate trade rows found. Nothing to do.")
            return

        total_to_delete = 0
        all_delete_ids: list[int] = []

        for (ticker, acct_id, org_id, entry_date, exit_date), rows in sorted(duplicates.items()):
            keep_id = rows[0].id
            delete_rows = rows[1:]
            delete_ids = [r.id for r in delete_rows]
            total_to_delete += len(delete_rows)
            all_delete_ids.extend(delete_ids)

            print(f"  {ticker}  entry={entry_date}  exit={exit_date}  acct={acct_id}  org={org_id}")
            print(f"    KEEP  --> id={keep_id}  reason={rows[0].exit_reason}  pnl={rows[0].net_pnl_aud}")
            for r in delete_rows:
                print(f"    DELETE--> id={r.id}  reason={r.exit_reason}  pnl={r.net_pnl_aud}")
            print()

        print(f"Summary: {len(duplicates)} duplicate group(s), {total_to_delete} row(s) to delete.")
        print()

        if not execute:
            print("-> Re-run with --execute to commit these deletions.")
            return

        # Confirm before deleting
        confirm = input(f"Type 'yes' to delete {total_to_delete} row(s): ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return

        # Delete in batches of 100
        deleted = 0
        for i in range(0, len(all_delete_ids), 100):
            batch = all_delete_ids[i:i + 100]
            session.execute(
                text("DELETE FROM trades WHERE id IN :ids"),
                {"ids": tuple(batch)},
            )
            deleted += len(batch)

        session.commit()
        print(f"\nDone. {deleted} duplicate trade row(s) deleted.")

        # Final verification
        remaining = find_duplicates(session)
        if remaining:
            print(f"\nWARNING: {len(remaining)} duplicate group(s) still exist after cleanup!")
            for key, rows in remaining.items():
                print(f"   {key}: ids={[r.id for r in rows]}")
        else:
            print("Verification passed -- no duplicate groups remain.")

    except Exception as e:
        session.rollback()
        print(f"\nError: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
