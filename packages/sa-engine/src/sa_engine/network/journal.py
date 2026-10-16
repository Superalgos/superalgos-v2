"""A provider's durable state: what it signed, what the relay acknowledged, and the anchor proofs.

Layout of a state directory (all append-only except the small JSON files)::

    events.jsonl   every event the provider signed, written (and fsynced) BEFORE it is sent
    acked.jsonl    ids of events a relay accepted
    proofs.jsonl   anchor proofs, one per line
    ruleset.json   the provider's signed rule set
    replay.json    the replay specification of the current run (replay mode only)

The seq/prev chain is derived from ``events.jsonl``, so a crash between signing and sending can never
fork it: on restart the provider continues from the last journaled link and re-sends what was not acked.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sa_core.models import ANCHOR_KIND, COMMITMENT_KIND, SIGNAL_KIND, AnchorProof, NostrEvent, ProviderRuleSet


class JournalError(Exception):
    """The state directory is damaged or unwritable. The message names the file and the fix."""


class JournalEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event: NostrEvent
    strategy_id: str | None = None  # set on the entry that advances a chain
    seq: int | None = None
    ref: str | None = None  # chain reference: signal id (public) or commitment id (private)
    plaintext: str | None = None  # private signals only: kept so the provider can reveal later
    run: str | None = None  # identifies a replay, so a restarted replay skips what it already signed
    intent_index: int | None = None


@dataclass(frozen=True)
class ChainHead:
    seq: int
    ref: str


class ProviderStore:
    """Thread-safe (a live strategy signs from the Nautilus thread, the relay loop acks from another)."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._entries: list[JournalEntry] = []
        self._acked: set[str] = set()
        self._proofs: list[AnchorProof] = []
        self._heads: dict[str, ChainHead] = {}
        self._load()

    # ------------------------------------------------------------------ files
    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def ruleset_path(self) -> Path:
        return self.dir / "ruleset.json"

    @property
    def replay_path(self) -> Path:
        return self.dir / "replay.json"

    def _lines(self, path: Path) -> Iterator[tuple[int, str]]:
        if not path.exists():
            return
        with path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if line.strip():
                    yield n, line

    def _load(self) -> None:
        for n, line in self._lines(self.events_path):
            try:
                entry = JournalEntry.model_validate_json(line)
            except ValueError as exc:
                raise JournalError(
                    f"{self.events_path}:{n} is not a journal entry ({exc}); a crash can cut the last line: "
                    "remove only that line, never edit earlier ones"
                ) from exc
            self._index(entry)
            self._entries.append(entry)
        for _n, line in self._lines(self.dir / "acked.jsonl"):
            self._acked.add(line.strip())
        for n, line in self._lines(self.dir / "proofs.jsonl"):
            try:
                self._proofs.append(AnchorProof.model_validate_json(line))
            except ValueError as exc:
                raise JournalError(f"{self.dir / 'proofs.jsonl'}:{n} is not an anchor proof: {exc}") from exc

    def _index(self, entry: JournalEntry) -> None:
        if entry.strategy_id is not None and entry.seq is not None and entry.ref is not None:
            head = self._heads.get(entry.strategy_id)
            if head is not None and entry.seq != head.seq + 1:
                raise JournalError(
                    f"journal for strategy {entry.strategy_id!r} jumps from seq {head.seq} to {entry.seq}; "
                    f"the chain in {self.events_path} is damaged, restore it from a backup"
                )
            self._heads[entry.strategy_id] = ChainHead(entry.seq, entry.ref)

    def _append(self, path: Path, text: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # ------------------------------------------------------------------ journal
    def head(self, strategy_id: str) -> ChainHead | None:
        with self._lock:
            return self._heads.get(strategy_id)

    def append(self, *entries: JournalEntry) -> None:
        """Durably record entries (in order) before anything is sent."""
        with self._lock:
            for entry in entries:
                self._index(entry)
                self._append(self.events_path, entry.model_dump_json())
                self._entries.append(entry)

    def entries(self) -> list[JournalEntry]:
        with self._lock:
            return list(self._entries)

    def mark_acked(self, event_id: str) -> None:
        with self._lock:
            if event_id not in self._acked:
                self._append(self.dir / "acked.jsonl", event_id)
                self._acked.add(event_id)

    def unacked(self) -> list[NostrEvent]:
        with self._lock:
            return [e.event for e in self._entries if e.event.id not in self._acked]

    def acked_count(self) -> int:
        with self._lock:
            return len(self._acked)

    def progress(self, run: str) -> int:
        """How many intents of replay ``run`` were already signed (so a restart does not repeat them)."""
        with self._lock:
            done = [e.intent_index for e in self._entries if e.run == run and e.intent_index is not None]
            return max(done) + 1 if done else 0

    # ------------------------------------------------------------------ proofs
    def add_proof(self, proof: AnchorProof) -> None:
        with self._lock:
            self._append(self.dir / "proofs.jsonl", proof.model_dump_json())
            self._proofs.append(proof)

    def proofs(self) -> list[AnchorProof]:
        with self._lock:
            return list(self._proofs)

    def unanchored_refs(self) -> list[str]:
        """Chain refs (signal or commitment ids) that have no proof yet, in journal order."""
        with self._lock:
            have = {p.digest for p in self._proofs}
            return [
                e.event.id
                for e in self._entries
                if e.ref is not None and e.event.id == e.ref and e.event.id not in have
            ]

    # ------------------------------------------------------------------ rule set
    def save_ruleset(self, rules: ProviderRuleSet) -> None:
        self.ruleset_path.write_text(rules.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def load_ruleset(self) -> ProviderRuleSet:
        try:
            return ProviderRuleSet.model_validate_json(self.ruleset_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise JournalError(
                f"{self.ruleset_path} not found; run `sa provider register` first to sign and publish a rule set"
            ) from exc
        except ValueError as exc:
            raise JournalError(f"{self.ruleset_path} is not a rule set: {exc}") from exc

    # ------------------------------------------------------------------ stats
    def counts(self) -> dict[str, int]:
        with self._lock:
            kinds = [e.event.kind for e in self._entries]
            return {
                "signals": kinds.count(SIGNAL_KIND),
                "commitments": kinds.count(COMMITMENT_KIND),
                "attestations": kinds.count(ANCHOR_KIND),
                "acked": len(self._acked),
                "proofs": len(self._proofs),
            }

    def dump(self) -> str:
        return json.dumps(self.counts(), sort_keys=True)


__all__ = ["ChainHead", "JournalEntry", "JournalError", "ProviderStore"]
