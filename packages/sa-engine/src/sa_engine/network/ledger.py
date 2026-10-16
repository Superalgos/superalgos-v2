"""The follower's local ledger: every signal received, when, what the checks said, and the paper fill.

SQLite (standard library), one row per signal event. The chain heads it keeps (last accepted seq and
chain reference per provider and strategy) make chain-continuity checks survive a restart.
"""

from __future__ import annotations

import sqlite3
import weakref
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sa_engine.network.executor import Execution

Status = Literal["accepted", "rejected", "pending"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  event_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  strategy_id TEXT,
  seq INTEGER,
  ref TEXT,
  prev TEXT,
  source TEXT NOT NULL,
  received_ms INTEGER NOT NULL,
  created_ms INTEGER,
  status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  signal_json TEXT,
  anchor_status TEXT NOT NULL DEFAULT 'pending',
  anchored_s INTEGER,
  placed INTEGER NOT NULL DEFAULT 0,
  filled INTEGER NOT NULL DEFAULT 0,
  qty TEXT,
  price REAL,
  fee REAL,
  filled_ms INTEGER,
  equity_before REAL,
  ref_price REAL,
  exec_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS signals_chain ON signals (provider, strategy_id, seq);
"""


class LedgerRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    provider: str
    strategy_id: str | None
    seq: int | None
    ref: str | None
    prev: str | None
    source: str
    received_ms: int
    created_ms: int | None
    status: str
    reason: str
    signal_json: str | None
    anchor_status: str
    anchored_s: int | None
    placed: bool
    filled: bool
    qty: str | None
    price: float | None
    fee: float | None
    filled_ms: int | None
    equity_before: float | None
    ref_price: float | None
    exec_reason: str


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._finalizer = weakref.finalize(self, self._db.close)  # no leaked handle if close() is forgotten

    def close(self) -> None:
        self._finalizer()

    # ------------------------------------------------------------------ writes
    def record(
        self,
        *,
        event_id: str,
        provider: str,
        source: str,
        received_ms: int,
        status: Status,
        reason: str = "",
        strategy_id: str | None = None,
        seq: int | None = None,
        ref: str | None = None,
        prev: str | None = None,
        created_ms: int | None = None,
        signal_json: str | None = None,
    ) -> None:
        """Insert or replace the row of ``event_id`` (a pending row becomes accepted or rejected)."""
        self._db.execute(
            "INSERT INTO signals (event_id, provider, strategy_id, seq, ref, prev, source, received_ms, created_ms,"
            " status, reason, signal_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(event_id) DO UPDATE SET strategy_id=excluded.strategy_id, seq=excluded.seq,"
            " ref=excluded.ref, prev=excluded.prev, status=excluded.status, reason=excluded.reason,"
            " created_ms=excluded.created_ms, signal_json=excluded.signal_json",
            (
                event_id,
                provider,
                strategy_id,
                seq,
                ref,
                prev,
                source,
                received_ms,
                created_ms,
                status,
                reason,
                signal_json,
            ),
        )
        self._db.commit()

    def set_execution(self, ex: Execution) -> None:
        self._db.execute(
            "UPDATE signals SET placed=?, filled=?, qty=COALESCE(?, qty), price=COALESCE(?, price),"
            " fee=COALESCE(?, fee), filled_ms=COALESCE(?, filled_ms), equity_before=COALESCE(?, equity_before),"
            " ref_price=COALESCE(?, ref_price), exec_reason=? WHERE event_id=?",
            (
                int(ex.placed),
                int(ex.filled),
                ex.qty,
                ex.price,
                ex.fee,
                ex.ts_ms,
                ex.equity_before,
                ex.ref_price,
                ex.reason,
                ex.ref,
            ),
        )
        self._db.commit()

    def set_anchor(self, ref: str, status: str, anchored_s: int | None) -> None:
        self._db.execute("UPDATE signals SET anchor_status=?, anchored_s=? WHERE ref=?", (status, anchored_s, ref))
        self._db.commit()

    # ------------------------------------------------------------------ reads
    def _rows(self, sql: str, args: tuple[object, ...] = ()) -> Iterator[LedgerRow]:
        with closing(self._db.execute(sql, args)) as cur:
            for r in cur:
                d = dict(r)
                d["placed"], d["filled"] = bool(d["placed"]), bool(d["filled"])
                yield LedgerRow(**d)

    def rows(self, status: str | None = None) -> list[LedgerRow]:
        if status is None:
            return list(self._rows("SELECT * FROM signals ORDER BY received_ms, event_id"))
        return list(self._rows("SELECT * FROM signals WHERE status=? ORDER BY received_ms, event_id", (status,)))

    def get(self, event_id: str) -> LedgerRow | None:
        return next(self._rows("SELECT * FROM signals WHERE event_id=?", (event_id,)), None)

    def by_ref(self, ref: str) -> list[LedgerRow]:
        return list(self._rows("SELECT * FROM signals WHERE ref=?", (ref,)))

    def head(self, provider: str, strategy_id: str) -> tuple[int, str] | None:
        """(seq, ref) where this chain stands: the newest accepted signal, or one rejected for a gap
        (a gap rejects the signal but still moves the head, so later signals verify against it)."""
        row = self._db.execute(
            "SELECT seq, ref FROM signals WHERE provider=? AND strategy_id=? AND ref IS NOT NULL"
            " AND (status='accepted' OR reason LIKE 'gap:%') ORDER BY seq DESC LIMIT 1",
            (provider, strategy_id),
        ).fetchone()
        return None if row is None else (int(row["seq"]), str(row["ref"]))
