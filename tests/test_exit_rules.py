"""
Tests for app/screener/exit_rules.py — evaluate_exit_rules().

All rules are exercised directly (no DB, no Celery) using a minimal
stub RuleEngine that returns predictable thresholds.
"""
from datetime import date, timedelta
import pandas as pd
import pytest

from app.screener.exit_rules import evaluate_exit_rules, ExitSignal
from app.models.trade import ExitReason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class StubEngine:
    """Minimal RuleEngine stand-in: all rules enabled, thresholds configurable."""

    def __init__(self, thresholds: dict = None, disabled: set = None):
        self._thresholds = thresholds or {}
        self._disabled = disabled or set()

    def is_enabled(self, rule_id: str) -> bool:
        return rule_id not in self._disabled

    def threshold(self, rule_id: str):
        return self._thresholds.get(rule_id)


def _make_df(close=10.0, high=10.5, low=9.5, volume=100_000, ma_50=9.0, rows=60):
    """Return a minimal daily DataFrame with the last row matching the given values."""
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(rows)]
    data = {
        "date": dates,
        "open": [close] * rows,
        "high": [high] * rows,
        "low": [low] * rows,
        "close": [close] * rows,
        "volume": [volume] * rows,
        "ma_50": [ma_50] * rows,
        "ma_200": [close * 0.9] * rows,
        "avg_vol_50": [volume] * rows,
    }
    return pd.DataFrame(data)


def _call(
    ticker="WOW.AX",
    entry_price=10.0,
    current_price=11.0,
    current_stop=8.5,
    entry_date=date(2026, 1, 1),
    today=date(2026, 4, 1),   # ~65 days later
    weekly_closes=None,
    df=None,
    avg_vol_50=100_000.0,
    next_earnings_date=None,
    engine=None,
    pivot_price=None,
):
    return evaluate_exit_rules(
        ticker=ticker,
        entry_price=entry_price,
        current_price=current_price,
        current_stop=current_stop,
        entry_date=entry_date,
        today=today,
        weekly_closes=weekly_closes or [11.0, 10.8, 10.6, 10.4, 10.2],
        df_daily=df if df is not None else _make_df(close=current_price),
        avg_vol_50=avg_vol_50,
        next_earnings_date=next_earnings_date,
        engine=engine or StubEngine(),
        pivot_price=pivot_price,
    )


# ---------------------------------------------------------------------------
# Stop loss
# ---------------------------------------------------------------------------

def test_stop_loss_triggers_when_close_at_stop():
    df = _make_df(close=8.4)  # close <= stop 8.5
    sigs = _call(current_price=8.4, current_stop=8.5, df=df)
    assert len(sigs) == 1
    assert sigs[0].reason == ExitReason.STOP_LOSS
    assert sigs[0].should_exit


def test_stop_loss_does_not_trigger_above_stop():
    df = _make_df(close=10.0)
    sigs = _call(current_price=10.0, current_stop=8.5, df=df,
                 engine=StubEngine(disabled={"exit_time_stop", "exit_profit_target_1",
                                             "exit_profit_target_2", "exit_climax_top",
                                             "exit_parabolic_move", "exit_break_below_50ma",
                                             "exit_three_weeks_tight"}))
    assert not any(s.reason == ExitReason.STOP_LOSS for s in sigs)


def test_stop_loss_returns_immediately_no_other_rules():
    """When stop hits, function returns immediately — no other signals appended."""
    df = _make_df(close=8.0, volume=9_000_000)  # also meets climax/volume conditions
    engine = StubEngine(thresholds={"exit_climax_top": 250.0, "exit_climax_top_min_run": 5.0})
    sigs = _call(current_price=8.0, current_stop=8.5, df=df, engine=engine)
    assert len(sigs) == 1
    assert sigs[0].reason == ExitReason.STOP_LOSS


# ---------------------------------------------------------------------------
# Failed breakout (R3 / CLAUDE.md #42)
# ---------------------------------------------------------------------------

def test_failed_breakout_triggers_close_below_pivot_within_window():
    # Entry day 1, checked on day 2 (1 day held) — well within the default 3-day window.
    df = _make_df(close=9.5)  # close is below pivot (10.0), above stop (8.5)
    sigs = _call(
        current_price=9.5, current_stop=8.5, df=df,
        entry_date=date(2026, 1, 1), today=date(2026, 1, 2),
        pivot_price=10.0,
        engine=StubEngine(disabled={"exit_time_stop"}),
    )
    assert any(s.reason == ExitReason.FAILED_BREAKOUT and s.should_exit for s in sigs)


