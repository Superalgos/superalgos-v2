"""The ledger is shared between the main thread and the follower bot's helper thread (Nautilus holds the main one)."""

from __future__ import annotations

import threading
from pathlib import Path

from sa_engine.network.ledger import Ledger


def test_a_ledger_opened_on_one_thread_is_usable_from_many(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.sqlite")
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(25):
                ev = f"{n}-{i}"
                ledger.record(event_id=ev, provider="p", source="relay", received_ms=i, status="accepted")
                assert ledger.get(ev) is not None
                ledger.head("p", "s")
                ledger.rows()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(ledger.rows()) == 100
    ledger.close()
