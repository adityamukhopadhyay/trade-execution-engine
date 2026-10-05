"""Correlation ids shared by the engine (which sets them) and the JSON logger (which reads them)."""
from contextvars import ContextVar

run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
order_id_var: ContextVar[str | None] = ContextVar("order_id", default=None)
