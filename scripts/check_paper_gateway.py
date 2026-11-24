"""Read-only paper gateway identity probe. Prints no account identifiers."""
import json
import logging
import sys
from dotenv import dotenv_values
from ib_insync import IB

logging.disable(logging.CRITICAL)
expected = (dotenv_values(sys.argv[1]).get("IBKR_ACCOUNT") or "").strip()
ib = IB()
try:
    ib.connect("127.0.0.1", 4004, clientId=91, readonly=True, timeout=25)
    accounts = ib.managedAccounts()
    paper = bool(accounts) and all(a.upper().startswith(("DU", "DF")) for a in accounts)
    print(json.dumps({"connected": ib.isConnected(), "all_managed_accounts_paper": paper,
                      "configured_account_matches": expected in accounts,
                      "managed_account_count": len(accounts),
                      "position_count": len(ib.positions()), "open_order_count": len(ib.openTrades())}))
except Exception as exc:
    print(json.dumps({"connected": False, "error_type": type(exc).__name__}))
    sys.exit(1)
finally:
    ib.disconnect()
