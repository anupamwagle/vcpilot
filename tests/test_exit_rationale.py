"""
Guards that every ExitReason has a user-facing plain-English rationale.

This is what makes the "why was this trade closed?" note in the Closed Trades
view complete and future-proof: if someone adds a new ExitReason without a
rationale entry, this test fails loudly rather than shipping a blank note.
"""
from app.models.trade import (
    ExitReason,
    EXIT_REASON_RATIONALE,
    exit_reason_rationale,
)


def test_every_exit_reason_has_rationale():
    missing = [r.value for r in ExitReason if r.value not in EXIT_REASON_RATIONALE]
    assert not missing, f"ExitReason(s) without a rationale: {missing}"


def test_rationale_entries_are_non_empty_and_well_formed():
    for r in ExitReason:
        entry = EXIT_REASON_RATIONALE[r.value]
        assert set(entry.keys()) >= {"summary", "detail"}, f"{r.value} missing keys"
        assert entry["summary"].strip(), f"{r.value} has empty summary"
        assert len(entry["detail"].strip()) >= 40, f"{r.value} detail too short to be useful"


def test_helper_accepts_enum_value_and_raw_string():
    expected = EXIT_REASON_RATIONALE["TIME_STOP"]
    assert exit_reason_rationale(ExitReason.TIME_STOP) == expected
    assert exit_reason_rationale("TIME_STOP") == expected
    assert exit_reason_rationale("ExitReason.TIME_STOP") == expected


def test_helper_is_safe_for_unknown_and_none():
    assert exit_reason_rationale(None) == {"summary": "", "detail": ""}
    assert exit_reason_rationale("NOT_A_REAL_REASON") == {"summary": "", "detail": ""}
