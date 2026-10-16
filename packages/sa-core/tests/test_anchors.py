"""Anchors: Merkle batches, BscCommitAnchor on a simulated chain, OpenTimestamps, anchored_time."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
from opentimestamps.core.op import OpAppend, OpSHA256
from opentimestamps.core.serialize import BytesSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from sa_core.anchors import (
    BLOCK_SLACK_S,
    OTS_BITCOIN_SLACK_S,
    Anchor,
    AnchorError,
    BscCommitAnchor,
    JsonRpcChainReader,
    MerkleTree,
    OtsAnchor,
    SimChain,
    StaticHeaders,
    TimeBound,
    anchored_time,
    parse_header,
    root_from_path,
    verify_proofs,
)
from sa_core.models import AnchorProof

DATA = Path(__file__).parent / "data"
SIM_TARGET = 0x7FFFFF << (8 * (0x20 - 3))
EASY_BITS = 0x207FFFFF


def d32(i: int) -> bytes:
    return hashlib.sha256(f"event-{i}".encode()).digest()


# ---------------------------------------------------------------- Merkle


@given(n=st.integers(1, 40), data=st.data())
def test_merkle_paths_reach_the_root_and_nothing_else_does(n: int, data: st.DataObject) -> None:
    ids = [d32(i) for i in range(n)]
    tree = MerkleTree(ids)
    i = data.draw(st.integers(0, n - 1))
    assert root_from_path(ids[i], tree.path(i)) == tree.root
    assert root_from_path(d32(10_000), tree.path(i)) != tree.root
    j = data.draw(st.integers(0, n - 1))
    if j != i:
        assert root_from_path(ids[j], tree.path(i)) != tree.root


def test_merkle_input_validation() -> None:
    with pytest.raises(AnchorError, match="0 digests"):
        MerkleTree([])
    with pytest.raises(AnchorError, match="32 bytes"):
        MerkleTree([b"short"])
    with pytest.raises(AnchorError, match="outside"):
        MerkleTree([d32(1)]).path(3)


# ---------------------------------------------------------------- BSC commit on a simulated chain


class Clock:
    def __init__(self, t: float = 1_800_000_000) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _bsc(min_depth: int = 3) -> tuple[SimChain, BscCommitAnchor, Clock]:
    clock = Clock()
    chain = SimChain(clock=clock, finality_depth=2)
    return chain, BscCommitAnchor(chain, chain, min_depth=min_depth), clock


def test_bsc_commit_proves_a_bound_equal_to_the_block_time_plus_slack() -> None:
    chain, anchor, clock = _bsc()
    ids = [d32(i) for i in range(5)]
    proofs = anchor.stamp_batch(ids)
    with pytest.raises(AnchorError, match="not mined yet"):
        anchor.verify(proofs[0], ids[0])
    clock.t += 2
    chain.mine()
    clock.t += 1
    chain.mine_many(3, start=int(clock.t))
    for p, d in zip(proofs, ids, strict=True):
        bound = anchor.verify(p, d)
        assert bound == TimeBound(not_after=1_800_000_002 + BLOCK_SLACK_S, kind="bsc-commit")
    assert isinstance(anchor, Anchor)


def test_bsc_commit_rejects_forgeries_and_unfinal_commits() -> None:
    chain, anchor, _ = _bsc(min_depth=50)
    ids = [d32(i) for i in range(3)]
    proofs = anchor.stamp_batch(ids)
    chain.mine()
    with pytest.raises(AnchorError, match="not final"):
        anchor.verify(proofs[0], ids[0])
    chain.mine_many(3)  # finality_depth 2 makes the block finalized
    assert anchor.verify(proofs[0], ids[0]).kind == "bsc-commit"
    with pytest.raises(AnchorError, match="not this digest"):
        anchor.verify(proofs[0], ids[1])
    wrong_path = proofs[0].model_copy(update={"digest": ids[1].hex()})
    with pytest.raises(AnchorError, match="does not lead to the claimed root"):
        anchor.verify(wrong_path, ids[1])
    data = json.loads(proofs[0].data)
    data["tx"] = "0x" + "11" * 32
    with pytest.raises(AnchorError, match="not found"):
        anchor.verify(proofs[0].model_copy(update={"data": json.dumps(data)}), ids[0])
    other_tx = chain.commit(b"\x01" * 32)
    chain.mine()
    data["tx"] = other_tx
    with pytest.raises(AnchorError, match="another tx"):
        anchor.verify(proofs[0].model_copy(update={"data": json.dumps(data)}), ids[0])
    with pytest.raises(AnchorError, match="malformed"):
        anchor.verify(proofs[0].model_copy(update={"data": "{"}), ids[0])
    with pytest.raises(AnchorError, match="proof kind"):
        anchor.verify(proofs[0].model_copy(update={"kind": "ots"}), ids[0])
    with pytest.raises(AnchorError, match="32 bytes"):
        anchor.verify(proofs[0], b"x")


def test_a_reorg_removes_the_proof_until_remined_later() -> None:
    chain, anchor, _ = _bsc()
    ids = [d32(1)]
    (proof,) = anchor.stamp_batch(ids)
    chain.mine(1_800_000_001)
    chain.mine_many(4, start=1_800_000_002)
    first = anchor.verify(proof, ids[0]).not_after
    chain.reorg(5)
    with pytest.raises(AnchorError, match="not mined yet"):
        anchor.verify(proof, ids[0])
    chain.mine(1_800_000_500)  # re-mined much later: the bound can only get later
    chain.mine_many(4, start=1_800_000_501)
    assert anchor.verify(proof, ids[0]).not_after > first


def test_a_dropped_block_with_the_tx_is_detected() -> None:
    chain, anchor, _ = _bsc()
    ids = [d32(1)]
    (proof,) = anchor.stamp_batch(ids)
    chain.mine()
    chain.mine_many(4)
    chain.reorg(5)  # tx back in mempool
    chain.mine_many(5)
    # re-mined in a block with a different hash/number: proof still valid, bound is from new block
    assert anchor.verify(proof, ids[0]).kind == "bsc-commit"


def test_verify_only_anchor_cannot_stamp() -> None:
    chain = SimChain()
    anchor = BscCommitAnchor(chain)
    with pytest.raises(AnchorError, match="verify-only"):
        anchor.stamp(d32(1))


# ---------------------------------------------------------------- JSON-RPC chain reader


def test_json_rpc_chain_reader_parses_and_is_read_only() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        m = body["method"]
        if m == "eth_blockNumber":
            res: Any = "0x10"
        elif m == "eth_getBlockByNumber" and body["params"][0] == "finalized":
            res = {"number": "0xe"}
        elif m == "eth_getBlockByNumber":
            res = {"number": "0x5", "hash": "0xaa", "timestamp": "0x64", "transactions": ["0xt1"]}
            if body["params"][0] == "0x63":
                res = None
        else:
            res = {"input": "0x" + "ab" * 32, "blockNumber": "0x5", "blockHash": "0xaa"}
            if body["params"][0] == "0xmissing":
                res = None
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": res})

    reader = JsonRpcChainReader(
        "https://rpc", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert reader.head() == 16 and reader.finalized() == 14
    blk = reader.block(5)
    assert blk is not None and (blk.number, blk.timestamp, blk.tx_hashes) == (5, 100, ("0xt1",))
    assert reader.block(99) is None
    tx = reader.tx("0xt1")
    assert tx is not None and tx.block_number == 5 and tx.data == bytes.fromhex("ab" * 32)
    assert reader.tx("0xmissing") is None
    with pytest.raises(AnchorError, match="read-only"):
        reader._call("eth_sendRawTransaction", ["0x"])


def test_json_rpc_chain_reader_errors_are_anchor_errors() -> None:
    bad = JsonRpcChainReader(
        "https://rpc",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    with pytest.raises(AnchorError, match="eth_blockNumber"):
        bad.head()
    err = JsonRpcChainReader(
        "https://rpc",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": "nope"}))
        ),
    )
    with pytest.raises(AnchorError, match="returned error"):
        err.head()


# ---------------------------------------------------------------- OpenTimestamps


def _headers_static() -> StaticHeaders:
    blob = json.loads((DATA / "headers-358391.json").read_text())
    return StaticHeaders({int(k): v for k, v in blob["headers"].items()}, blob["tip"])


HELLO = hashlib.sha256((DATA / "hello-world.txt").read_bytes()).digest()


def _proof(path: str, digest: bytes) -> AnchorProof:
    return AnchorProof(
        kind="ots", digest=digest.hex(), data=base64.b64encode((DATA / path).read_bytes()).decode()
    )


def test_real_confirmed_ots_proof_verifies_offline() -> None:
    """tests/data/hello-world.txt.ots is a real proof (Bitcoin block 358391, header saved beside it)."""
    anchor = OtsAnchor(_headers_static())
    bound = anchor.verify(_proof("hello-world.txt.ots", HELLO), HELLO)
    assert bound == TimeBound(not_after=1432827678 + OTS_BITCOIN_SLACK_S, kind="ots")


def test_ots_rejections() -> None:
    anchor = OtsAnchor(_headers_static())
    good = _proof("hello-world.txt.ots", HELLO)
    with pytest.raises(AnchorError, match="not this digest"):
        anchor.verify(good, b"\x01" * 32)
    wrong = good.model_copy(update={"digest": ("01" * 32)})
    with pytest.raises(AnchorError, match="stamps"):
        anchor.verify(wrong, b"\x01" * 32)
    pending_digest = hashlib.sha256((DATA / "incomplete.txt").read_bytes()).digest()
    with pytest.raises(AnchorError, match="pending"):
        anchor.verify(_proof("incomplete.txt.ots", pending_digest), pending_digest)
    with pytest.raises(AnchorError, match="not base64"):
        anchor.verify(good.model_copy(update={"data": "***"}), HELLO)
    with pytest.raises(AnchorError, match="not a valid .ots"):
        anchor.verify(good.model_copy(update={"data": base64.b64encode(b"junk").decode()}), HELLO)
    with pytest.raises(AnchorError, match="no header source"):
        OtsAnchor().verify(good, HELLO)
    with pytest.raises(AnchorError, match="proof kind"):
        anchor.verify(good.model_copy(update={"kind": "bsc-commit"}), HELLO)
    blob = json.loads((DATA / "headers-358391.json").read_text())
    h = bytearray(bytes.fromhex(blob["headers"]["358391"]))
    h[40] ^= 1  # corrupt the merkle root: proof of work fails first
    with pytest.raises(AnchorError, match="proof of work"):
        OtsAnchor(StaticHeaders({358391: h.hex()}, blob["tip"])).verify(good, HELLO)
    with pytest.raises(AnchorError, match="not in the offline file"):
        OtsAnchor(StaticHeaders({}, 1)).verify(good, HELLO)
    with pytest.raises(AnchorError, match="80 bytes"):
        parse_header(b"x")


class FakeBitcoin:
    """Regtest-difficulty header chain; a simulated calendar commits digests into it."""

    def __init__(self) -> None:
        self.headers: dict[int, bytes] = {}
        self._mine(0, b"\x00" * 32)

    def _mine(self, ts: int, merkle_root: bytes) -> int:
        height = len(self.headers)
        prev = (
            hashlib.sha256(hashlib.sha256(self.headers[height - 1]).digest()).digest()
            if height
            else b"\x00" * 32
        )
        for nonce in range(1 << 20):
            hdr = struct.pack("<I32s32sIII", 1, prev, merkle_root, ts, EASY_BITS, nonce)
            h = hashlib.sha256(hashlib.sha256(hdr).digest()).digest()
            if int.from_bytes(h, "little") <= SIM_TARGET:
                self.headers[height] = hdr
                return height
        raise RuntimeError("no nonce")

    def header(self, height: int) -> bytes:
        return self.headers[height]

    def tip_height(self) -> int:
        return len(self.headers) - 1

    def ots_for(self, digest: bytes, block_time: int) -> AnchorProof:
        f = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
        root = f.timestamp.ops.add(OpAppend(os.urandom(16))).ops.add(OpSHA256())
        root.attestations.add(BitcoinBlockHeaderAttestation(self._mine(block_time, root.msg)))
        ctx = BytesSerializationContext()
        f.serialize(ctx)
        return AnchorProof(
            kind="ots", digest=digest.hex(), data=base64.b64encode(ctx.getbytes()).decode()
        )

    def bury(self, n: int, start: int) -> None:
        for i in range(n):
            self._mine(start + 600 * (i + 1), os.urandom(32))


def test_simulated_calendar_needs_six_confirmations_and_adds_the_slack() -> None:
    btc = FakeBitcoin()
    d = d32(7)
    proof = btc.ots_for(d, 1_800_000_000)
    anchor = OtsAnchor(btc, max_target=SIM_TARGET)
    with pytest.raises(AnchorError, match="confirmations"):
        anchor.verify(proof, d)
    btc.bury(6, 1_800_000_000)
    assert anchor.verify(proof, d).not_after == 1_800_000_000 + OTS_BITCOIN_SLACK_S


def test_cheap_forged_header_is_refused_by_the_mainnet_difficulty_floor() -> None:
    btc = FakeBitcoin()
    d = d32(8)
    proof = btc.ots_for(d, 1_700_000_000)
    btc.bury(6, 1_700_000_000)
    with pytest.raises(AnchorError, match="below the difficulty floor"):
        OtsAnchor(btc).verify(proof, d)


def test_ots_stamp_and_upgrade_with_mocked_calendars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The calendar answer is a one-op timestamp; stamping merges it, upgrade finds nothing yet."""
    from opentimestamps.core.serialize import BytesSerializationContext as Ctx

    def fake_post(url: str, *, content: bytes, **_: Any) -> httpx.Response:
        stub = Timestamp(content)
        stub.attestations.add(
            __import__(
                "opentimestamps.core.notary", fromlist=["PendingAttestation"]
            ).PendingAttestation(url)
        )
        ctx = Ctx()
        stub.serialize(ctx)
        return httpx.Response(200, content=ctx.getbytes(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_: httpx.Response(404, text="Pending", request=httpx.Request("GET", url)),
    )
    anchor = OtsAnchor(calendars=("https://cal.example",))
    proof = anchor.stamp(d32(9))
    assert proof.kind == "ots" and proof.digest == d32(9).hex()
    same, changed = anchor.upgrade(proof)
    assert not changed and same == proof
    with pytest.raises(AnchorError, match="pending"):
        OtsAnchor(_headers_static()).verify(proof, d32(9))
    with pytest.raises(AnchorError, match="32 bytes"):
        anchor.stamp(b"x")


def test_ots_stamp_fails_loudly_when_no_calendar_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(url: str, **_: Any) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(AnchorError, match="no OpenTimestamps calendar accepted"):
        OtsAnchor(calendars=("https://a", "https://b")).stamp(d32(1))


def test_ots_upgrade_merges_a_calendar_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    btc = FakeBitcoin()
    d = d32(11)

    def fake_post(url: str, *, content: bytes, **_: Any) -> httpx.Response:
        from opentimestamps.core.notary import PendingAttestation

        stub = Timestamp(content)
        stub.attestations.add(PendingAttestation(url))
        ctx = BytesSerializationContext()
        stub.serialize(ctx)
        return httpx.Response(200, content=ctx.getbytes(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    anchor = OtsAnchor(btc, calendars=("https://cal.example",), max_target=SIM_TARGET)
    proof = anchor.stamp(d)

    def fake_get(url: str, **_: Any) -> httpx.Response:
        node_msg = bytes.fromhex(url.rsplit("/", 1)[1])
        height = btc._mine(1_800_000_000, node_msg)  # calendar commits this node into Bitcoin
        stub = Timestamp(node_msg)
        stub.attestations.add(BitcoinBlockHeaderAttestation(height))
        ctx = BytesSerializationContext()
        stub.serialize(ctx)
        return httpx.Response(200, content=ctx.getbytes(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    upgraded, changed = anchor.upgrade(proof)
    assert changed
    btc.bury(6, 1_800_000_000)
    assert anchor.verify(upgraded, d).not_after == 1_800_000_000 + OTS_BITCOIN_SLACK_S


# ---------------------------------------------------------------- combining proofs


def test_anchored_time_is_the_earliest_valid_bound_and_none_when_unanchored() -> None:
    chain, bsc, _ = _bsc()
    btc = FakeBitcoin()
    ots = OtsAnchor(btc, max_target=SIM_TARGET)
    d = d32(21)
    (bsc_proof,) = bsc.stamp_batch([d])
    chain.mine(1_800_000_010)
    chain.mine_many(4, start=1_800_000_011)
    ots_proof = btc.ots_for(d, 1_800_000_100)
    btc.bury(6, 1_800_000_100)
    verifiers: dict[str, Anchor] = {"bsc-commit": bsc, "ots": ots}
    t = anchored_time(d, [ots_proof, bsc_proof], verifiers)
    assert t == 1_800_000_010 + BLOCK_SLACK_S  # BSC is far tighter than OTS
    assert anchored_time(d, [ots_proof], verifiers) == 1_800_000_100 + OTS_BITCOIN_SLACK_S
    assert anchored_time(d, [], verifiers) is None
    assert anchored_time(d32(22), [bsc_proof], verifiers) is None  # proof is for another digest
    bad = bsc_proof.model_copy(update={"data": "{"})
    bounds, why = verify_proofs(
        d, [bad, ots_proof, AnchorProof(kind="tsa", digest=d.hex(), data="x")], verifiers
    )
    assert [b.kind for b in bounds] == ["ots"]
    assert any("malformed" in w for w in why) and any("no verifier" in w for w in why)
