"""Whether "now" falls inside NSE/BSE regular trading hours (Mon-Fri 09:15-15:30 IST)."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone

from app.core.models import utcnow

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def market_hours_warning(now: datetime | None = None) -> str | None:
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(IST)
    if local.weekday() >= 5:
        reason = f"it is {local:%A}"
    elif not MARKET_OPEN <= local.time() <= MARKET_CLOSE:
        reason = f"it is {local:%H:%M} IST"
    else:
        return None
    return (f"outside NSE/BSE regular hours (Mon-Fri 09:15-15:30 IST): {reason}. MARKET orders may be "
            "rejected or queue until the next session. Exchange holidays are not checked.")
