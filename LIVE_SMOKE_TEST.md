# AstraTrade — Live Order Smoke-Test Checklist

> **Run by you, not the agent.** These steps place real orders. Paper/IBKR-paper and
> crypto-simulation are no-money; the **LIVE** sections move real money. Do the paper
> tests first, in order, and only proceed to live once each paper step passes.
>
> Environment verified 2026-06-24: IBKR gateway `TRADING_MODE=paper` on port 4002,
> crypto `crypto_testnet=true` (Independent Reserve → local simulation), ASX regime
> BEAR / crypto CAUTION (so the autonomous screener will **not** self-trigger entries —
> good for controlled testing). Org = `1` (Default Org), account paper, A$5,000.

Host shortcuts used below:
```
D=/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker
# run app code inside the worker (has app/, ib_insync, ccxt):
RUN(){ $D exec -u root vcpilot-worker-equities sh -c "cd /app && python -c \"$1\""; }
```

---

## 0. Pre-flight (read-only, no orders)

- [ ] Gateway healthy: `$D ps | grep vcpilot-ibkr` shows `Up`.
- [ ] Confirm paper mode: `$D inspect vcpilot-ibkr --format '{{range .Config.Env}}{{println .}}{{end}}' | grep TRADING_MODE` → `paper`.
- [ ] Confirm app paper port: `$D exec vcpilot-api env | grep IBKR_PORT` → `4002`.
- [ ] Trading not paused: dashboard → Admin → Config → `trading_paused = false` (or set `true` while you test manually so the scheduler stays out of your way).
- [ ] Note current open positions (so you can tell your test order apart):
  dashboard → Positions, or via DB.

---

## 1. IBKR — PAPER bracket order  (no real money)

Because the running worker holds the gateway's API session, place the test order
**through the app's own broker** rather than a second raw connection. Easiest path:

