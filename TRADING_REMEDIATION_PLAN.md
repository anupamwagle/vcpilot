# AstraTrade — Trading Engine Remediation Plan (audit of 5 Jul 2026)

> Companion to [REMEDIATION_PLAN.md](REMEDIATION_PLAN.md) (security/architecture). This file covers the
> **dashboard, watchlist, signals, orders and positions pipeline** — the money path. Written for Sonnet
> to execute item-by-item before the release next week.
>
> **Guardrails for the executing agent:** read CLAUDE.md first (esp. #30 Position-vs-Trade close
> pattern, #34 one-live-intent, #9 `_run_screen_force`). One item per branch. Run
> `docker compose run --rm --no-deps worker-equities pytest -q` after each — no new failures. Extend
> `tests/` for every behaviour change (the conftest SQLite harness + monkeypatch patterns are already
> established). Never place/cancel live orders from test code — monkeypatch `IBKRBroker`.
>
> Severity: 🔴 must fix before release · 🟠 should fix before release · 🟡 fast follow

---

## Root-cause summary (what the audit found)

The four symptoms the owner reported all trace to **one missing component and two wiring gaps**:

1. **There is no order-status reconciliation task at all.** `Order` rows are created as `SUBMITTED`
   ([app/tasks/trading.py:740](app/tasks/trading.py:740)) and *nothing ever updates them* — the only
   transition in the entire codebase is the manual cancel button
   ([web/main.py:1857](web/main.py:1857)). No task polls IBKR for fills, cancellations, or DAY-expiry.
   That is why open orders look stale: they are.
2. **The signal is marked TRIGGERED at submission, not at fill**
   ([app/tasks/trading.py:793](app/tasks/trading.py:793)), and a `Position` row is only created in the
   *simulated* path ([trading.py:768](app/tasks/trading.py:768)). A real IBKR fill only becomes a DB
   position when `sync_ibkr_positions_task` later "imports an orphan" — with the stop **defaulted to
   -10%** and targets +20/+40% ([trading.py:1877](app/tasks/trading.py:1877)), losing the signal's real
   VCP stop, targets and signal linkage. An unfilled DAY order silently evaporates at the close: order
   stuck SUBMITTED, signal stuck TRIGGERED (hidden from every view), no position — the trade is lost
   with zero telemetry.
3. **Equity P&L never uses IBKR.** `update_position_pnl_task` calls
   `get_intraday_price(ticker, asset_type=…)` **without `organization_id`**
   ([trading.py:1365](app/tasks/trading.py:1365)) — and the IBKR branch in
   [app/data/fetcher.py:1532](app/data/fetcher.py:1532) requires `organization_id is not None`, so it
   always falls through to yfinance 15-min delayed bars. `refresh_live_prices_cache_task` skips
   equities entirely ([trading.py:1435](app/tasks/trading.py:1435)), so watchlist/signals UI equity
   prices are EOD. The fetcher's priority order is already correct — the *callers* defeat it.

Everything below builds on fixing those three.

---

## T1 🔴 — Build `sync_order_status`: the missing order-fill reconciliation task (L)

**New Celery task** `app.tasks.trading.sync_order_status(exchange_key=None, organization_id=None)`,
scheduled every 5 min during each market session (mirror the `sync-stops` beat entries incl. the US
23:00/0–6h windows) **plus one run 20 min after each session close** (catches DAY expiry).

Per org with a configured `ibkr_account` (reuse the guard at [trading.py:1776](app/tasks/trading.py:1776)):

1. Connect once. Pull `reqAllOpenOrders()` (already wrapped: `IBKRBroker.get_open_orders()`), and add a
   new broker method `get_executions(days=2)` wrapping `reqExecutions` + `ib.fills()` — return
   `{perm_id, order_id, order_ref, ticker, side, qty, avg_price, commission, time}` per fill
   (commission from `commissionReport`).
2. For every DB `Order` in `SUBMITTED`/`PENDING` (match on `ibkr_order_id`, fall back to
   `order_ref == f"astratrade-{signal_id}"` — the ref is already set at
   [app/broker/ibkr.py:354](app/broker/ibkr.py:354)):
   - **Filled (parent BUY):** set `status=FILLED`, `qty_filled`, `avg_fill_price` (real fill, not the
     confirm price), `filled_at`. **Create the `Position` here** from the linked Signal: entry = actual
     avg fill, `initial_stop`/`current_stop` = `signal.stop_price`, targets = signal targets,
     `signal_id` linkage, `risk_aud` recomputed from the real fill. Skip creation if the position
     already exists (the position-sync import may have won the race — in that case *repair* it: set
     signal_id, real stop/targets, real entry price, and audit the repair). AuditLog `ORDER_FILLED` +
     `POSITION_OPENED` + Telegram fill alert (currently the "order fill" Telegram fires at submission —
     move the *fill* wording here, keep a "submitted" notice at submission).
   - **Partially filled at session end:** record `qty_filled`, create/adjust the Position for the
     filled quantity, audit clearly.
   - **Cancelled / Inactive / DAY-expired** (order no longer in open orders and no fill executions):
     set `status=CANCELLED` (add `EXPIRED` to `OrderStatus` enum via migration if distinct value
     wanted), and **revert the Signal `TRIGGERED → PENDING`** with an AuditLog ("entry order expired
     unfilled — signal re-armed for next session"). The next session's `check_entry_triggers` will
     re-validate breakout conditions from scratch, which is exactly the Minervini behaviour (breakout
     entries are only valid on a fresh breakout).
   - **SELL orders** (stop child / target child / manual): on fill, close the DB Position via the
     CLAUDE.md #30 pattern (Position→CLOSED + Trade row) using the **execution's** price/commission,
     `exit_reason=STOP_LOSS` for the stop child, `PROFIT_TARGET_1` for the target child. This replaces
     the guesswork in the BROKER_SYNC close path with real fill data.
3. Idempotency: wrap the whole run in a Redis `SET NX EX 240` lock per org; every mutation guarded by
   current-state checks so re-runs are no-ops.

**Why this is the keystone:** it fixes stale open orders (owner concern 1), makes the new
`already_ordered` guard and available-capital math in the uncommitted `check_entry_triggers` work
correctly (dead orders currently block a ticker and reserve capital forever), gives real fill prices to
P&L/stats, and turns `sync_ibkr_positions_task` back into what it should be — a safety net, not the
primary fill-detection mechanism.

**Migrations:** `Order.perm_id` column (int, nullable), optional `OrderStatus.EXPIRED`,
`Order.commission` (numeric, nullable). Alembic/migrate_saas step + model + tests.

**Tests:** monkeypatched-broker scenarios — fill creates position with signal stop; expiry re-arms
signal; stop-child fill closes position with real price; re-run idempotent; partial fill.

---

## T2 🔴 — Minervini-correct entry order type + stale-order strategy (M)

**Current behaviour:** plain **LMT at the breakout-confirm price, TIF=DAY**
([app/broker/ibkr.py:334-355](app/broker/ibkr.py:334)). Because the system detects the breakout *after*
price crosses the pivot, the limit is at-or-below the market at submission. If the stock keeps running
(the exact stocks you want), the order sits below the market all day and dies unfilled at the close —
the "IBKR order not executed when price moved" problem. If price collapses back through the limit, you
get filled on a *failing* breakout — adverse selection in both directions.

**The professional/Minervini-aligned fix** (SEPA buys as close to the pivot as possible, never chases
an extended stock — the "max extension from pivot" rule is already seeded in RuleConfig):

1. **Order type → BUY STOP-LIMIT.** Stop trigger = max(pivot, confirm price); limit = trigger ×
   (1 + `entry_limit_buffer_pct`, default **1.0%**, org-configurable via RuleConfig/SystemConfig).
   This caps slippage at ~1% past the trigger instead of hoping a stale limit gets hit. In ib_insync:
   build the bracket manually (parent `Order(orderType="STP LMT", auxPrice=trigger,
   lmtPrice=trigger*1.01, …)` + the same stop/target children with the existing transmit-flag pattern
   and DAY TIF — keep the atomic-transmit and 10349 TIF notes from the current code).
2. **Extension guard at submission (hard rule):** if the live price is already >
   `max_buy_extension_pct` (default **5%**) above the pivot, do NOT submit. Audit-log
   "breakout extended >5% past pivot — not chasing (Minervini extension rule)"; leave the signal
   PENDING. The seeded VCP `max extension` rule threshold should be the single source of this number —
   read it from the RuleEngine rather than hardcoding.
3. **Working-order babysitter** (add to T1's task, runs every 5 min): for each working entry order,
   fetch the live price:
   - price > limit × (1 + extension buffer beyond the 5% max-buy zone) → **cancel the bracket**, audit
     "cancelled — extended beyond buy range", revert signal to PENDING (it may set up again on a
     pullback or be re-screened tomorrow).
   - price back below the *stop trigger* → leave it working; a stop-limit that hasn't triggered costs
     nothing and simply expires at the close if the breakout fails. (Do not cancel on pullback — the
     stock can re-attempt intra-day; this is standard breakout-desk practice.)
   - DAY expiry handling is already covered by T1 (revert to PENDING).
4. **Keep TIF=DAY.** GTC entry orders on a breakout system are wrong (a fill three days later is a
   different trade); DAY + re-arm via T1 is the correct loop. Document this in CLAUDE.md.
5. Config: `entry_limit_buffer_pct` (SystemConfig per org, seeded 1.0) and reuse the RuleConfig
   extension threshold. Show both on `/admin/config` via `FIELD_HINTS`.

**Tests:** trigger/limit math incl. tick rounding (`_round_to_tick`), extension guard blocks and
audits, babysitter cancels extended orders and reverts the signal.

---

## T3 🔴 — Equity stop handling: remove the double-sell path, modify stops at IBKR (L)

Two serious problems in `sync_stop_orders`'s equity branch
([app/tasks/trading.py:1120-1191](app/tasks/trading.py:1120)):

1. **Double-sell / phantom-short risk.** When the app sees price ≤ stop it submits a **SELL bracket**
   (`submit_bracket_order(action="SELL", stop_price=0, target_price=0)` — malformed: a "bracket" with
   0-priced children) **while the original bracket's stop-loss child is still working at IBKR**. If
   the broker stop fills (it usually will, first), the app's extra sell executes against shares you no
   longer hold → naked short. If the app's sell fills first, the broker stop remains live → same
   problem in reverse. It also closes the DB position even when the IBKR sell call *failed*
   (the exception is swallowed at [trading.py:1153](app/tasks/trading.py:1153)) → DB says flat, broker
   says long.
   **Fix:** for equity positions that have a live IBKR bracket, the app must **never** fire its own
   sell on stop-breach. Detection of the stop firing is T1's job (stop-child execution → close DB from
   the real fill). Keep an app-side breach *alert* (Telegram "price is through your stop, broker stop
   should be executing — check gateway") but no order. Only if the position has **no working stop order
   at IBKR** (imported positions, bracket cancelled) may the app act — and then with a plain
   `MarketOrder("SELL", qty)` (add `IBKRBroker.submit_market_sell()`), after first cancelling any stray
   children, and only mark the DB position closed from the resulting execution (via T1), not
   optimistically.
2. **Trailing stops never reach the broker** (the documented "IBKR stop modify TBD" gap — now a
   release blocker). The DB `current_stop` can rise (exit-rule engine / crypto-style trailing), but the
   IBKR stop child stays at the initial stop — so the *real* protection is stale the moment you trail.
   **Fix:** implement `IBKRBroker.modify_stop_order(ibkr_order_id, new_stop)` — retrieve the working
   stop child via `reqAllOpenOrders`, set `auxPrice=new_stop` (tick-rounded), re-`placeOrder` with the
   same orderId (that's how IBKR modifies). In `sync_stop_orders`, when the equity trailing logic
   raises `current_stop` (see 3), push the change to IBKR *first*, then commit the DB value, and audit
   both old→new. Requires storing the stop child's order id: capture it in T1/order submission
   (`raw_ibkr_response` already holds the legs — add `stop_order_id` to `Order` or a JSON field on
   `Position`).
3. **Equity trailing rules (Minervini progression)** — currently only crypto has ATR trailing.
   Implement for equities, org-rule-driven, evaluated in `sync_stop_orders`:
   - move stop to **breakeven** once unrealised gain ≥ 1R (or ≥ `pyramid min profit` threshold),
   - after target-1 partial (when implemented) or ≥ 2R, trail under the **50-day MA** (Minervini's
     standard trail for leaders) or the low of the most recent 3-weeks-tight — use the exit-rule
     engine's existing MA data (PriceBar table, no network),
   - never lower a stop. Every ratchet → `modify_stop_order` + audit + Telegram.

**Tests:** breach-with-live-bracket does NOT submit a sell; modify pushes before DB commit and rolls
back the DB value if the IBKR modify fails; ratchet never lowers; imported-position (no bracket) path
uses market sell.

---

## T4 🟠 — IBKR as primary price source for equities, everywhere (M)

`get_intraday_price()` already prioritises IBKR → yfinance-backup for equities. The callers defeat it:

1. **[trading.py:1365](app/tasks/trading.py:1365)** `update_position_pnl_task`: pass
   `organization_id=pos.organization_id` (one-line root cause of "pricing is pulled from yfinance").
2. **`refresh_live_prices_cache_task`** ([trading.py:1435](app/tasks/trading.py:1435)): extend to
   equity watchlist + pending-signal tickers during their market's open hours. Use one shared broker
   connection for the whole batch (see 4), write the same `live_price:{ticker}` payload with
   `data_source: "ibkr"`. Outside market hours keep the current skip (EOD PriceBar is correct then).
3. **Web inline fetches**: `/trader/prices`, `/trader/watchlist/data`, `/watchlist` cold-cache paths
   currently only inline-fetch crypto. After (2) the cache covers equities; additionally pass the
   session `organization_id` into any remaining inline `get_intraday_price` equity calls.
4. **Stop reconnecting per ticker.** Every equity price lookup does a full
   connect→handshake→snapshot→disconnect (`with IBKRBroker(...)` per call; handshake up to 8s/port,
   plus a class-level failure cooldown that then poisons *subsequent* callers into yfinance for 60s+).
   Add `IBKRBroker.get_market_snapshots(tickers: list, exchange_key_map) -> dict` that qualifies and
   requests all contracts in ONE connection (`reqMktData` each, single `sleep(2)` pump, then read all),
   and refactor `check_entry_triggers`, `check_exit_rules_task`, `sync_stop_orders`,
   `update_position_pnl_task` to prefetch the run's prices once. This alone will cut the 5-min cycle
   time dramatically and stop entry checks landing on delayed data because a previous connect failed.
5. **Delayed-data fallback inside IBKR before falling to yfinance:** in `get_market_snapshot`
   ([app/broker/ibkr.py:523](app/broker/ibkr.py:523)) call `self._ib.reqMarketDataType(1)` and, when
   no ticks arrive, retry with `reqMarketDataType(3)` (IBKR delayed, ~15 min) before giving up. Use
   bid/ask midpoint when `last` is NaN (thin ASX names). Return `data_source: "ibkr_delayed",
   delay_mins: 15` so the Data Log banner stays honest. IBKR-delayed beats yfinance-delayed (same
   latency class, but consistent with fills/stops and no rate limits).
6. Add `data_source` counters to the entry-check audit (already stored per check in
   `entry_check_logs`) — surface a warning chip on `/admin/health` when >50% of the last hour's equity
   checks used `yfinance` while the gateway was supposedly connected (drift detector).

**Tests:** pnl task passes org id (assert via monkeypatched fetcher), batch snapshot used once per run,
delayed-type fallback path.

---

## T5 🟠 — Position-sync gaps (S/M)

[app/tasks/trading.py:1737](app/tasks/trading.py:1737) `sync_ibkr_positions_task` is solid on safety
guards, but:

1. **Not scheduled during the US session.** Beat entry (`celery_app.py:132`) runs 10:00–16:00 AEST
   Mon–Fri only — a NYSE fill at 2am AEST isn't reconciled for ~8 hours. Add the same 23h + 0–6h
   Tue–Sat entries used by `sync-stops`. (After T1 this is a safety net, but the net should cover both
   sessions.)
2. **Import heuristics mislabel.** `currency == "USD" → exchange_key "NYSE"` labels every NASDAQ stock
   NYSE, and anything AUD gets `.AX` appended ([trading.py:1874](app/tasks/trading.py:1874)). Use
   `contract.primaryExchange` from `get_open_positions` (extend the dict) to map NASDAQ correctly.
3. **Repair, don't duplicate.** Before importing an "orphan" IBKR holding, check for a recent
   FILLED/SUBMITTED BUY `Order` / TRIGGERED `Signal` for that symbol and org; if found, build the
   position from the signal (real stop/targets/linkage) instead of the -10%/+20/+40 defaults. (T1 makes
   this rare; keep it for gateway-restart races.)
4. The `-10% default stop — review` import already audits; also send a Telegram alert, since an
   unreviewed default stop is a real risk exposure.

---

## T6 🟡 — Fill/stat accuracy (S, mostly falls out of T1)

- `Order.avg_fill_price`, real commissions (from `commissionReport`) → `Trade.net_pnl_aud` instead of
  the hardcoded A$6 ([trading.py:1141](app/tasks/trading.py:1141)).
- `BROKER_SYNC` closes: after T1, prefer the actual execution price over "last known price"; keep the
  current behaviour only when no execution is found (and say so in the audit row).
- Position `risk_aud` recompute on real fill (T1) keeps portfolio-heat maths honest.

## T7 🟡 — Exit-check cycle efficiency (S)

[app/tasks/trading.py:889-928](app/tasks/trading.py:889) per position per 5 min:
- `get_fundamentals(pos.ticker)` is a live yfinance call fetched **only** for `next_earnings_date` —
  cache in Redis for 24h (`earnings_date:{ticker}`).
- Verify `get_price_history(pos.ticker, period="6mo")` serves from the `price_bars` table rather than
  the network; if it hits yfinance, add a DB-first path for tickers whose EOD bars are current. The
  intraday component already comes via `get_intraday_price`.
- After T4.4, prices for the whole run come from one broker connection.

## T8 🟡 — Dashboard/watchlist/signals staleness UX (S/M)

- **Signals page:** a TRIGGERED signal disappears from all views the moment the order is submitted —
  the user can't tell "working order" from "filled" from "lost". Add a *Working Orders* state: show
  TRIGGERED signals with their DB `Order` status chip (SUBMITTED/FILLED/CANCELLED/EXPIRED — accurate
  after T1), so the lifecycle is visible end-to-end. Keep them out of the "pending" counts.
- **Open Orders panel** ([web/main.py:1773](web/main.py:1773)) is live-IBKR only; when the gateway is
  down it shows an empty list indistinguishable from "no orders". Fall back to DB `Order` rows in
  SUBMITTED/PENDING with a "gateway offline — last known state" banner.
- **Watchlist/home equity prices** become live during market hours via T4.2's cache; make the
  price-source/delay chip (already on Data Log) visible on Positions and Watchlist rows
  (`data_source`/`delay_mins` are already in the live_price payload).
- Cancel route ([web/main.py:1861](web/main.py:1861)): the "phantom position" delete matches *any* OPEN
  position by ticker — after T1 creates real positions on fill, restrict the delete to positions with
  `signal_id == order.signal_id` **and** no fill executions, else you can delete a genuinely filled
  position while cancelling a leftover order.

## T9 🟡 — Release-week safety rails (S each)

- **Overlap locks:** Redis `SET NX EX` per (task, org, exchange) around `check_entry_triggers`,
  `sync_order_status`, `sync_stop_orders` bodies. With the new available-capital math, two overlapping
  entry runs can double-spend capital; with order tasks, double-submit. (= item A12 in the main plan,
  promoted to release-blocking for these three tasks.)
- **Kill switch:** per-org `trading_kill_switch` SystemConfig checked at the top of
  `check_entry_triggers` and `place_order` MCP tool; flip via dashboard + Telegram `STOP`. Faster and
  blunter than pause (pause already exists — kill switch should also cancel working entry orders).
- **Max daily loss halt:** sum today's realised (Trade rows) + unrealised P&L per org; if below
  `max_daily_loss_aud` (SystemConfig, default off), stop new entries for the day + Telegram alert.
- **Opening-noise guard:** ASX opens by staggered auction 10:00–10:09; the 10:00/10:05 entry checks can
  confirm "breakouts" on auction prints and *partial-day* volume. Add
  `entry_skip_open_minutes` (SystemConfig, default 10) and — worth a dedicated review — verify the
  breakout **volume confirmation compares time-of-day-projected volume** (e.g. cumulative volume ÷
  fraction of session elapsed vs 50-day avg), not raw partial-day volume against a full-day average,
  which under-confirms in the morning and over-confirms never. Fix if it's raw.
- **P0.1 from the main plan is a prerequisite:** commit + verify the in-flight capital-guard work
  (confirm `submitted_value_this_run` is incremented — it is, at
  [trading.py:745](app/tasks/trading.py:745) — and the FX direction in the share-cap block).

---

## Execution order for Sonnet

| # | Item | Depends on | Size |
|---|------|-----------|------|
| 1 | T4.1 (one-line org_id fix) + T4.5 delayed fallback | — | S |
| 2 | T1 order-status task + migrations | — | L |
| 3 | T3.1 remove double-sell path (safe once T1 detects stop fills) | T1 | M |
| 4 | T2 stop-limit entry + extension guard + babysitter | T1 | M |
| 5 | T3.2/T3.3 stop modification + equity trailing | T1 | L |
| 6 | T9 locks, kill switch, daily-loss halt, opening guard | — | M |
| 7 | T5 position-sync gaps | T1 | S |
| 8 | T4.2–T4.4 batch snapshots + equity live cache | — | M |
| 9 | T8 UI states + T6 stats + T7 perf | T1 | M |

Items 1–6 are the release blockers. 7–9 can land in the first week post-release without risk.
(See also sections R and I below — R1, I1, I2, I3 join the release-blocker list.)

---

# R — Minervini rule-config audit (seed vs the books)

Audited `scripts/seed_config.py` RULE_CONFIGS against *Trade Like a Stock Market Wizard* and
*Think & Trade Like a Champion*.

**Verdict: the seed is largely faithful.** All 8 Trend Template criteria are present with correct
defaults (200MA slope lookback 21 days ≈ the book's "at least 1 month"; ≥30% above 52-w low; within
25% of 52-w high; RS floor deliberately raised 70→80 and documented — a defensible tightening).
Fundamentals match the Code-33 spirit (EPS +25% QoQ, acceleration, annual +25%, sales +25%, ROE 17%,
improving margins, institutional sponsorship). VCP rules (volume dry-up ≤50% of 50-d avg, breakout
volume ≥150%, 5% max chase), stop discipline (mandatory stop, 8% cap with the 10%-never note), the
earnings-cushion hold, 20–25% partial profit-taking, trailing give-back instead of a hard cap, climax
top / parabolic / 3-weeks-tight, 2% risk per trade, and pyramid-only-winners are all consistent with
the books. The following items are the deviations and gaps:

- **R1 🔴 `risk_max_position_pct` default 30% → 25%. (S)**
  [seed_config.py:599-606](scripts/seed_config.py:599). Minervini's stated maximum in one name is
  20–25% (a 50% position he calls outright dangerous), so 30% as a *default* over-concentrates every
  new org. Change the seeded default to 25.0 and fix the `minervini_ref` text ("cap single-name
  exposure (≤ 50%)" misstates the book — the 25% figure is the cap, 50% is the cautionary example).
  Data migration: update existing org rows only where the value is still the untouched default 30.0
  (don't clobber deliberate overrides). AW org check included in the migration output.

- **R2 🟠 Missing rule: minimum liquidity (average daily dollar volume). (M)**
  Minervini trades institutional-quality liquidity only; the seed's only proxy is
  `entry_min_share_price`, which is **disabled by default** and price ≠ liquidity. On ASX —
  especially with `asx_universe_scope=ALL_LISTED` (~2,200 tickers) — the screener can put genuinely
  untradeable names on the watchlist, and a 2%-risk position can be multiple days of volume. Add
  `entry_min_avg_dollar_volume` (ENTRY, asset_types=EQUITY, default A$500,000/day over 50 days,
  org-tunable, enabled by default): enforce in the screener (PriceBar has volume + close; add the
  rolling turnover calc where `avg_vol_50` is already computed) and re-check in
  `check_entry_triggers`. Also cap position size at a % of avg daily volume (e.g. 20%) inside
  `calculate_position_size` so fills are realistic.

- **R3 🟠 Missing rule: failed-breakout exit. (M)**
  There is no defensive exit for a breakout that *fails* — currently a failed breakout just drifts
  down to the full stop. Minervini's practice is to cut quickly when the breakout doesn't act right:
  price closing back below the pivot (or violating the breakout day's low) within days of entry is
  the sell signal, and it's a big part of how his average loss stays ~5–6% instead of the full 8–10%
  stop. Add `exit_failed_breakout` (EXIT_DEFENSIVE, enabled by default): exit if a daily close is
  back below the pivot price within `N` days of entry (threshold default 3, range 1–10). The
  Signal's `pivot_price` must be carried onto the Position for this (small migration — T1's
  position-creation path is the natural place to set it). Implement in
  `app/screener/exit_rules.py::evaluate_exit_rules` alongside the existing defensive checks, and add
  `FAILED_BREAKOUT` to `ExitReason` + `EXIT_REASON_RATIONALE` (same enum-migration pattern as
  BROKER_SYNC, CLAUDE.md #35).

- **R4 🟡 `vcp_min_contractions` default 3 → 2. (S)**
  [seed_config.py:297-304](scripts/seed_config.py:297). The book describes VCPs with typically 2–4
  contractions (up to 5–6); requiring 3 as the default rejects textbook 2-T patterns. The
  threshold_min already allows 2 — change the default to 2.0, same guarded data-migration approach
  as R1. (Keep orgs' explicit overrides.)

- **R5 🟡 Time stop is stricter than the book — review, don't silently change. (S)**
  `exit_time_stop` = "not up 10% in 3 weeks → exit". Minervini does rotate out of laggards, but
  10%-in-3-weeks is aggressive enough to eject slow-but-valid bases in a quieter tape (ASX moves
  slower than 1990s NASDAQ). Recommended defaults: gain 5% / 4 weeks, and never fire while the
  3-weeks-tight hold rule is active (verify `evaluate_exit_rules` gives 3WT precedence over the time
  stop — if it doesn't, that's a bug to fix regardless). Present to the owner as a config change —
  it's their risk posture, both are defensible.

- **R6 🟡 Stale comment: `trend_template.py` header says "RS ≥ 70"** while the seeded default is 80.
  The engine reads the DB threshold so behaviour is correct — fix the comment
  ([app/screener/trend_template.py:13](app/screener/trend_template.py:13)) to stop the next reader
  "fixing" the wrong number.

- **R7 🟡 Rules seeded but not implemented — hide or label them. (S)**
  `entry_sector_leadership` (sector RS ranking not implemented — CLAUDE.md "What's NOT Built Yet")
  and `pyramid_min_profit_pct`/`pyramid_max_count` (pyramiding task logic TBD) render on
  `/admin/rules` as if toggling them does something. Until implemented, show a "not yet active"
  badge (add an `is_implemented` flag or a hardcoded set in the rules route) so orgs don't build
  expectations on dead switches. Post-release: implement sector RS (the data exists —
  `compute_rs_ratings` + sector labels) and pyramiding.

- **R8 🟡 Covered elsewhere, cross-references:** stop-never-widens + breakeven/50MA trailing
  progression = T3.3; breakout *entry* mechanics (stop-limit, extension guard, DAY expiry) = T2;
  time-of-day-projected breakout volume = T9. A follow-through-day style regime re-entry signal
  (Minervini leans on the equivalent when a correction ends) is a worthwhile post-release addition to
  `market_regime.py` — the current BULL/CAUTION/BEAR (index vs 200MA + breadth + distribution days)
  is a sound approximation and IBD-consistent as-is.

---

# I — Per-org IBKR model + paper/live simplification

Owner's requirement: **one org ↔ one IBKR account, and paper-vs-live must come from the IBKR login
itself** (IBKR separates paper/live by username — DU*/DF* accounts are paper, U* are live). No
app-side paper/live config that can contradict reality. The audit found the app currently has **five
overlapping paper/live indicators**, and three of them can silently disagree:

| # | Flag | Where | Actually does |
|---|------|-------|----------------|
| 1 | Gateway login (`IBKR_USERNAME` in `.env` → `TWS_USERID`) + `TRADING_MODE` | [docker-compose.yml:261-263](docker-compose.yml:261) | **The only one that's real** |
| 2 | Per-org `ibkr_paper_mode` SystemConfig | read at [app/broker/ibkr.py:68](app/broker/ibkr.py:68) | Picks socat port 4004 vs 4003 — but `connect()` **tries both ports anyway** ([ibkr.py:157](app/broker/ibkr.py:157)), so a wrong flag can't prevent a live connection; it only mislabels one |
| 3 | Global `settings.ibkr_paper_mode` | [app/config.py:113](app/config.py:113) | Fallback for #2 — via the org-unfiltered DB lookup (bug S4) |
| 4 | `Account.is_paper` DB column | [app/models/account.py:84](app/models/account.py:84) | Drives every `is_paper` tag on Orders/Positions/Trades and all UI badges — never validated against the gateway |
| 5 | `ibkr_simulate` | global SystemConfig | Internal no-broker simulation (legitimately separate, keep) |

And a live inconsistency: **Migration in `migrate_saas.py` already deletes** the per-org
`ibkr_username`/`ibkr_password`/`ibkr_paper_mode` rows ("they belong in .env only",
[migrate_saas.py:471-476](scripts/migrate_saas.py:471)) — but [seed_config.py:43-49](scripts/seed_config.py:43)
still seeds them, `IBKRBroker.__init__` still reads `ibkr_paper_mode`, `Settings` still exposes
`ibkr_username`/`ibkr_password` properties that **no runtime code uses** (the gateway reads `.env`
directly), and CLAUDE.md still documents `ibkr_paper_mode` as a per-org key. This is exactly the
confusion the owner flagged.

- **I1 🔴 Make the gateway login the single source of truth for paper/live. (M)**
  1. On successful `connect()`, read `self._ib.managedAccounts()`; derive
     `detected_paper = account.startswith(("DU", "DF"))` for the org's resolved account. Store as
     `self.detected_paper_mode` and log it.
  2. `submit_bracket_order` (and T1's fill handler) stamp `is_paper` on Order/Position/Trade from
     `detected_paper_mode` — **not** from `Account.is_paper`.
  3. If `detected_paper_mode != Account.is_paper`, write a loud AuditLog + Telegram alert once per
     day ("app says live, gateway is logged into a PAPER account" or vice versa) and surface a
     banner on `/admin/health`; auto-correct `Account.is_paper` to the detected value (it's a label,
     reality already happened).
  4. Delete the dead config: remove `ibkr_username`/`ibkr_password`/`ibkr_paper_mode` from
     `seed_config.py` SYSTEM_CONFIGS, remove the `ibkr_paper_mode` read in `IBKRBroker.__init__`
     (keep the socat-port normalisation, defaulting to try 4004 then 4003 — connect already falls
     back), remove the unused `Settings.ibkr_username/ibkr_password` properties, and remove these
     keys from `FIELD_HINTS`/CLAUDE.md. Keep `.env` `IBKR_PAPER_MODE` **only** as the gateway
     container's `TRADING_MODE` input, with a comment in `.env.example`: "must match the type of
     IBKR_USERNAME (paper creds ⇒ paper). The app auto-detects paper/live from the logged-in
     account and needs no setting."
  5. `Settings.ibkr_paper_mode` remains only as the compose-level convenience; nothing in the order
     path may read it after this change.

- **I2 🔴 Enforce org ↔ ibkr_account one-to-one. (S)**
  Nothing stops two orgs saving the **same** `ibkr_account` — both would then submit orders to and
  reconcile against the same real account (double entries, cross-org closes). Add validation in the
  `/admin/config/{id}/update` route and the superadmin org-config path: on saving `ibkr_account`,
  reject (with a clear error) if another org already has that value (trimmed, case-insensitive).
  Add a `migrate_saas` check that *reports* any existing duplicates (don't auto-fix — a human must
  decide which org keeps it). The existing runtime guards are good and stay: connect refuses
  without an org-own account ([ibkr.py:99](app/broker/ibkr.py:99)), sync skips on
  account-mismatch, `order.account` routes to the sub-account ([ibkr.py:357](app/broker/ibkr.py:357)).

- **I3 🔴 Cross-org open-orders leak — `account` field missing. (S)**
  `IBKRBroker.get_open_orders()` ([ibkr.py:499-515](app/broker/ibkr.py:499)) omits the order's
  `account`, so the `/positions/open-orders` filter
  (`o.get("account") == acct`, [web/main.py:1806](web/main.py:1806)) compares against `None` and
  **keeps everything** — under a multi-org (FA/linked) gateway every org sees every org's working
  orders, and the same un-filter applies anywhere else the dict is consumed. Fix: add
  `"account": getattr(o, "account", "") or ""` to the dict. T1's execution matching must filter by
  account too (note added there). Add a regression test with two fake orders on different accounts.

- **I4 🟠 Document + guard the single-login gateway model. (S)**
  One gateway container = one IBKR login. Multi-org therefore only works when every org's
  `ibkr_account` is a sub-account of that login (Family/FA or linked accounts) — otherwise an org's
  orders are submitted to a gateway that cannot trade its account. Current behaviour on mismatch is
  scattered (sync detects it; submission would be rejected by IBKR downstream). Make it explicit:
  at `connect()`, if the org's `ibkr_account` is not in `managedAccounts()`, set
  `last_error="account X not managed by this gateway login"`, refuse order submission (return the
  standard error result so signals stay PENDING), and audit. Write the model into CLAUDE.md
  (Broker section): "paper/live = which username the gateway is logged into; org isolation = IBKR
  sub-account routing + app-side account filter; to give an org a fully separate login it needs its
  own gateway container (per-org `ibkr_host`/`ibkr_port` SystemConfig — supported by
  `IBKRBroker.__init__` today via settings defaults, formalise post-release)."

- **I5 🟡 Cross-reference:** the global `settings.ibkr_*` properties ride on the org-unfiltered
  `_get_db_config` (main plan item S4). After I1 removes the username/password/paper properties,
  S4's IBKR exposure shrinks to `ibkr_account` for org-less callers — still fix S4, but the blast
  radius drops.

- **I6 🟡 UI/docs sweep. (S)** Remove paper/live wording that implies the app controls it:
  `/admin/config` broker group hints, the `is_paper` badge tooltips (should say "detected from the
  IBKR login"), CLAUDE.md Core-Config table (`ibkr_paper_mode` row), `.env.example` comments, and
  the `web/main.py:631` fallback (`os.getenv("IBKR_PAPER_MODE")`) which should use the
  detected/Account value instead of the env var.

---

## Updated release-blocker list

T1, T2, T3, T4.1, T9 (locks/kill switch), **R1, R2, R3, I1, I2, I3**. Everything else is fast-follow.
Suggested insertion into the execution order: I3 and R1 immediately (one-liners), I1+I2 right after
T1 (they touch the same submission path), R2+R3 alongside T2 (same screener/exit files).
