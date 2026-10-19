"""The follower's disaster stop settings (SEP-14.5). Pure: importable without Nautilus (the CLI reads the profile)."""

from __future__ import annotations

from decimal import Decimal

DISASTER_STOP_MAX_PCT = 50.0
# The resting stop-limit's limit sits this far below its trigger. A stop-limit that triggers in a fast fall only
# fills at or above its limit: 1% leaves room for the gap between trigger and fill, yet bounds the worst price paid.
DISASTER_LIMIT_OFFSET = Decimal("0.01")


def check_disaster_stop_pct(pct: float) -> float:
    """``pct`` is 0 (off) or in (0, 50]; anything else is refused."""
    if pct == 0 or 0 < pct <= DISASTER_STOP_MAX_PCT:
        return pct
    raise ValueError(
        f"disaster stop percent {pct!r} is outside (0, {DISASTER_STOP_MAX_PCT:g}]; "
        "use a value such as 10, or 0 to turn it off"
    )