### Option A — via the dashboard (recommended)
1. [ ] Dashboard → Watchlist → add a liquid ASX name you don't already hold (e.g. `CBA`).
2. [ ] Manually promote it to a signal (Watchlist Terminal → **▲ Promote to Signal**).
3. [ ] On the Signals page, confirm the pivot/stop/target look sane.
4. [ ] Trigger an entry check: Admin → Health → run the entry/exit cycle (or wait for the 5-min beat). Because regime is BEAR, equity entries are blocked by `regime_bear_block_equities` — **temporarily disable that rule** (Admin → Rules → Market Regime → toggle off) for the test, then re-enable it after.
5. [ ] Verify: a **paper** bracket order appears in IBKR (gateway VNC: https://vnc.astradigital.com.au/vnc_lite.html?password=changeme&autoconnect=true) and a Position row is created.
6. [ ] **Close it:** Positions → close (reason `MANUAL`). Confirm the Position → Trade transition and the audit log entry.
7. [ ] **Re-enable** `regime_bear_block_equities`.

### Option B — direct broker call (isolated, auto-cancels)
Places one far-from-market paper limit order through `IBKRBroker`, confirms the
ack, then cancels it. Does **not** create DB rows. Run on the host:
```
$D exec -u root vcpilot-worker-equities python - <<'PY'
from app.broker.ibkr import IBKRBroker
IBKRBroker._last_fail_times.clear()
b = IBKRBroker(organization_id=1)
# NOTE: stop the worker first OR use a free clientId the gateway trusts,
# otherwise the single-session gateway will time out (see Troubleshooting).
ok = b.connect()
print("connected:", ok)
if ok:
    r = b.submit_bracket_order(ticker="CBA", action="BUY", qty=1,
                               entry_price=1.00,         # far below market: will NOT fill
                               stop_price=0.90, target_price=1.20,
                               exchange_key="ASX", order_ref="smoke-test")
    print("submit:", r)
    # cancel everything we just placed
    import time; time.sleep(2)
    for o in b._ib.openOrders():
        b._ib.cancelOrder(o)
    print("cancelled open orders")
    b.disconnect()
PY
```
- [ ] Order acknowledged with an IBKR id, then cancelled. No fill, no position.

> **Single-session caveat:** the gateway accepts the worker's clientId. A second
> concurrent client may time out. For Option B either (a) briefly `… stop vcpilot-worker-equities`
> first, run the test, then `… start` it, or (b) set a clientId you've whitelisted in
> the gateway's API settings.

---

## 2. Crypto — SIMULATION order  (no real money, `crypto_testnet=true`)

With `crypto_testnet=true`, Independent Reserve has no sandbox so the broker runs
**local simulation** (this is the bug fixed on 2026-06-24 — it used to error).
```
$D exec -u root vcpilot-worker-crypto python - <<'PY'
from app.broker.crypto import get_crypto_broker_for_org
with get_crypto_broker_for_org(1) as cb:
    print("is_connected:", cb.is_connected, "(False = simulation, expected)")
    r = cb.submit_bracket_order(ticker="BTC-AUD", action="BUY", qty=0.0001,
                                entry_price=50000, stop_price=45000,
                                target_price=60000, order_ref="smoke-sim")
    print("simulated order:", r)   # expect status 'simulated'
PY
```
- [ ] Returns a `simulated` result, no error, no real order.

---

## 3. IBKR — LIVE bracket order  ⚠️ REAL MONEY

Only after Section 1 passes. Use the **smallest possible** size.
1. [ ] Fund/confirm the live IBKR account.
2. [ ] Admin → Config → set `ibkr_paper_mode = false` (app switches to port 4001).
3. [ ] Restart the gateway in live mode: set `vcpilot-ibkr` env `TRADING_MODE=live`, recreate the container, complete any 2FA in the gateway VNC.
4. [ ] Place **1 share** of a liquid name via the dashboard flow (Section 1A), entry as a marketable limit.
5. [ ] Verify the real fill in IBKR + the Position row + WhatsApp/Telegram alert.
6. [ ] **Immediately close** the position (Positions → close, `MANUAL`); confirm the Trade + realised P&L.
7. [ ] Revert: `ibkr_paper_mode = true`, gateway back to `TRADING_MODE=paper`.

---

## 4. Crypto — LIVE order (Independent Reserve)  ⚠️ REAL MONEY

IR has **no testnet** — the only live test is real funds. Use the exchange minimum.
1. [ ] Fund the IR account; confirm API key has **trade** permission (no withdrawal).
2. [ ] Admin → Config → set `crypto_testnet = false`.
3. [ ] Confirm connectivity (read-only):
   ```
   $D exec -u root vcpilot-worker-crypto python -c "from app.broker.crypto import get_crypto_broker_for_org;\
   c=get_crypto_broker_for_org(1); c.connect(); print('connected:', c.is_connected); print('bal:', c.get_balance())"
   ```
   - [ ] `connected: True` and a real balance prints.
4. [ ] Place a **minimum-size** BTC-AUD buy via the dashboard (promote → entry), or a single `submit_bracket_order` with the smallest qty IR allows.
5. [ ] Verify the fill on IR + Position row + alert.
6. [ ] **Sell/close** immediately; confirm Trade + realised P&L.
7. [ ] Revert `crypto_testnet = true` when done testing.

---

## 5. Post-test cleanup
- [ ] No leftover open Positions from testing (close any).
- [ ] No dangling open orders in IBKR (gateway VNC → cancel).
- [ ] `regime_bear_block_equities` re-enabled.
- [ ] `ibkr_paper_mode = true`, `crypto_testnet = true`.
- [ ] `trading_paused` back to your normal setting.

## Troubleshooting
- **IBKR `TimeoutError` on connect** → the gateway grants its API session to the
  running worker; a second client times out. Stop the worker for the test or
  whitelist a second clientId in the gateway API settings.
- **`PermissionError` running a copied script in a container** → QNAP `docker cp`
  adds an ACL; run the command with `-u root` (as shown above).
- **Crypto `NoneType is not iterable`** → fixed 2026-06-24 (`app/broker/crypto.py`);
  make sure the image is rebuilt from latest `main`.
