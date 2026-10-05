"""Turns a validated portfolio into the broker orders to send, SELLs first so their proceeds fund the BUYs."""
from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from app.core.instruments import InstrumentTable
from app.core.models import (
    BrokerSession,
    ExecutionPlan,
    FirstTimePayload,
    Holding,
    InstructionAction,
    OrderSide,
    Phase,
    PlannedOrder,
    RebalanceInstruction,
    RebalancePayload,
    SymbolRef,
)
from app.execution.market_hours import market_hours_warning


class _Leg(NamedTuple):
    ref: SymbolRef
    side: OrderSide
    quantity: int
    source_action: InstructionAction


def make_tag(run_id: str, seq: int) -> str:
    return f"kp{run_id[:8]}{seq:03d}"


def build_plan(run_id: str, session: BrokerSession, portfolio: FirstTimePayload | RebalancePayload,
               holdings: list[Holding], instruments: InstrumentTable, *, now: datetime | None = None,
               market_hours_warn: bool = True) -> ExecutionPlan:
    legs = _legs(portfolio)
    sells = [leg for leg in legs if leg.side is OrderSide.SELL]
    buys = [leg for leg in legs if leg.side is OrderSide.BUY]
    planned = [_planned_order(run_id, seq, leg, instruments)
               for seq, leg in enumerate([*sells, *buys], start=1)]
    warning = market_hours_warning(now) if market_hours_warn else None
    return ExecutionPlan(
        run_id=run_id, session_id=session.session_id, broker=session.broker, mode=portfolio.mode,
        sells=planned[:len(sells)], buys=planned[len(sells):],
        warnings=[warning] if warning else [], holdings_before=list(holdings),
    )


def _legs(portfolio: FirstTimePayload | RebalancePayload) -> list[_Leg]:
    if isinstance(portfolio, FirstTimePayload):
        return [_Leg(line, OrderSide.BUY, line.quantity, InstructionAction.BUY) for line in portfolio.lines]
    return [_leg_for(instruction) for instruction in portfolio.instructions]


def _leg_for(instr: RebalanceInstruction) -> _Leg:
    if instr.action is InstructionAction.REBALANCE:
        delta = instr.quantity_delta or 0  # validator guarantees non-zero
        return _Leg(instr, OrderSide.SELL if delta < 0 else OrderSide.BUY, abs(delta), instr.action)
    side = OrderSide.SELL if instr.action is InstructionAction.SELL else OrderSide.BUY
    return _Leg(instr, side, instr.quantity or 0, instr.action)  # validator guarantees a quantity


def _planned_order(run_id: str, seq: int, leg: _Leg, instruments: InstrumentTable) -> PlannedOrder:
    return PlannedOrder(
        tag=make_tag(run_id, seq), symbol=leg.ref.symbol, exchange=leg.ref.exchange,
        isin=instruments.get(leg.ref.symbol, leg.ref.exchange).isin, side=leg.side, quantity=leg.quantity,
        seq=seq, phase=Phase.SELL if leg.side is OrderSide.SELL else Phase.BUY,
        source_action=leg.source_action,
    )
