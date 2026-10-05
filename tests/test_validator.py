"""validate(): every rule, every issue collected, nothing short-circuits."""
from __future__ import annotations

from app.execution.validator import validate
from tests.fakes import buy, first_time, holding, rebalance, rebalance_by, sell


def messages(issues) -> list[str]:
    return [issue["message"] for issue in issues]


def test_unknown_symbol(instruments):
    issues = validate(first_time(("NOPE", 1)), [], instruments, allow_existing_holdings=False)
    assert [i["symbol"] for i in issues] == ["NOPE"]
    assert "not in the instrument table" in issues[0]["message"]


def test_isin_mismatch(instruments):
    portfolio = rebalance(buy("INFY", 1, isin="INE467B01029"))  # TCS's isin on INFY
    issues = validate(portfolio, [], instruments, allow_existing_holdings=False)
    assert len(issues) == 1 and "does not match" in issues[0]["message"]


def test_duplicate_symbol_exchange(instruments):
    issues = validate(first_time(("INFY", 1), ("INFY", 2)), [], instruments, allow_existing_holdings=False)
    assert messages(issues) == ["INFY/NSE appears 2 times"]


def test_first_time_with_holdings_is_an_error_unless_allowed(instruments):
    held = [holding("SBIN", 3)]
    issues = validate(first_time(("INFY", 1)), held, instruments, allow_existing_holdings=False)
    assert len(issues) == 1 and issues[0]["symbol"] is None and "SBIN" in issues[0]["message"]
    assert validate(first_time(("INFY", 1)), held, instruments, allow_existing_holdings=True) == []


def test_first_time_symbol_already_held(instruments):
    issues = validate(first_time(("INFY", 1)), [holding("INFY", 5)], instruments,
                      allow_existing_holdings=False)
    assert [i["symbol"] for i in issues] == [None, "INFY"]  # account non-empty + this symbol held


def test_sell_more_than_held(instruments):
    issues = validate(rebalance(sell("INFY", 10)), [holding("INFY", 4)], instruments,
                      allow_existing_holdings=False)
    assert messages(issues) == ["SELL 10 INFY: only 4 held"]


def test_sell_not_held(instruments):
    issues = validate(rebalance(sell("INFY", 1)), [], instruments, allow_existing_holdings=False)
    assert messages(issues) == ["SELL 1 INFY: not held"]


def test_rebalance_not_held(instruments):
    issues = validate(rebalance(rebalance_by("INFY", 2)), [], instruments, allow_existing_holdings=False)
    assert messages(issues) == ["REBALANCE +2 INFY: not held"]


def test_rebalance_below_zero(instruments):
    issues = validate(rebalance(rebalance_by("INFY", -5)), [holding("INFY", 4)], instruments,
                      allow_existing_holdings=False)
    assert "below zero" in issues[0]["message"] and "-5" in issues[0]["message"]


def test_rebalance_exactly_to_zero_is_fine(instruments):
    assert validate(rebalance(rebalance_by("INFY", -4)), [holding("INFY", 4)], instruments,
                    allow_existing_holdings=False) == []


def test_buy_already_held(instruments):
    held = [holding("INFY", 4)]
    issues = validate(rebalance(buy("INFY", 1)), held, instruments, allow_existing_holdings=False)
    assert "already held" in issues[0]["message"]
    assert validate(rebalance(buy("INFY", 1)), held, instruments, allow_existing_holdings=True) == []


def test_three_faults_give_three_issues(instruments):
    portfolio = rebalance(sell("NOPE", 1), sell("INFY", 99), buy("TCS", 1))
    issues = validate(portfolio, [holding("INFY", 1), holding("TCS", 1)], instruments,
                      allow_existing_holdings=False)
    assert [i["symbol"] for i in issues] == ["NOPE", "INFY", "TCS"]


def test_clean_payloads(instruments):
    assert validate(first_time(("INFY", 1), ("TCS", 2)), [], instruments, allow_existing_holdings=False) == []
    held = [holding("INFY", 10), holding("TCS", 5)]
    clean = rebalance(sell("TCS", 5), rebalance_by("INFY", -4), buy("HDFCBANK", 6, isin="INE040A01034"))
    assert validate(clean, held, instruments, allow_existing_holdings=False) == []
