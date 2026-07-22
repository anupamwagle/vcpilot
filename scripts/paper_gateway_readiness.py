"""Read-only IBKR paper-gateway readiness probe.

Run inside the ``web`` or ``worker-equities`` container before a paper
acceptance drill:

    python -m scripts.paper_gateway_readiness --organization-id 1

It deliberately does not submit, cancel, or modify orders.  A successful probe
proves that the organisation resolves its own configured IBKR account, the
gateway is reachable, and the account detected by IBKR is a DU/DF paper account.
The subsequent acceptance drill must verify entry fill, partial fill, stop,
manual exit, reconnect, and reconciliation using the normal UI/MCP paths.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.broker.ibkr import IBKRBroker


def probe(organization_id: int | None = None) -> tuple[bool, dict]:
    """Return a JSON-serialisable, read-only gateway readiness result."""
    with IBKRBroker(organization_id=organization_id) as broker:
        if not broker.is_connected:
            return False, {
                "ready": False,
                "reason": broker.last_error or "IBKR gateway is not connected",
                "organization_id": organization_id,
            }
        detected_paper = broker.detected_paper_mode
        account = broker.account or ""
        result = {
            "ready": bool(detected_paper is True),
            "organization_id": organization_id,
            "account": account,
            "detected_paper_mode": detected_paper,
            "open_order_count": len(broker.get_open_orders()),
            "position_count": len(broker.get_open_positions()),
            "host": broker.host,
            "port": broker.port,
        }
        if detected_paper is not True:
            result["reason"] = "Gateway account is not a detected DU/DF paper account; paper drill refused"
        return bool(result["ready"]), result


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only AstraTrade IBKR paper readiness probe")
    parser.add_argument("--organization-id", type=int, default=None)
    args = parser.parse_args()
    ready, result = probe(args.organization_id)
    print(json.dumps(result, indent=2, default=str))
    return 0 if ready else 2


if __name__ == "__main__":
    sys.exit(main())