def test_failed_breakout_does_not_trigger_above_pivot():
    df = _make_df(close=10.5)
    sigs = _call(
        current_price=10.5, current_stop=8.5, df=df,
        entry_date=date(2026, 1, 1), today=date(2026, 1, 2),
        pivot_price=10.0,
        engine=StubEngine(disabled={"exit_time_stop"}),
    )
    assert not any(s.reason == ExitReason.FAILED_BREAKOUT for s in sigs)


def test_failed_breakout_does_not_trigger_outside_window():
    """Close back below pivot, but only after the max-days window has passed."""
    df = _make_df(close=9.5)
    sigs = _call(
        current_price=9.5, current_stop=8.5, df=df,
        entry_date=date(2026, 1, 1), today=date(2026, 1, 10),  # 9 days later
        pivot_price=10.0,
        engine=StubEngine(thresholds={"exit_failed_breakout": 3.0}, disabled={"exit_time_stop"}),
    )
    assert not any(s.reason == ExitReason.FAILED_BREAKOUT for s in sigs)


def test_failed_breakout_skipped_when_no_pivot_price():
    """Positions with no carried-over pivot_price (e.g. created before this
    column existed) must never trigger — the check is a no-op without it."""
    df = _make_df(close=9.5)
    sigs = _call(
        current_price=9.5, current_stop=8.5, df=df,
        entry_date=date(2026, 1, 1), today=date(2026, 1, 2),
        pivot_price=None,
        engine=StubEngine(disabled={"exit_time_stop"}),
    )
    assert not any(s.reason == ExitReason.FAILED_BREAKOUT for s in sigs)


def test_failed_breakout_disabled_rule_never_triggers():
    df = _make_df(close=9.5)
    sigs = _call(
        current_price=9.5, current_stop=8.5, df=df,
        entry_date=date(2026, 1, 1), today=date(2026, 1, 2),
        pivot_price=10.0,
        engine=StubEngine(disabled={"exit_time_stop", "exit_failed_breakout"}),
    )
    assert not any(s.reason == ExitReason.FAILED_BREAKOUT for s in sigs)


def test_failed_breakout_takes_priority_over_time_stop_when_both_trigger():
    """Order matters — the caller (check_exit_rules_task) acts on the FIRST
    should_exit=True signal, so failed-breakout (placed earlier in the
    function) must win when both could plausibly apply."""
    df = _make_df(close=9.5)
    sigs = _call(
        current_price=9.5, current_stop=8.5, df=df,
        entry_date=date(2026, 1, 1), today=date(2026, 1, 2),
        pivot_price=10.0,
        engine=StubEngine(thresholds={"exit_time_stop": 100.0, "exit_time_stop_weeks": 0}),
    )
    first_exit = next(s for s in sigs if s.should_exit)
    assert first_exit.reason == ExitReason.FAILED_BREAKOUT


# ---------------------------------------------------------------------------
# Time stop
# ---------------------------------------------------------------------------

def test_time_stop_triggers_after_max_weeks_with_low_gain():
    # 20 days held, 3-week / 15-day max, gain only 3% (< 10% threshold)
    engine = StubEngine(thresholds={"exit_time_stop": 10.0, "exit_time_stop_weeks": 3})
    sigs = _call(
        entry_price=10.0, current_price=10.3, current_stop=8.5,
        entry_date=date(2026, 1, 1), today=date(2026, 2, 1),  # 31 days
        engine=engine,
    )
    time_sigs = [s for s in sigs if s.reason == ExitReason.TIME_STOP]
    assert time_sigs, "Time stop should trigger"


def test_time_stop_does_not_trigger_with_sufficient_gain():
    engine = StubEngine(thresholds={"exit_time_stop": 10.0, "exit_time_stop_weeks": 3})
    sigs = _call(
        entry_price=10.0, current_price=11.5,  # 15% gain
        entry_date=date(2026, 1, 1), today=date(2026, 2, 1),
        engine=engine,
    )
    assert not any(s.reason == ExitReason.TIME_STOP for s in sigs)


def test_time_stop_does_not_trigger_before_max_weeks():
    engine = StubEngine(thresholds={"exit_time_stop": 10.0, "exit_time_stop_weeks": 6})
    sigs = _call(
        entry_price=10.0, current_price=10.3,
        entry_date=date(2026, 3, 1), today=date(2026, 3, 20),  # only 19 days
        engine=engine,
    )
    assert not any(s.reason == ExitReason.TIME_STOP for s in sigs)


# ---------------------------------------------------------------------------
# Profit targets
# ---------------------------------------------------------------------------

