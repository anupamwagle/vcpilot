"""
Exit Rules — Both defensive (cut losses) and offensive (take profits).

Defensive (mandatory — cannot be disabled):
  exit_stop_loss            — Hard stop hit → exit immediately
  exit_time_stop            — Not up 10% in N weeks → reassess / exit
  exit_market_regime        — Market goes BEAR → exit all / reduce
  exit_earnings_avoid       — Exit N days before earnings

Offensive (configurable):
  exit_profit_target_1      — Take partial profits at 20-25%
  exit_profit_target_2      — Take remaining at 40-50%
  exit_climax_top           — Exhaustion gap-up on extreme volume
  exit_three_weeks_tight    — Three weekly closes within 1.5% → hold (no exit)
  exit_parabolic_move       — 3+ consecutive weeks up >5% each
  exit_break_below_50ma     — Close below 50MA on volume
  exit_round_number         — Partial exit at prior resistance / round number
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Optional
import pandas as pd
from loguru import logger

from app.screener.rules import RuleEngine, RuleResult
from app.models.trade import ExitReason


@dataclass
class ExitSignal:
    should_exit: bool
    reason: Optional[ExitReason] = None
    exit_type: str = "FULL"          # FULL | PARTIAL
    partial_pct: float = 100.0       # % of position to exit
    message: str = ""
    rule_id: str = ""


def evaluate_exit_rules(
    ticker: str,
    entry_price: float,
    current_price: float,
    current_stop: float,
    entry_date: date,
    today: date,
    weekly_closes: list[float],      # Last 3–5 weekly closing prices (latest first)
    df_daily: pd.DataFrame,          # Recent daily bars
    avg_vol_50: float,
    next_earnings_date: Optional[date],
    engine: RuleEngine,
    pivot_price: Optional[float] = None,   # R3 (CLAUDE.md #42): Signal.pivot_price carried
                                            # onto the Position at creation — None (the
                                            # default) skips exit_failed_breakout entirely.
) -> list[ExitSignal]:
    """
    Evaluate all exit rules for an open position.
    Returns a list of triggered ExitSignals (may be empty if no exit needed).
    Multiple signals can trigger (e.g., both partial target AND trailing stop).
    """
    signals: list[ExitSignal] = []
    latest = df_daily.iloc[-1]
    close = float(latest["close"])
    today_vol = float(latest["volume"])
    pnl_pct = ((current_price - entry_price) / entry_price) * 100
    hold_days = (today - entry_date).days

    # =========================================================================
    # DEFENSIVE EXITS
    # =========================================================================

    # -------------------------------------------------------------------------
    # Hard Stop Loss (MANDATORY — cannot be disabled via DB)
    # -------------------------------------------------------------------------
    rule_id = "exit_stop_loss"
    if close <= current_stop:
        signals.append(ExitSignal(
            should_exit=True, reason=ExitReason.STOP_LOSS, exit_type="FULL",
            message=f"Stop hit: close {close:.3f} ≤ stop {current_stop:.3f}",
            rule_id=rule_id
        ))
        return signals  # Stop hit — exit immediately, don't evaluate other rules

    # -------------------------------------------------------------------------
    # Failed Breakout (R3 / CLAUDE.md #42) — close back below the pivot within
    # N days of entry. A correct breakout should hold above the pivot almost
    # immediately; Minervini cuts these fast rather than waiting for the full
    # stop to be hit — a big part of why his average loss stays ~5-6% instead
    # of the full 8-10% stop.
    # -------------------------------------------------------------------------
    rule_id = "exit_failed_breakout"
    if engine.is_enabled(rule_id) and pivot_price:
        max_days = int(engine.threshold(rule_id) or 3)
        if hold_days <= max_days and close < float(pivot_price):
            signals.append(ExitSignal(
                should_exit=True, reason=ExitReason.FAILED_BREAKOUT, exit_type="FULL",
                message=(f"Failed breakout: close {close:.3f} back below pivot "
                         f"{float(pivot_price):.3f} within {hold_days}d of entry (max {max_days}d)"),
                rule_id=rule_id,
            ))

    # -------------------------------------------------------------------------
    # Time Stop
    # -------------------------------------------------------------------------
    rule_id = "exit_time_stop"
    if engine.is_enabled(rule_id):
        min_pct = float(engine.threshold(rule_id) or 10.0)
        max_weeks = int(engine.threshold("exit_time_stop_weeks") or 3)
        max_days = max_weeks * 5
        if hold_days >= max_days and pnl_pct < min_pct:
            signals.append(ExitSignal(
                should_exit=True, reason=ExitReason.TIME_STOP, exit_type="FULL",
                message=f"Time stop: {hold_days}d held, only {pnl_pct:.1f}% gain (need {min_pct}%)",
                rule_id=rule_id
            ))

    # -------------------------------------------------------------------------
    # Earnings Avoidance (exit N trading days before earnings)
    # -------------------------------------------------------------------------
    rule_id = "exit_earnings_avoid"
    if engine.is_enabled(rule_id) and next_earnings_date:
        days_to_earnings = (next_earnings_date - today).days
        buffer_days = int(engine.threshold(rule_id) or 5)
        early_warn_days = buffer_days * 3   # early warning at 3× the buffer (e.g. 15d if buffer=5)
        if 0 <= days_to_earnings <= buffer_days:
            # Within the exit buffer. Minervini doesn't blanket-exit before every print —
            # he holds through earnings WHEN there is a comfortable profit cushion, and only
            # exits names that are thin/near breakeven (where a gap-down does real damage).
            cushion_rule = "exit_earnings_hold_cushion_pct"
            hold_cushion = engine.is_enabled(cushion_rule)
            cushion_pct = float(engine.threshold(cushion_rule) or 10.0)
            if hold_cushion and pnl_pct >= cushion_pct:
                # Enough cushion — hold through earnings (surfaced as a hold in the log)
                signals.append(ExitSignal(
                    should_exit=False, reason=ExitReason.EARNINGS_AVOID,
                    message=(f"Earnings in {days_to_earnings}d — holding through "
                             f"(cushion {pnl_pct:.1f}% ≥ {cushion_pct:.0f}%)"),
                    rule_id=rule_id
                ))
            else:
                # Thin cushion (or rule disabled) — exit to avoid binary earnings risk
                _why = (f"cushion {pnl_pct:.1f}% < {cushion_pct:.0f}%" if hold_cushion
                        else f"buffer {buffer_days}d")
                signals.append(ExitSignal(
                    should_exit=True, reason=ExitReason.EARNINGS_AVOID, exit_type="FULL",
                    message=f"Earnings in {days_to_earnings}d — exiting per rule ({_why})",
                    rule_id=rule_id
                ))
        elif days_to_earnings <= early_warn_days:
            # Early warning window — flag it but don't exit yet
            # (surfaces in audit log "holding" summary so operator can decide)
            signals.append(ExitSignal(
                should_exit=False, reason=ExitReason.EARNINGS_AVOID,
                message=f"⚠ Earnings approaching in {days_to_earnings}d — monitor position (exit in ≤{buffer_days}d)",
                rule_id=rule_id
            ))

    # =========================================================================
    # OFFENSIVE EXITS
    # =========================================================================

    # -------------------------------------------------------------------------
    # Profit Target 1 (partial exit at 20–25%)
    # -------------------------------------------------------------------------
    rule_id = "exit_profit_target_1"
    if engine.is_enabled(rule_id):
        target_pct = float(engine.threshold(rule_id) or 20.0)
        partial_sell = float(engine.threshold("exit_profit_target_1_sell_pct") or 50.0)
        if pnl_pct >= target_pct:
            signals.append(ExitSignal(
                should_exit=True, reason=ExitReason.PROFIT_TARGET_1,
                exit_type="PARTIAL", partial_pct=partial_sell,
                message=f"Target 1: {pnl_pct:.1f}% gain ≥ {target_pct}% — sell {partial_sell}%",
                rule_id=rule_id
            ))

    # -------------------------------------------------------------------------
    # Profit Target 2 → Trailing give-back stop (let winners run)
    # -------------------------------------------------------------------------
    # Minervini's edge is asymmetric: a few very large winners pay for many small
    # losses. A fixed "sell everything at 40%" cap guarantees you amputate those
    # runners. Instead, once the gain reaches the activation level we trail from the
    # peak-since-entry and only exit when price gives back `exit_trail_giveback_pct`
    # from that high. If the trailing rule is disabled we fall back to the legacy
    # hard full exit at the target so behaviour is never silently lost.
    rule_id = "exit_profit_target_2"
    if engine.is_enabled(rule_id):
        target_pct = float(engine.threshold(rule_id) or 40.0)
        if pnl_pct >= target_pct:
            if engine.is_enabled("exit_trail_giveback_pct"):
                giveback_pct = float(engine.threshold("exit_trail_giveback_pct") or 10.0)
                # Peak since entry (fall back to the full window if dates unavailable)
                try:
                    _since = df_daily[df_daily.index >= pd.Timestamp(entry_date)]
                    peak = float(_since["high"].max()) if not _since.empty else float(df_daily["high"].max())
                except Exception:
                    peak = float(df_daily["high"].max())
                if not peak or peak <= 0:
                    peak = max(current_price, entry_price)
                trail_stop = peak * (1.0 - giveback_pct / 100.0)
                if current_price <= trail_stop:
                    signals.append(ExitSignal(
                        should_exit=True, reason=ExitReason.PROFIT_TARGET_2, exit_type="FULL",
                        message=(f"Trailing stop: +{pnl_pct:.1f}% — gave back ≥{giveback_pct:.0f}% "
                                 f"from peak {peak:.3f} (trail {trail_stop:.3f})"),
                        rule_id=rule_id
                    ))
                else:
                    # Still riding the winner — surface as a hold so it shows in the log
                    signals.append(ExitSignal(
                        should_exit=False, reason=ExitReason.PROFIT_TARGET_2,
                        message=(f"Riding winner +{pnl_pct:.1f}% — trailing stop {trail_stop:.3f} "
                                 f"({giveback_pct:.0f}% below peak {peak:.3f})"),
                        rule_id=rule_id
                    ))
            else:
                # Legacy behaviour: hard full exit at the fixed target
                signals.append(ExitSignal(
                    should_exit=True, reason=ExitReason.PROFIT_TARGET_2, exit_type="FULL",
                    message=f"Target 2: {pnl_pct:.1f}% gain ≥ {target_pct}% — full exit",
                    rule_id=rule_id
                ))

    # -------------------------------------------------------------------------
    # Climax Top (exhaustion — extreme volume + wide range up day after big run)
    # -------------------------------------------------------------------------
    rule_id = "exit_climax_top"
    if engine.is_enabled(rule_id):
        vol_threshold = float(engine.threshold(rule_id) or 250.0)  # % of avg
        min_run = float(engine.threshold("exit_climax_top_min_run") or 50.0)  # % gain before climax
        vol_ratio = (today_vol / avg_vol_50 * 100) if avg_vol_50 > 0 else 0
        day_range_pct = ((float(latest["high"]) - float(latest["low"])) / float(latest["low"]) * 100)
        if pnl_pct >= min_run and vol_ratio >= vol_threshold and day_range_pct >= 3:
            signals.append(ExitSignal(
                should_exit=True, reason=ExitReason.CLIMAX_TOP, exit_type="FULL",
                message=f"Climax top: vol {vol_ratio:.0f}% of avg, range {day_range_pct:.1f}%",
                rule_id=rule_id
            ))

    # -------------------------------------------------------------------------
    # Parabolic Move (3+ consecutive weeks up >5% each)
    # -------------------------------------------------------------------------
    rule_id = "exit_parabolic_move"
    if engine.is_enabled(rule_id) and len(weekly_closes) >= 4:
        weekly_gains = [
            ((weekly_closes[i] - weekly_closes[i+1]) / weekly_closes[i+1]) * 100
            for i in range(min(3, len(weekly_closes)-1))
        ]
        parabolic_threshold = float(engine.threshold(rule_id) or 5.0)
        if all(g >= parabolic_threshold for g in weekly_gains):
            signals.append(ExitSignal(
                should_exit=True, reason=ExitReason.CLIMAX_TOP, exit_type="PARTIAL",
                partial_pct=50.0,
                message=f"Parabolic: 3 weeks up {weekly_gains[0]:.1f}%/{weekly_gains[1]:.1f}%/{weekly_gains[2]:.1f}%",
                rule_id=rule_id
            ))

    # -------------------------------------------------------------------------
    # Break below 50MA on volume (trend break signal)
    # -------------------------------------------------------------------------
    rule_id = "exit_break_below_50ma"
    if engine.is_enabled(rule_id):
        ma50 = float(latest.get("ma_50", 0) or 0)
        vol_ratio = (today_vol / avg_vol_50 * 100) if avg_vol_50 > 0 else 0
        if ma50 > 0 and close < ma50 and vol_ratio >= 100:
            signals.append(ExitSignal(
                should_exit=True, reason=ExitReason.TRAILING_STOP, exit_type="FULL",
                message=f"Broke 50MA {ma50:.3f} on {vol_ratio:.0f}% volume",
                rule_id=rule_id
            ))

    # -------------------------------------------------------------------------
    # 3-Weeks-Tight: THREE consecutive weekly closes within 1.5% → DO NOT EXIT
    # (This overrides any weak exit signal — the stock is coiling for next move)
    # -------------------------------------------------------------------------
    rule_id = "exit_three_weeks_tight"
    if engine.is_enabled(rule_id) and len(weekly_closes) >= 3:
        tight_threshold = float(engine.threshold(rule_id) or 1.5)
        w1, w2, w3 = weekly_closes[0], weekly_closes[1], weekly_closes[2]
        spread_1_2 = abs((w1 - w2) / w2 * 100) if w2 > 0 else 100
        spread_2_3 = abs((w2 - w3) / w3 * 100) if w3 > 0 else 100
        if spread_1_2 <= tight_threshold and spread_2_3 <= tight_threshold:
            # Remove any weak exit signals — let it ride
            signals = [s for s in signals if s.reason in (
                ExitReason.STOP_LOSS, ExitReason.EARNINGS_AVOID
            )]
            if not signals:
                logger.info(f"{ticker}: 3-weeks-tight pattern — holding position")

    return signals
# end evaluate_exit_rules
