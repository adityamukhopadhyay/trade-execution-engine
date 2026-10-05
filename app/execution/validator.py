"""Pure portfolio validation: every rule runs and every problem is collected."""
from __future__ import annotations

from collections import Counter
from typing import TypedDict

from app.core.instruments import InstrumentTable
from app.core.models import (
    FirstTimePayload,
    Holding,
    InstructionAction,
    PortfolioLine,
    RebalanceInstruction,
    RebalancePayload,
    SymbolRef,
)

Key = tuple[str, str]  # (symbol, exchange)


class ValidationIssue(TypedDict):
    symbol: str | None
    message: str


def validate(portfolio: FirstTimePayload | RebalancePayload, holdings: list[Holding],
             instruments: InstrumentTable, *, allow_existing_holdings: bool) -> list[ValidationIssue]:
    held = _held_quantities(holdings)
    refs: list[SymbolRef] = list(portfolio.lines if isinstance(portfolio, FirstTimePayload)
                                 else portfolio.instructions)
    issues = _check_symbols(refs, instruments) + _check_duplicates(refs)
    # holdings rules only for known symbols
    if isinstance(portfolio, FirstTimePayload):
        lines = [line for line in portfolio.lines if line.key in instruments]
        issues += _check_first_time(lines, held, allow_existing_holdings)
    else:
        instructions = [instr for instr in portfolio.instructions if instr.key in instruments]
        issues += _check_rebalance(instructions, held, allow_existing_holdings)
    return issues


def _issue(ref: SymbolRef | None, message: str) -> ValidationIssue:
    return {"symbol": ref.symbol if ref else None, "message": message}


def _held_quantities(holdings: list[Holding]) -> dict[Key, int]:
    return {(h.symbol.upper(), h.exchange): h.quantity for h in holdings}


def _check_symbols(refs: list[SymbolRef], instruments: InstrumentTable) -> list[ValidationIssue]:
    issues = []
    for ref in refs:
        if ref.key not in instruments:
            issues.append(_issue(ref, f"{ref.symbol}/{ref.exchange} is not in the instrument table"))
        elif ref.isin and ref.isin != instruments.get(*ref.key).isin:
            expected = instruments.get(*ref.key).isin
            issues.append(_issue(ref, f"isin {ref.isin} does not match {expected} for {ref.symbol}"))
    return issues


def _check_duplicates(refs: list[SymbolRef]) -> list[ValidationIssue]:
    counts = Counter(ref.key for ref in refs)
    return [_issue(SymbolRef(symbol=symbol, exchange=exchange), f"{symbol}/{exchange} appears {n} times")
            for (symbol, exchange), n in counts.items() if n > 1]


def _check_first_time(lines: list[PortfolioLine], held: dict[Key, int],
                      allow_existing: bool) -> list[ValidationIssue]:
    if allow_existing:
        return []
    issues = []
    if held:
        names = ", ".join(sorted(symbol for symbol, _ in held))
        issues.append(_issue(None, f"account already holds {len(held)} symbol(s) ({names}); first_time "
                                   "expects an empty account -- set allow_existing_holdings to proceed"))
    for line in lines:
        if line.key in held:
            issues.append(_issue(line, f"{line.symbol} is already held ({held[line.key]}); "
                                       "set allow_existing_holdings to buy more"))
    return issues


def _check_rebalance(instructions: list[RebalanceInstruction], held: dict[Key, int],
                     allow_existing: bool) -> list[ValidationIssue]:
    issues = []
    for instr in instructions:
        held_qty = held.get(instr.key)
        if instr.action is InstructionAction.SELL:
            if held_qty is None:
                issues.append(_issue(instr, f"SELL {instr.quantity} {instr.symbol}: not held"))
            elif held_qty < (instr.quantity or 0):
                issues.append(_issue(instr, f"SELL {instr.quantity} {instr.symbol}: only {held_qty} held"))
        elif instr.action is InstructionAction.REBALANCE:
            delta = instr.quantity_delta or 0
            if held_qty is None:
                issues.append(_issue(instr, f"REBALANCE {delta:+d} {instr.symbol}: not held"))
            elif held_qty + delta < 0:
                issues.append(_issue(instr, f"REBALANCE {delta:+d} {instr.symbol} would take the holding "
                                            f"below zero (held {held_qty})"))
        elif held_qty is not None and not allow_existing:
            issues.append(_issue(instr, f"BUY {instr.symbol}: already held ({held_qty}); use REBALANCE "
                                        "or set allow_existing_holdings"))
    return issues