def test_profit_target_1_partial_exit():
    engine = StubEngine(thresholds={"exit_profit_target_1": 20.0,
                                    "exit_profit_target_1_sell_pct": 50.0})
    sigs = _call(entry_price=10.0, current_price=12.1, engine=engine)  # 21% gain
    t1 = [s for s in sigs if s.reason == ExitReason.PROFIT_TARGET_1]
    assert t1
    assert t1[0].exit_type == "PARTIAL"
    assert t1[0].partial_pct == 50.0


def test_profit_target_2_full_exit():
    engine = StubEngine(thresholds={"exit_profit_target_2": 40.0})
    sigs = _call(entry_price=10.0, current_price=14.5, engine=engine)  # 45% gain
    t2 = [s for s in sigs if s.reason == ExitReason.PROFIT_TARGET_2]
    assert t2
    assert t2[0].exit_type == "FULL"


def test_profit_target_does_not_trigger_below_threshold():
    engine = StubEngine(thresholds={"exit_profit_target_1": 20.0,
                                    "exit_profit_target_1_sell_pct": 50.0,
                                    "exit_profit_target_2": 40.0})
    sigs = _call(entry_price=10.0, current_price=10.5, engine=engine)  # only 5%
    assert not any(s.reason in (ExitReason.PROFIT_TARGET_1, ExitReason.PROFIT_TARGET_2)
                   for s in sigs)


# ---------------------------------------------------------------------------
# Earnings avoidance
# ---------------------------------------------------------------------------

def test_earnings_avoid_triggers_within_buffer():
    engine = StubEngine(thresholds={"exit_earnings_avoid": 3})
    today = date(2026, 4, 1)
    sigs = _call(today=today, next_earnings_date=today + timedelta(days=2),
                 engine=engine)
    assert any(s.reason == ExitReason.EARNINGS_AVOID for s in sigs)


def test_earnings_avoid_does_not_trigger_outside_buffer():
    engine = StubEngine(thresholds={"exit_earnings_avoid": 3})
    today = date(2026, 4, 1)
    sigs = _call(today=today, next_earnings_date=today + timedelta(days=10),
                 engine=engine)
    assert not any(s.reason == ExitReason.EARNINGS_AVOID for s in sigs)


# ---------------------------------------------------------------------------
# Break below 50MA
# ---------------------------------------------------------------------------

def test_break_below_50ma_triggers_on_volume():
    df = _make_df(close=8.8, ma_50=9.0, volume=200_000)  # close < MA50, high volume
    engine = StubEngine()
    sigs = _call(current_price=8.8, current_stop=7.0, df=df, avg_vol_50=100_000.0,
                 engine=engine)
    assert any(s.reason == ExitReason.TRAILING_STOP for s in sigs)


def test_break_below_50ma_no_trigger_on_low_volume():
    df = _make_df(close=8.8, ma_50=9.0, volume=50_000)  # below MA but volume < avg
    engine = StubEngine()
    sigs = _call(current_price=8.8, current_stop=7.0, df=df, avg_vol_50=100_000.0,
                 engine=engine)
    assert not any(s.reason == ExitReason.TRAILING_STOP for s in sigs)


# ---------------------------------------------------------------------------
# Three-weeks-tight overrides weak exits
# ---------------------------------------------------------------------------

def test_three_weeks_tight_suppresses_time_stop():
    engine = StubEngine(thresholds={
        "exit_time_stop": 10.0, "exit_time_stop_weeks": 3,
        "exit_three_weeks_tight": 1.5,
    })
    # Tight weekly closes (within 1.5%) → 3WT should suppress time stop
    tight_closes = [10.10, 10.05, 10.00, 9.95, 9.90]
    sigs = _call(
        entry_price=10.0, current_price=10.1,
        entry_date=date(2026, 1, 1), today=date(2026, 2, 5),  # >15 days
        weekly_closes=tight_closes,
        engine=engine,
    )
    assert not any(s.reason == ExitReason.TIME_STOP for s in sigs)


# ---------------------------------------------------------------------------
# Parabolic move
# ---------------------------------------------------------------------------

def test_parabolic_move_triggers_on_three_up_weeks():
    engine = StubEngine(thresholds={"exit_parabolic_move": 5.0},
                        disabled={"exit_three_weeks_tight"})
    # Three consecutive weeks each up >5% (latest first)
    weekly = [13.0, 12.0, 11.0, 10.0, 9.5]
    # gains: 13/12-1=8.3%, 12/11-1=9.1%, 11/10-1=10%
    sigs = _call(entry_price=9.0, current_price=13.5, weekly_closes=weekly, engine=engine)
    parabolic = [s for s in sigs if "Parabolic" in s.message]
    assert parabolic
