-- ============================================================================
-- VCPilot Migration 003 — Duplicate-Trade Prevention
-- Run via: python3 -m scripts.migrate_saas (called automatically on startup)
-- Safe to re-run: all statements use IF NOT EXISTS / DO NOTHING / conditional
--                 guards so re-applying is a no-op.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Add position_id FK column to trades
--    Links each closed Trade back to the originating Position for exact
--    deduplication. Nullable — rows from before this migration and broker-sync
--    rows that have no matching Position row will have NULL here.
-- ----------------------------------------------------------------------------
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS position_id INTEGER REFERENCES positions(id);

-- Index for FK lookups (position → trade history)
CREATE INDEX IF NOT EXISTS ix_trades_position_id ON trades (position_id);

-- ----------------------------------------------------------------------------
-- 2. Unique constraint on position_id (DB-level dedup guard T-DEDUP-4)
--    Prevents a second Trade being inserted for the same position_id.
--    NULL values are excluded from uniqueness checks in PostgreSQL, so
--    rows with position_id = NULL (legacy / broker-sync) are unaffected.
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_trades_position_id'
          AND conrelid = 'trades'::regclass
    ) THEN
        ALTER TABLE trades
            ADD CONSTRAINT uq_trades_position_id UNIQUE (position_id);
    END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- 3. Backfill position_id for existing Trade rows (best-effort)
--    Matches on (ticker, account_id, organization_id, entry_date) — the same
--    fields used to create the Trade at close time. Where multiple positions
--    match (re-entries on the same ticker/date) we pick the lowest id.
--    Rows that cannot be matched are left as NULL — they are still valid.
--
--    NOTE: run AFTER the cleanup script (scripts/cleanup_duplicate_trades.py)
--    has removed any existing duplicate Trade rows, otherwise the unique
--    constraint added in step 2 will block the backfill on duplicate groups.
-- ----------------------------------------------------------------------------
UPDATE trades t
SET    position_id = (
    SELECT p.id
    FROM   positions p
    WHERE  p.ticker          = t.ticker
      AND  p.account_id      = t.account_id
      AND  p.organization_id = t.organization_id
      AND  p.entry_date      = t.entry_date
    ORDER  BY p.id
    LIMIT  1
)
WHERE  t.position_id IS NULL;
