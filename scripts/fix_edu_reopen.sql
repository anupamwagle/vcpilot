-- =============================================================================
-- Fix: Re-open EDU.AX position incorrectly closed by check_exit_rules_task bug
-- =============================================================================
-- Run on the server:
--   docker exec -i <db-container> psql -U vcpilot -d vcpilot < fix_edu_reopen.sql
-- Or via psql directly if the DB port is forwarded:
--   psql -h 127.0.0.1 -p 5439 -U vcpilot -d vcpilot -f fix_edu_reopen.sql
-- =============================================================================

BEGIN;

-- 1. Show current state before touching anything
SELECT
    id, ticker, status, entry_price, current_stop, qty,
    updated_at AT TIME ZONE 'Australia/Sydney' AS updated_at_sydney
FROM positions
WHERE ticker = 'EDU.AX'
ORDER BY updated_at DESC
LIMIT 5;

-- 2. Re-open the most recently closed EDU.AX position
--    (the one incorrectly closed at ~14:35 AEST today by check_exit_rules_task)
UPDATE positions
SET
    status     = 'open',
    updated_at = NOW()
WHERE
    ticker     = 'EDU.AX'
    AND status = 'closed'
    -- Safety: only re-open if it was closed within the last 60 minutes
    AND updated_at >= NOW() - INTERVAL '60 minutes'
RETURNING id, ticker, status, updated_at;

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
    E'⚠️ Manual correction: EDU.AX position re-opened — was incorrectly closed at 14:35 '
    E'by check_exit_rules_task bug (STOP_LOSS signal fired in parallel with a live IBKR '
    E'bracket stop). Position re-opened; Trade record left for sync_order_status reconciliation.',
    '{"source": "manual_fix", "bug": "check_exit_rules_stop_loss_equity_race"}'::jsonb,
    NOW()
FROM positions
WHERE ticker = 'EDU.AX'
LIMIT 1;

COMMIT;
