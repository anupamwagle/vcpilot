# IBKR paper and MCP acceptance — 24 November 2026

User requested IBKR algorithmic execution and Codex MCP, with paper account first.
Live orders and an unattended live trader remain disabled.

## Implemented

- Default paper-only deployment; configured managed-account and mode verification;
  one API port and stable client ID, without live-port/random-client fallback.
- Disconnection errors instead of synthetic fills. Simulation requires explicit
  IBKR_SIMULATE plus caller opt-in. Paper broker exits await execution reports.
- Shared signal execution rejects unconfigured capital/accounts, nonfinite prices,
  stale-price fallbacks, pause, kill switch and daily-loss halt.
- Automated and MCP entries atomically claim a signal and persist intent before
  broker I/O. Exit intents also persist before submission. Ambiguous outcomes
  remain blocked for reconciliation instead of automatically resubmitting.
- Account/client-scoped cancellation requires acknowledgement; global cancel is
  disabled. Market sells wait for cancellation confirmation.
- Missing holdings do not invent sales/P&L. Partial sells retain remaining quantity
  and deduplicate execution IDs. The unique Trade row aggregates realised exits.
- Redis lock failure blocks task execution. Unit fixtures explicitly provide lock
  availability; a boundary regression tests backend failure.
- MCP validates issuer/audience/claims, active credential/client/organisation and
  current scopes. Credential verification failure rejects requests. Recovery
  removes all write scopes, including from previously issued tokens.
- Stateless Streamable HTTP at `/mcp/` replaces legacy SSE. The mounted transport
  lifecycle starts explicitly. A stdio bridge obtains short-lived tokens using a
  private file; no secret in Codex configuration or process arguments.
- The dashboard no longer invents 1,000 AUD for an unconfigured account.

## Runtime and connection

Production keeps the recovery stack: read/audit-only DB role, no workers/gateway,
internal app network, live trading disabled. `ASTRA_RECOVERY_MCP_READ_ONLY=true`
permits only the authenticated MCP endpoint and token endpoint in addition to the
existing review routes. No broker configuration is copied into production.

`scripts/prepare_mcp_readonly_credential.py` creates private provisioning SQL and
bridge config. Apply SQL through the DB owner; the credential has read scopes
only and expires after 90 days. `scripts/check_mcp_connection.py` tests real OAuth,
initialization, discovery, read and write denial. Codex runs WSL Python with
`scripts/mcp_stdio_bridge.py --config <private file>`. Transport failure never
causes an automatic order retry.

## Outstanding acceptance

Local gateway is pinned to
`ghcr.io/gnzsnz/ib-gateway@sha256:4f2704a1ab2ebcd468ac6ca58e2b99d2c6c056b303a3f499b67fc80012ad5a10`,
with paper mode, read-only API, loopback exposure and no automatic restart.
The first login rejected saved credentials. The user supplied a password update;
the subsequent API handshake still failed. The saved account is not a paper
identifier. Updated paper username/account requested. Gateways stopped; no actual
broker paper or live orders sent.

Before enabling execution: verify paper login/account and authentication, current
market data, bounded trial allocation/limits, shared-host capacity, actual
submit/cancel/fill/protective exit/restart reconciliation. Legacy pyramid and
remaining task paths need further concurrency/risk review before unattended use.
Ambiguous intents require operator reconciliation. Historical recovered August
bars and synthetic tests are not current quotes, broker fills or profitability
evidence. No claim of complete unattended trading acceptance.

References checked: [gateway maintainer](https://github.com/gnzsnz/ib-gateway-docker),
[IBKR orders](https://www.interactivebrokers.com/docs/tws-api/doc/orders/place-order),
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
