"""The follower's local ledger: every signal received, when, what the checks said, and the paper fill.

SQLite (standard library), one row per signal event. The chain heads it keeps (last accepted seq and
chain reference per provider and strategy) make chain-continuity checks survive a restart.
"""

from __future__ import annotations

import sqlite3
import threading
import weakref
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
  exec_reason TEXT NOT NULL DEFAULT '',
  venue_order_id TEXT,
  queued INTEGER NOT NULL DEFAULT 0
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
    venue_order_id: str | None = None  # the exchange's order id (testnet executor)
    queued: bool = False  # waiting to reach the exchange: not placed yet, not dropped


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # The ledger is shared between threads: with a Nautilus executor the bot's loop runs in a helper thread while
        # the main thread opens the file and flushes it at exit. One lock serialises every statement.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(signals)")}
        if "venue_order_id" not in columns:  # ledger created before the testnet executor existed
            self._db.execute("ALTER TABLE signals ADD COLUMN venue_order_id TEXT")
        if "queued" not in columns:  # ledger created before a queued close was told apart from a refused one
            self._db.execute("ALTER TABLE signals ADD COLUMN queued INTEGER NOT NULL DEFAULT 0")
        self._db.commit()
        self._finalizer = weakref.finalize(self, self._db.close)  # no leaked handle if close() is forgotten

    def close(self) -> None:
        with self._lock:
            self._finalizer()

    # ------------------------------------------------------------------ writes
    def _write(self, sql: str, args: tuple[object, ...]) -> None:
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

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
        self._write(
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

    def discard(self, event_id: str) -> None:
        """Delete the row of ``event_id`` (one recorded from an event that failed verification)."""
        self._write("DELETE FROM signals WHERE event_id=?", (event_id,))

    def set_execution(self, ex: Execution) -> None:
        self._write(
            "UPDATE signals SET placed=?, filled=?, qty=COALESCE(?, qty), price=COALESCE(?, price),"
            " fee=COALESCE(?, fee), filled_ms=COALESCE(?, filled_ms), equity_before=COALESCE(?, equity_before),"
            " ref_price=COALESCE(?, ref_price), exec_reason=?,"
            " venue_order_id=COALESCE(?, venue_order_id), queued=? WHERE event_id=?",
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
                ex.venue_order_id,
                int(ex.queued),
                ex.ref,
            ),
        )

    def set_anchor(self, ref: str, status: str, anchored_s: int | None) -> None:
        self._write("UPDATE signals SET anchor_status=?, anchored_s=? WHERE ref=?", (status, anchored_s, ref))

    # ------------------------------------------------------------------ reads
    def _rows(self, sql: str, args: tuple[object, ...] = ()) -> list[LedgerRow]:
        with self._lock, closing(self._db.execute(sql, args)) as cur:
            fetched = [dict(r) for r in cur]
        for d in fetched:
            d["placed"], d["filled"], d["queued"] = bool(d["placed"]), bool(d["filled"]), bool(d["queued"])
        return [LedgerRow(**d) for d in fetched]

    def rows(self, status: str | None = None) -> list[LedgerRow]:
        if status is None:
            return self._rows("SELECT * FROM signals ORDER BY received_ms, event_id")
        return self._rows("SELECT * FROM signals WHERE status=? ORDER BY received_ms, event_id", (status,))

    def get(self, event_id: str) -> LedgerRow | None:
        found = self._rows("SELECT * FROM signals WHERE event_id=?", (event_id,))
        return found[0] if found else None

    def by_ref(self, ref: str) -> list[LedgerRow]:
        return self._rows("SELECT * FROM signals WHERE ref=?", (ref,))

    def head(self, provider: str, strategy_id: str) -> tuple[int, str] | None:
        """(seq, ref) where this chain stands: the newest accepted signal, or one rejected for a gap
        (a gap rejects the signal but still moves the head, so later signals verify against it)."""
        with self._lock:
            row = self._db.execute(
                "SELECT seq, ref FROM signals WHERE provider=? AND strategy_id=? AND ref IS NOT NULL"
                " AND (status='accepted' OR reason LIKE 'gap:%') ORDER BY seq DESC LIMIT 1",
                (provider, strategy_id),
            ).fetchone()
        return None if row is None else (int(row["seq"]), str(row["ref"]))
