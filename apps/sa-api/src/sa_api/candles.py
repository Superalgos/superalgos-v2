"""Where a provider's candles come from: the replay it was scored against, or Binance's public REST klines."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from sa_api.config import ApiConfig
from sa_core.trackrecord import Candle
from sa_engine.network.replay import (
    ReplayError,
    ReplaySpec,
    fetch_rest_candles,
    load_replay_candles,
)

DAY_MS = 86_400_000


class CandleLoader(Protocol):
    def load(
        self, provider: str, instruments: list[str], first_ms: int, last_ms: int
    ) -> dict[str, list[Candle]]: ...


class CandleSource:
    """Cached candle loading. Raises ``ReplayError`` (with what to do) when none can be had."""

    def __init__(self, cfg: ApiConfig) -> None:
        self.cfg = cfg
        self._replay: dict[tuple[str, int], dict[str, list[Candle]]] = {}
        self._rest: dict[tuple[str, int, int], tuple[float, list[Candle]]] = {}

    def load(
        self, provider: str, instruments: list[str], first_ms: int, last_ms: int
    ) -> dict[str, list[Candle]]:
        spec_path = self.cfg.replay_specs.get(provider)
        if spec_path is not None:
            return self._from_replay(Path(spec_path))
        return {i: self._from_rest(i, first_ms, last_ms) for i in instruments}

    def _from_replay(self, path: Path) -> dict[str, list[Candle]]:
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError as exc:
            raise ReplayError(
                f"replay spec {path} is not readable: {exc.strerror}; has the provider started?"
            ) from exc
        if key not in self._replay:
            spec = ReplaySpec.load(path)
            self._replay[key] = {
                spec.instrument: load_replay_candles(
                    spec, data_dir=self.cfg.data_dir, tolerance_s=self.cfg.tolerance_s
                )
            }
        return self._replay[key]

    def _from_rest(self, instrument: str, first_ms: int, last_ms: int) -> list[Candle]:
        start = (first_ms - 60_000) // 60_000 * 60_000
        end = min(last_ms + 7 * DAY_MS, int(time.time() * 1000)) // 60_000 * 60_000
        key = (instrument, start, end // DAY_MS)
        hit = self._rest.get(key)
        if hit is not None and time.time() - hit[0] < 300:
            return hit[1]
        candles = fetch_rest_candles(instrument.partition(":")[2], start, max(end, start + 60_000))
        self._rest[key] = (time.time(), candles)
        return candles
