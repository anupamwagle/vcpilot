-- =============================================================================
-- Fix: Re-open EDU.AX position incorrectly closed by check_exit_rules_task bug
-- =============================================================================
-- Run on the server:
--   docker exec -i vcpilot-database psql -U vcpilot -d vcpilot < scripts/fix_edu_reopen.sql
-- =============================================================================

BEGIN;

-- 1. Show current state before touching anything
SELECT
    id, ticker, status, entry_price, current_stop, qty,
    last_updated AT TIME ZONE 'Australia/Sydney' AS last_updated_sydney
FROM positions
WHERE ticker = 'EDU.AX'
ORDER BY last_updated DESC
LIMIT 5;

-- 2. Re-open the most recently closed EDU.AX position
--    (incorrectly closed at ~14:35 AEST by check_exit_rules_task bug)
UPDATE positions
SET
    status       = 'OPEN',
    last_updated = NOW()
WHERE
    ticker       = 'EDU.AX'
    AND status   = 'CLOSED'
    -- Safety: only re-open if it was closed within the last 60 minutes
    AND last_updated >= NOW() - INTERVAL '60 minutes'
RETURNING id, ticker, status, last_updated;

-- 3. Audit log entry explaining the correction
INSERT INTO audit_logs (
    action,
    organization_id,
    ticker,
    message,
    detail,
    created_at
)
SELECT
    'TASK_RUN',
    organization_id,
    'EDU.AX',
    'Manual correction: EDU.AX position re-opened. Was incorrectly closed at 14:35 by check_exit_rules_task bug (STOP_LOSS signal fired in parallel with live IBKR bracket stop). Trade record left for sync_order_status reconciliation.',
    '{"source": "manual_fix", "bug": "check_exit_rules_stop_loss_equity_race"}'::jsonb,
    NOW()
FROM positions
WHERE ticker = 'EDU.AX'
LIMIT 1;

COMMIT;
