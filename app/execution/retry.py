"""Backoff for order placements the broker refused (429) or never received (connect error)."""
from __future__ import annotations

import random


def backoff_delay(attempt: int, retry_after: float | None, *, base: float, cap: float) -> float:
    """Seconds to sleep after failed attempt `attempt` (1-based): Retry-After (capped) when given, else capped
    exponential backoff with jitter."""
    if retry_after is not None:
        return min(cap, max(0.0, retry_after))
    return min(cap, base * 2 ** (attempt - 1)) + random.uniform(0, base)
