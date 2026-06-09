#!/bin/bash
# Resets stuck TRX-AUD TRIGGERED signal back to PENDING
# Run from WSL: bash /mnt/c/vcpilot/reset_trx_signal.sh

docker exec vcpilot-database-1 psql -U vcpilot -d vcpilot -c "
  UPDATE signals
  SET status = 'PENDING'
  WHERE ticker = 'TRX-AUD'
    AND status = 'TRIGGERED'
    AND organization_id = 10
  RETURNING id, ticker, status, signal_date;
"
