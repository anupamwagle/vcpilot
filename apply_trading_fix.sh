#!/bin/bash
# Applies the check_entry_triggers fix and resets stuck TRX signal
# Run from WSL: bash /mnt/c/vcpilot/apply_trading_fix.sh

set -e

echo "=== Step 1: Reset stuck TRX-AUD signal to PENDING ==="
docker exec vcpilot-database psql -U vcpilot -d vcpilot -c "
  UPDATE signals
  SET status = 'PENDING'
  WHERE ticker = 'TRX-AUD'
    AND status = 'TRIGGERED'
    AND organization_id = 10
  RETURNING id, ticker, status, signal_date;
"

echo ""
echo "=== Step 2: Restart worker-crypto and api to pick up code changes ==="
docker compose restart worker-crypto api

echo ""
echo "=== Done. TRX-AUD will be retried on the next 5-min entry check cycle. ==="
echo "Watch for it: docker logs -f vcpilot-worker-crypto"
