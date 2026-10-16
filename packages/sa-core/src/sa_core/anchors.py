"""Time anchors: proof that a 32-byte digest (normally a Nostr event id) existed by some time.

`Anchor.stamp(digest) -> AnchorProof`, `Anchor.verify(proof, digest) -> TimeBound(not_after, kind)`.
`not_after` is an upper bound on when the digest existed: a late poster can only produce a later
bound, never an earlier one, so backdating does not work. The block-hash "not before" idea is
deliberately absent: it proves nothing about publication time.

Implementations:
- `OtsAnchor`: OpenTimestamps (Bitcoin). Free, hours of slack. For votes and allocations.
- `TsaAnchor` (module `sa_core.tsa`): RFC 3161 tokens from k organisations over a Merkle root.
  Free, seconds of slack, offline verification. For signals.
- `BscCommitAnchor`: a Merkle root in plain-transaction calldata on BSC, checked through a
  `ChainReader`. Seconds of slack. For signals. `SimChain` is the simulated chain for tests;
  nothing in this package sends a real transaction.

Anchored time of an event = the earliest `not_after` over the proofs that verify (`anchored_time`).
No verifying proof -> `None`: the event is unanchored and must never count as on time.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.op import OpAppend, OpSHA256
from opentimestamps.core.serialize import (
    BytesDeserializationContext,
    BytesSerializationContext,
    StreamDeserializationContext,
)
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
from pydantic import BaseModel, ConfigDict, Field

from .models import AnchorProof


class AnchorError(Exception):
    """A proof does not establish a time bound. The message says why and what to do."""


class TimeBound(BaseModel):
    model_config = ConfigDict(frozen=True)

    not_after: int = Field(ge=0)  # unix seconds: the digest existed no later than this
    kind: str


@runtime_checkable
class Anchor(Protocol):
    kind: str

    def stamp(self, digest: bytes) -> AnchorProof: ...

    def verify(self, proof: AnchorProof, digest: bytes) -> TimeBound: ...


def _check_digest(proof: AnchorProof, digest: bytes) -> None:
    if len(digest) != 32:
        raise AnchorError(f"digest must be 32 bytes, got {len(digest)}")
    if proof.digest != digest.hex():
        raise AnchorError(
            f"proof stamps {proof.digest[:12]}.., not this digest {digest.hex()[:12]}.."
        )


# ---------------------------------------------------------------- Merkle batches


def _h(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def leaf_hash(digest: bytes) -> bytes:
    return _h(b"\x00" + digest)


def node_hash(left: bytes, right: bytes) -> bytes:
    return _h(b"\x01" + left + right)


class PathStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    sibling: str
    left: bool  # sibling sits to the left


class MerkleTree:
    """Binary tree over digests; domain-separated leaves; an odd node is promoted unchanged."""

    def __init__(self, digests: Sequence[bytes]) -> None:
        if not digests:
            raise AnchorError("cannot build a Merkle tree from 0 digests; add at least one")
        bad = [d for d in digests if len(d) != 32]
        if bad:
            raise AnchorError(f"every digest must be 32 bytes; got lengths {[len(d) for d in bad]}")
        self.digests = list(digests)
        level = [leaf_hash(d) for d in digests]
        self.levels = [level]
        while len(level) > 1:
            level = [
                node_hash(level[i], level[i + 1]) if i + 1 < len(level) else level[i]
                for i in range(0, len(level), 2)
            ]
            self.levels.append(level)

    @property
    def root(self) -> bytes:
        return self.levels[-1][0]

    def path(self, index: int) -> list[PathStep]:
        if not 0 <= index < len(self.digests):
            raise AnchorError(f"leaf index {index} outside 0..{len(self.digests) - 1}")
        out: list[PathStep] = []
        i = index
        for level in self.levels[:-1]:
            sib = i ^ 1
            if sib < len(level):
                out.append(PathStep(sibling=level[sib].hex(), left=sib < i))
            i //= 2
        return out


def root_from_path(digest: bytes, path: Iterable[PathStep]) -> bytes:
    cur = leaf_hash(digest)
    for step in path:
        sib = bytes.fromhex(step.sibling)
        cur = node_hash(sib, cur) if step.left else node_hash(cur, sib)
    return cur


# ---------------------------------------------------------------- chain interface


@dataclass(frozen=True)
class TxInfo:
    hash: str
    data: bytes  # calldata
    block_number: int | None
    block_hash: str | None


@dataclass(frozen=True)
class BlockInfo:
    number: int
    hash: str
    timestamp: int
    tx_hashes: tuple[str, ...]


class ChainReader(Protocol):
    def head(self) -> int: ...
    def finalized(self) -> int | None: ...
    def block(self, number: int) -> BlockInfo | None: ...
    def tx(self, tx_hash: str) -> TxInfo | None: ...


class Committer(Protocol):
    def commit(self, data: bytes) -> str:
        """Queue a calldata-only transaction; return its hash."""
        ...


class SimChain:
    """Simulated EVM chain with an injectable clock, finality depth, and reorgs (tests, demo)."""

    def __init__(self, clock: Callable[[], float] = time.time, finality_depth: int = 2) -> None:
        self.clock = clock
        self.finality_depth = finality_depth
        self.blocks: list[BlockInfo] = []
        self._txs: dict[str, TxInfo] = {}
        self._mempool: list[str] = []
        self._nonce = 0
        self.mine()  # genesis

    def commit(self, data: bytes) -> str:
        self._nonce += 1
        h = "0x" + _h(b"tx" + data + self._nonce.to_bytes(8, "big")).hex()
        self._txs[h] = TxInfo(h, data, None, None)
        self._mempool.append(h)
        return h

    def mine(self, timestamp: int | None = None) -> BlockInfo:
        number = len(self.blocks)
        ts = int(self.clock()) if timestamp is None else timestamp
        parent = self.blocks[-1].hash if self.blocks else "0x" + "00" * 32
        txs, self._mempool = tuple(self._mempool), []
        bh = "0x" + _h(f"{parent}{number}{ts}{txs}".encode()).hex()
        blk = BlockInfo(number, bh, ts, txs)
        self.blocks.append(blk)
        for t in txs:
            old = self._txs[t]
            self._txs[t] = TxInfo(t, old.data, number, bh)
        return blk

    def mine_many(self, n: int, start: int | None = None, step: int = 1) -> None:
        t0 = int(self.clock()) if start is None else start
        for i in range(n):
            self.mine(t0 + i * step)

    def reorg(self, depth: int) -> None:
        """Drop the newest `depth` blocks; their transactions return to the mempool."""
        for blk in self.blocks[-depth:]:
            for t in blk.tx_hashes:
                old = self._txs[t]
                self._txs[t] = TxInfo(t, old.data, None, None)
                self._mempool.append(t)
        del self.blocks[-depth:]

    def head(self) -> int:
        return len(self.blocks) - 1

    def finalized(self) -> int | None:
        n = self.head() - self.finality_depth
        return n if n >= 0 else None

    def block(self, number: int) -> BlockInfo | None:
        return self.blocks[number] if 0 <= number < len(self.blocks) else None

    def tx(self, tx_hash: str) -> TxInfo | None:
        return self._txs.get(tx_hash)


CHAIN_READ_ONLY = frozenset(
    {"eth_blockNumber", "eth_getBlockByNumber", "eth_getTransactionByHash", "eth_chainId"}
)


class JsonRpcChainReader:
    """Read-only JSON-RPC `ChainReader` (BSC and other EVM chains). Never sends a transaction."""

    def __init__(
        self, url: str, *, timeout_s: float = 15.0, client: httpx.Client | None = None
    ) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self._client = client or httpx.Client(timeout=timeout_s)

    def _call(self, method: str, params: list[Any]) -> Any:
        if method not in CHAIN_READ_ONLY:
            raise AnchorError(
                f"refusing RPC method {method!r}: anchors are read-only (allowed: {sorted(CHAIN_READ_ONLY)})"
            )
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = self._client.post(self.url, json=body, timeout=self.timeout_s)
            resp.raise_for_status()
            out = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AnchorError(f"{self.url} {method}: {type(exc).__name__}: {exc}") from exc
        if "error" in out:
            raise AnchorError(f"{self.url} {method} returned error {out['error']}")
        return out.get("result")

    def head(self) -> int:
        return int(self._call("eth_blockNumber", []), 16)

    def finalized(self) -> int | None:
        blk = self._call("eth_getBlockByNumber", ["finalized", False])
        return None if blk is None else int(blk["number"], 16)

    def block(self, number: int) -> BlockInfo | None:
        blk = self._call("eth_getBlockByNumber", [hex(number), False])
        if blk is None:
            return None
        return BlockInfo(
            int(blk["number"], 16),
            blk["hash"],
            int(blk["timestamp"], 16),
            tuple(blk.get("transactions", [])),
        )

    def tx(self, tx_hash: str) -> TxInfo | None:
        t = self._call("eth_getTransactionByHash", [tx_hash])
        if t is None:
            return None
        bn = t.get("blockNumber")
        return TxInfo(
            tx_hash,
            bytes.fromhex(str(t["input"]).removeprefix("0x")),
            None if bn is None else int(bn, 16),
            t.get("blockHash"),
        )


# ---------------------------------------------------------------- BSC commit anchor

BLOCK_SLACK_S = 3  # a producer can understate a block time by about one block interval


class BscCommitAnchor:
    """Merkle root of a batch of digests committed as calldata; verified through a ChainReader.

    The committer is not trusted for time: the block timestamp is. Verify-only when `committer`
    is None (the MVP never sends a real transaction; the committer is `SimChain` in tests).
    """

    kind = "bsc-commit"

    def __init__(
        self,
        reader: ChainReader,
        committer: Committer | None = None,
        *,
        chain: str = "bsc",
        min_depth: int = 12,
    ) -> None:
        self.reader = reader
        self.committer = committer
        self.chain = chain
        self.min_depth = min_depth

    def stamp_batch(self, digests: Sequence[bytes]) -> list[AnchorProof]:
        if self.committer is None:
            raise AnchorError(
                "this BscCommitAnchor is verify-only (no committer); the MVP never sends transactions"
            )
        tree = MerkleTree(digests)
        tx = self.committer.commit(tree.root)
        return [
            AnchorProof(
                kind=self.kind,
                digest=d.hex(),
                data=json.dumps(
                    {
                        "chain": self.chain,
                        "root": tree.root.hex(),
                        "tx": tx,
                        "path": [s.model_dump() for s in tree.path(i)],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            for i, d in enumerate(digests)
        ]

    def stamp(self, digest: bytes) -> AnchorProof:
        return self.stamp_batch([digest])[0]

    def verify(self, proof: AnchorProof, digest: bytes) -> TimeBound:
        _check_digest(proof, digest)
        if proof.kind != self.kind:
            raise AnchorError(f"proof kind {proof.kind!r} is not {self.kind!r}")
        try:
            data = json.loads(proof.data)
            path = [PathStep.model_validate(s) for s in data["path"]]
            root, tx_hash = str(data["root"]), str(data["tx"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AnchorError(f"malformed {self.kind} proof data: {exc}") from exc
        if root_from_path(digest, path).hex() != root:
            raise AnchorError(
                "Merkle path does not lead to the claimed root: tampered or wrong digest"
            )
        tx = self.reader.tx(tx_hash)
        if tx is None:
            raise AnchorError(
                f"transaction {tx_hash} not found on {self.chain}; wrong chain or dropped"
            )
        if tx.data.hex() != root:
            raise AnchorError(
                "transaction calldata is not this root; the proof points at another tx"
            )
        if tx.block_number is None:
            raise AnchorError(f"transaction {tx_hash} is not mined yet; retry in a few seconds")
        blk = self.reader.block(tx.block_number)
        if blk is None or blk.hash != tx.block_hash or tx_hash not in blk.tx_hashes:
            raise AnchorError(
                f"block {tx.block_number} no longer holds the transaction (reorg); retry"
            )
        head, fin = self.reader.head(), self.reader.finalized()
        if not (
            (fin is not None and tx.block_number <= fin) or head - tx.block_number >= self.min_depth
        ):
            raise AnchorError(
                f"commit in block {tx.block_number} is not final (head {head}); wait for finality"
            )
        return TimeBound(not_after=blk.timestamp + BLOCK_SLACK_S, kind=self.kind)


# ---------------------------------------------------------------- OpenTimestamps

OTS_CALENDARS = (
    "https://alice.btc.calendar.opentimestamps.org",
    "https://bob.btc.calendar.opentimestamps.org",
    "https://finney.calendar.eternitywall.com",
)
OTS_BITCOIN_SLACK_S = 7200  # header time may run 2 h ahead of the median; add it so "by" stays true
OTS_CONFIRMATIONS = 6
MAINNET_MAX_TARGET = 0xFFFF << (8 * (0x1D - 3))  # difficulty-1 floor: rejects cheap forged headers


class HeaderSource(Protocol):
    def header(self, height: int) -> bytes: ...
    def tip_height(self) -> int: ...


class StaticHeaders:
    """Offline header source: {height: 80-byte hex} saved from any node or explorer."""

    def __init__(self, headers: Mapping[int, str], tip: int) -> None:
        self._h = dict(headers)
        self._tip = tip

    def header(self, height: int) -> bytes:
        if height not in self._h:
            raise AnchorError(f"header for Bitcoin block {height} not in the offline file; add it")
        return bytes.fromhex(self._h[height])

    def tip_height(self) -> int:
        return self._tip


class BlockstreamHeaders:
    """Header source over a public explorer. Only public chain data; no key, no account."""

    def __init__(self, base: str = "https://blockstream.info/api", timeout_s: float = 20.0) -> None:
        self.base = base
        self.timeout_s = timeout_s

    def _get(self, path: str) -> str:
        try:
            r = httpx.get(self.base + path, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise AnchorError(f"{self.base}{path}: {type(exc).__name__}: {exc}") from exc
        if r.status_code != 200:
            raise AnchorError(f"{self.base}{path} answered HTTP {r.status_code}: {r.text[:80]}")
        return r.text.strip()

    def header(self, height: int) -> bytes:
        return bytes.fromhex(self._get(f"/block/{self._get(f'/block-height/{height}')}/header"))

    def tip_height(self) -> int:
        return int(self._get("/blocks/tip/height"))


def parse_header(raw: bytes, max_target: int = MAINNET_MAX_TARGET) -> tuple[bytes, int]:
    """Check proof of work on an 80-byte header. Returns (merkle_root, header_time)."""
    if len(raw) != 80:
        raise AnchorError(f"a Bitcoin header is 80 bytes, got {len(raw)}")
    bits = int.from_bytes(raw[72:76], "little")
    exp, mant = bits >> 24, bits & 0xFFFFFF
    target = mant << (8 * (exp - 3))
    if target > max_target:
        raise AnchorError(
            f"header target {target:#x} is below the difficulty floor; source is not real Bitcoin"
        )
    h = _h(_h(raw))
    if int.from_bytes(h, "little") > target:
        raise AnchorError(f"header {h[::-1].hex()} fails its own proof of work; source is lying")
    return raw[36:68], int.from_bytes(raw[68:72], "little")


def _ots_ser(f: DetachedTimestampFile) -> bytes:
    ctx = BytesSerializationContext()
    f.serialize(ctx)
    out: bytes = ctx.getbytes()
    return out


def _ots_de(data: bytes) -> DetachedTimestampFile:
    try:
        f: DetachedTimestampFile = DetachedTimestampFile.deserialize(
            StreamDeserializationContext(io.BytesIO(data))
        )
    except Exception as exc:  # the OTS parser raises many types on bad input
        raise AnchorError(f"not a valid .ots file ({len(data)} bytes): {exc}") from exc
    return f


def _pending(ts: Timestamp) -> list[tuple[Timestamp, PendingAttestation]]:
    out = [(ts, a) for a in ts.attestations if isinstance(a, PendingAttestation)]
    for sub in ts.ops.values():
        out.extend(_pending(sub))
    return out


class OtsAnchor:
    """OpenTimestamps. `stamp` and `upgrade` need the network; `verify` needs only the headers."""

    kind = "ots"

    def __init__(
        self,
        headers: HeaderSource | None = None,
        *,
        calendars: tuple[str, ...] = OTS_CALENDARS,
        max_target: int = MAINNET_MAX_TARGET,
        timeout_s: float = 20.0,
    ) -> None:
        self.headers = headers
        self.calendars = calendars
        self.max_target = max_target
        self.timeout_s = timeout_s

    def stamp(self, digest: bytes) -> AnchorProof:
        """Submit to the calendars; the result is *pending* until Bitcoin confirms (hours)."""
        if len(digest) != 32:
            raise AnchorError(f"digest must be 32 bytes, got {len(digest)}")
        f = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
        root = f.timestamp.ops.add(OpAppend(os.urandom(16))).ops.add(OpSHA256())
        errors: dict[str, str] = {}
        ok = 0
        for url in self.calendars:
            try:
                r = httpx.post(
                    url + "/digest",
                    content=root.msg,
                    timeout=self.timeout_s,
                    headers={"Accept": "application/vnd.opentimestamps.v1"},
                )
                r.raise_for_status()
                root.merge(Timestamp.deserialize(BytesDeserializationContext(r.content), root.msg))
                ok += 1
            except Exception as exc:  # one calendar down must not fail the stamp
                errors[url] = f"{type(exc).__name__}: {exc}"
        if ok == 0:
            raise AnchorError(
                f"no OpenTimestamps calendar accepted the digest ({errors}); check the network"
            )
        return AnchorProof(
            kind=self.kind, digest=digest.hex(), data=base64.b64encode(_ots_ser(f)).decode()
        )

    def upgrade(self, proof: AnchorProof) -> tuple[AnchorProof, bool]:
        """Ask pending calendars for the Bitcoin path. Returns (new proof, changed)."""
        f = _ots_de(base64.b64decode(proof.data))
        changed = False
        for node, att in _pending(f.timestamp):
            try:
                r = httpx.get(
                    att.uri.rstrip("/") + "/timestamp/" + node.msg.hex(), timeout=self.timeout_s
                )
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                node.merge(Timestamp.deserialize(BytesDeserializationContext(r.content), node.msg))
                changed = True
        if not changed:
            return proof, False
        return proof.model_copy(update={"data": base64.b64encode(_ots_ser(f)).decode()}), True

    def verify(self, proof: AnchorProof, digest: bytes) -> TimeBound:
        _check_digest(proof, digest)
        if proof.kind != self.kind:
            raise AnchorError(f"proof kind {proof.kind!r} is not {self.kind!r}")
        if self.headers is None:
            raise AnchorError(
                "OtsAnchor has no header source; pass headers=StaticHeaders(..) or BlockstreamHeaders()"
            )
        try:
            blob = base64.b64decode(proof.data, validate=True)
        except ValueError as exc:
            raise AnchorError(f"ots proof data is not base64: {exc}") from exc
        f = _ots_de(blob)
        if f.timestamp.msg != digest:
            raise AnchorError(
                f"the .ots stamps {f.timestamp.msg.hex()[:16]}.., not {digest.hex()[:16]}.."
            )
        btc = [
            (m, a)
            for m, a in f.timestamp.all_attestations()
            if isinstance(a, BitcoinBlockHeaderAttestation)
        ]
        if not btc:
            raise AnchorError(
                "ots proof is pending: no Bitcoin block yet; run upgrade() in a few hours"
            )
        tip = self.headers.tip_height()
        best: int | None = None
        for att_digest, att in btc:
            merkle_root, t = parse_header(self.headers.header(att.height), self.max_target)
            if att_digest != merkle_root:
                continue
            if tip - att.height + 1 < OTS_CONFIRMATIONS:
                continue  # not buried yet: a reorg could still remove it
            bound = t + OTS_BITCOIN_SLACK_S
            best = bound if best is None else min(best, bound)
        if best is None:
            raise AnchorError(
                f"no Bitcoin attestation matches its block's merkle root with {OTS_CONFIRMATIONS} confirmations"
            )
        return TimeBound(not_after=best, kind=self.kind)


# ---------------------------------------------------------------- combining proofs


def verifier_map(*anchors: Anchor) -> dict[str, Anchor]:
    """The kind -> verifier map `trackrecord.compute(verifiers=...)` and the governance tally take.

    `verifier_map(TsaAnchor(), OtsAnchor(...), bsc)` accepts "tsa", "ots" and "bsc-commit" proofs.
    Two anchors of one kind are a mistake (one would be silently ignored), so it raises.
    """
    out: dict[str, Anchor] = {}
    for a in anchors:
        if a.kind in out:
            raise AnchorError(
                f"two anchors claim proof kind {a.kind!r}; pass one verifier per kind"
            )
        out[a.kind] = a
    return out


def verify_proofs(
    digest: bytes, proofs: Iterable[AnchorProof], verifiers: Mapping[str, Anchor]
) -> tuple[list[TimeBound], list[str]]:
    """Verify every proof for `digest`. Returns (valid bounds, reasons for the failures).

    Proofs for other digests are ignored; proofs of a kind with no verifier are reported.
    """
    bounds: list[TimeBound] = []
    why: list[str] = []
    for p in proofs:
        if p.digest != digest.hex():
            continue
        verifier = verifiers.get(p.kind)
        if verifier is None:
            why.append(f"{p.kind}: no verifier configured")
            continue
        try:
            bounds.append(verifier.verify(p, digest))
        except AnchorError as exc:
            why.append(f"{p.kind}: {exc}")
    return bounds, why


def anchored_time(
    digest: bytes, proofs: Iterable[AnchorProof], verifiers: Mapping[str, Anchor]
) -> int | None:
    """Earliest `not_after` over the proofs that verify; None when the digest is unanchored."""
    bounds, _ = verify_proofs(digest, proofs, verifiers)
    return min((b.not_after for b in bounds), default=None)
