"""The order-intent seam: every order a strategy wants to place is described by an :class:`OrderIntent`.

Compiled strategies call ``on_order_intent(intent)`` before submitting; another package (sa-core / the
provider bot) turns the intent into a signed signal. Followers feed received intents to
:class:`sa_engine.paper.FollowerExecutor`.

Sizing semantics (the contract between provider and follower):

* ``action="open"`` / ``"adjust"``: notional = ``size_fraction x capital`` (capital = account equity).
* ``action="close"``: quantity = ``size_fraction x`` current position in that instrument (``1`` = flatten).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Side = Literal["buy", "sell"]
Action = Literal["open", "close", "adjust"]
OrderType = Literal["market", "limit", "stop"]

INSTRUMENT_PATTERN = r"^[A-Z0-9]+:[A-Z0-9]+$"
BACKTEST_SYMBOLS = ("BTCUSDT", "ETHUSDT")  # symbols with a built-in backtest instrument (sa_engine.catalog)


class OrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(pattern=INSTRUMENT_PATTERN, description="VENUE:SYMBOL, e.g. BINANCE:BTCUSDT")
    side: Side
    action: Action
    size_fraction: float = Field(gt=0, le=1)
    order_type: OrderType
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    timeframe: int = Field(gt=0, description="strategy timeframe in seconds")
    ts: int = Field(ge=0, description="emission time, ms since epoch (UTC)")

    @model_validator(mode="after")
    def _prices_match_type(self) -> OrderIntent:
        if self.order_type == "market" and (self.limit_price is not None or self.stop_price is not None):
            raise ValueError("market orders take no limit_price/stop_price")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit orders need limit_price")
        if self.order_type == "stop" and self.stop_price is None:
            raise ValueError("stop orders need stop_price")
        return self


def to_nautilus_id(instrument: str) -> str:
    """``BINANCE:BTCUSDT`` -> ``BTCUSDT.BINANCE``."""
    venue, sep, symbol = instrument.partition(":")
    if not sep or not venue or not symbol:
        raise ValueError(f"instrument {instrument!r} must look like VENUE:SYMBOL, e.g. BINANCE:BTCUSDT")
    return f"{symbol}.{venue}"


def from_nautilus_id(instrument_id: str) -> str:
    """``BTCUSDT.BINANCE`` -> ``BINANCE:BTCUSDT``."""
    symbol, sep, venue = instrument_id.rpartition(".")
    if not sep or not venue or not symbol:
        raise ValueError(f"instrument id {instrument_id!r} must look like SYMBOL.VENUE, e.g. BTCUSDT.BINANCE")
    return f"{venue}:{symbol}"


def normalize_instrument(value: str, default_venue: str = "BINANCE") -> str:
    """Accept ``BTCUSDT``, ``BINANCE:BTCUSDT`` or ``BTCUSDT.BINANCE``; return ``VENUE:SYMBOL``."""
    v = value.strip().upper()
    if ":" in v:
        to_nautilus_id(v)
        return v
    if "." in v:
        return from_nautilus_id(v)
    if not v.isalnum():
        raise ValueError(f"instrument {value!r} is not a symbol like BTCUSDT or VENUE:SYMBOL")
    return f"{default_venue}:{v}"
