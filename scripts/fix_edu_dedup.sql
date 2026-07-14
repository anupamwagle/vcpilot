-- =============================================================================
-- FULL CLEANUP: EDU.AX duplicate positions + duplicate submitted orders
-- =============================================================================
-- Run:
--   docker exec -i vcpilot-database psql -U vcpilot -d vcpilot < scripts/fix_edu_dedup.sql
--
-- IMPORTANT: After running this script you MUST also cancel the duplicate
-- EDU.AX SELL orders in IBKR directly (TWS or the IBKR Open Orders panel).
-- Keep ONLY the original bracket stop order. Cancel every other EDU SELL.
-- =============================================================================

BEGIN;

-- ── STEP 1: Diagnose ─────────────────────────────────────────────────────────
\echo '=== All EDU.AX positions (any status) ==='
SELECT id, status, qty, entry_price, current_stop, created_at AT TIME ZONE 'Australia/Sydney' AS created_sydney
FROM positions
WHERE ticker = 'EDU.AX'
ORDER BY created_at ASC;

\echo ''
\echo '=== All EDU.AX orders (SUBMITTED/PENDING) ==='
SELECT id, action, order_type, status, qty_ordered, ibkr_order_id, perm_id, submitted_at AT TIME ZONE 'Australia/Sydney' AS submitted_sydney
FROM orders
WHERE ticker LIKE '%EDU%' AND status IN ('SUBMITTED', 'PENDING')
ORDER BY submitted_at ASC;

-- ── STEP 2: Close all duplicate OPEN EDU.AX positions ────────────────────────
-- Keep only the OLDEST one (lowest id = original position).
\echo ''
\echo '=== Closing duplicate OPEN EDU.AX positions (keeping oldest) ==='
UPDATE positions
SET
    status       = 'CLOSED',
    last_updated = NOW()
WHERE
    ticker = 'EDU.AX'
    AND status = 'OPEN'
    AND id NOT IN (
        SELECT id FROM positions
        WHERE ticker = 'EDU.AX' AND status = 'OPEN'
        ORDER BY created_at ASC
        LIMIT 1
    )
RETURNING id, status, created_at;

-- ── STEP 3: Cancel duplicate SUBMITTED/PENDING SELL orders in our DB ─────────
-- Keep only the OLDEST submitted SELL (the original bracket stop child order).
-- The broker-side cancellation must be done manually in IBKR.
\echo ''
\echo '=== Marking duplicate EDU.AX SELL orders as CANCELLED in DB ==='
UPDATE orders
SET
    status       = 'CANCELLED',
    cancelled_at = NOW(),
    updated_at   = NOW()
WHERE
    ticker LIKE '%EDU%'
    AND action  = 'SELL'
    AND status IN ('SUBMITTED', 'PENDING')
    AND id NOT IN (
        SELECT id FROM orders
        WHERE ticker LIKE '%EDU%'
          AND action = 'SELL'
          AND status IN ('SUBMITTED', 'PENDING')
        ORDER BY submitted_at ASC NULLS LAST
        LIMIT 1
    )
RETURNING id, action, order_type, status, ibkr_order_id, perm_id;

-- ── STEP 4: Confirm final state ───────────────────────────────────────────────
\echo ''
\echo '=== FINAL: EDU.AX open positions ==='
SELECT id, status, qty, entry_price, current_stop, created_at AT TIME ZONE 'Australia/Sydney' AS created_sydney
FROM positions
WHERE ticker = 'EDU.AX' AND status = 'OPEN';

\echo ''
\echo '=== FINAL: EDU.AX active orders ==='
SELECT id, action, order_type, status, qty_ordered, ibkr_order_id, perm_id, submitted_at AT TIME ZONE 'Australia/Sydney' AS submitted_sydney
FROM orders
WHERE ticker LIKE '%EDU%' AND status IN ('SUBMITTED', 'PENDING')
ORDER BY submitted_at ASC;

-- ── STEP 5: Audit log ─────────────────────────────────────────────────────────
INSERT INTO audit_logs (action, organization_id, ticker, message, detail, created_at)
SELECT
    'TASK_RUN',
    organization_id,
    'EDU.AX',
    'Manual cleanup: closed duplicate OPEN positions and cancelled duplicate SUBMITTED SELL orders in DB. Root cause: check_exit_rules_task STOP_LOSS race with sync_stop_orders. Broker-side duplicate SELL orders must be cancelled manually in IBKR.',
    '{"source": "manual_fix", "bug": "check_exit_rules_stop_loss_equity_race", "action": "dedup_positions_and_orders"}'::jsonb,
    NOW()
FROM positions
WHERE ticker = 'EDU.AX'
LIMIT 1;

COMMIT;

\echo ''
\echo '=================================================================='
\echo 'DB cleanup done. NOW GO TO IBKR AND MANUALLY CANCEL ALL DUPLICATE'
\echo 'EDU.AX SELL ORDERS. Keep only the original bracket stop order.'
\echo '=================================================================='
