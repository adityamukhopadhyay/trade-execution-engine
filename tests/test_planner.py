"""build_plan(): action -> side mapping, SELLs before BUYs, global seq, tags, isin, market-hours warning."""
from __future__ import annotations

import re
from datetime import UTC, datetime

from app.core.models import InstructionAction, OrderSide, Phase
from app.execution.planner import build_plan, make_tag
from tests.fakes import buy, first_time, holding, rebalance, rebalance_by, sell

TAG = re.compile(r"^kp[0-9a-f]{8}\d{3}$")


def test_first_time_is_all_buys(make_plan):
    plan = make_plan(first_time(("INFY", 10), ("TCS", 5)))
    assert plan.sells == []
    assert [(o.symbol, o.side, o.quantity, o.phase) for o in plan.buys] == [
        ("INFY", OrderSide.BUY, 10, Phase.BUY), ("TCS", OrderSide.BUY, 5, Phase.BUY)]
    assert {o.source_action for o in plan.buys} == {InstructionAction.BUY}


def test_rebalance_sign_split_and_routing(make_plan):
    plan = make_plan(rebalance(buy("HDFCBANK", 6), rebalance_by("INFY", -4), sell("TCS", 5),
                               rebalance_by("RELIANCE", 2)))
    assert [(o.symbol, o.side, o.quantity) for o in plan.sells] == [("INFY", OrderSide.SELL, 4),
                                                                    ("TCS", OrderSide.SELL, 5)]
    assert [(o.symbol, o.side, o.quantity) for o in plan.buys] == [("HDFCBANK", OrderSide.BUY, 6),
                                                                   ("RELIANCE", OrderSide.BUY, 2)]
    assert [o.source_action for o in plan.orders] == [InstructionAction.REBALANCE, InstructionAction.SELL,
                                                      InstructionAction.BUY, InstructionAction.REBALANCE]


def test_sells_precede_buys_with_one_global_seq(make_plan):
    plan = make_plan(rebalance(buy("HDFCBANK", 1), sell("TCS", 1), buy("SBIN", 1), sell("INFY", 1)))
    assert [o.seq for o in plan.orders] == [1, 2, 3, 4]
    assert [o.phase for o in plan.orders] == [Phase.SELL, Phase.SELL, Phase.BUY, Phase.BUY]


def test_tags_are_short_unique_and_derived_from_run_id(make_plan):
    plan = make_plan(first_time(("INFY", 1), ("TCS", 1), ("SBIN", 1)))
    tags = [o.tag for o in plan.orders]
    assert all(TAG.match(t) and len(t) == 13 for t in tags)
    assert len(set(tags)) == 3
    assert tags[0] == make_tag(plan.run_id, 1) == f"kp{plan.run_id[:8]}001"


def test_isin_filled_from_table(make_plan):
    plan = make_plan(first_time(("RELIANCE", 1)))
    assert plan.orders[0].isin == "INE002A01018"


def test_mode_session_and_holdings_preserved(make_plan, session):
    held = [holding("INFY", 10)]
    plan = make_plan(rebalance(rebalance_by("INFY", -4)), held)
    assert (plan.mode, plan.session_id, plan.broker) == ("rebalance", session.session_id, "scripted")
    assert plan.holdings_before == held


def test_market_warning_appended_outside_hours(session, instruments):
    saturday = datetime(2026, 10, 3, 4, 30, tzinfo=UTC)
    plan = build_plan("abcdef1234", session, first_time(("INFY", 1)), [], instruments, now=saturday)
    assert len(plan.warnings) == 1 and "Saturday" in plan.warnings[0]
    silent = build_plan("abcdef1234", session, first_time(("INFY", 1)), [], instruments, now=saturday,
                        market_hours_warn=False)
    assert silent.warnings == []
