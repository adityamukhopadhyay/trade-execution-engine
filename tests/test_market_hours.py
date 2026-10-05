"""market_hours_warning(): weekday + 09:15-15:30 IST only; naive input is UTC."""
from __future__ import annotations

from datetime import UTC, datetime

from app.execution.market_hours import IST, market_hours_warning


def ist(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=IST)


def test_tuesday_mid_session_is_fine():
    assert market_hours_warning(ist(2026, 10, 6, 10, 0)) is None


def test_edges_outside_session_warn():
    assert "09:14" in (market_hours_warning(ist(2026, 10, 6, 9, 14)) or "")
    assert "15:31" in (market_hours_warning(ist(2026, 10, 6, 15, 31)) or "")
    assert market_hours_warning(ist(2026, 10, 6, 9, 15)) is None
    assert market_hours_warning(ist(2026, 10, 6, 15, 30)) is None


def test_weekend_warns_by_day_name():
    assert "Saturday" in (market_hours_warning(ist(2026, 10, 3, 11, 0)) or "")
    assert "Sunday" in (market_hours_warning(ist(2026, 10, 4, 11, 0)) or "")


def test_naive_and_utc_inputs_are_treated_as_utc():
    assert market_hours_warning(datetime(2026, 10, 6, 4, 30)) is None  # 10:00 IST
    assert market_hours_warning(datetime(2026, 10, 6, 4, 30, tzinfo=UTC)) is None
    assert market_hours_warning(datetime(2026, 10, 6, 12, 0, tzinfo=UTC)) is not None  # 17:30 IST
