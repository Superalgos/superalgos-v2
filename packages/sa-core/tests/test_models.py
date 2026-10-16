"""SEP-3.2 (required fields), SEP-3.3 (lossless round trip), and model invariants."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from core_helpers import T0_MS, Provider
from sa_core.models import (
    AnchorProof,
    Bundle,
    Commitment,
    Delegation,
    GovDelegation,
    PowerAllocation,
    PrivateRule,
    Proposal,
    ProviderRuleSet,
    Signal,
    Tier,
    Vote,
    norm_addr,
)

HEX64 = st.text("0123456789abcdef", min_size=64, max_size=64)
ADDR = st.text("0123456789abcdef", min_size=40, max_size=40).map(lambda s: "0x" + s)
SIG = st.text("0123456789abcdef", min_size=130, max_size=130).map(lambda s: "0x" + s)


@st.composite
def signals(draw: st.DrawFn) -> Signal:
    seq = draw(st.integers(1, 10**9))
    order_type = draw(st.sampled_from(["market", "limit", "stop"]))
    price = st.floats(1e-9, 1e9, allow_nan=False, allow_infinity=False)
    return Signal(
        strategy_id=draw(st.text(min_size=1, max_size=40)),
        seq=seq,
        prev=None if seq == 1 else draw(HEX64),
        instrument=draw(st.from_regex(r"^[A-Z0-9]{1,8}:[A-Z0-9._-]{1,12}$", fullmatch=True)),
        side=draw(st.sampled_from(["buy", "sell"])),
        action=draw(st.sampled_from(["open", "close", "adjust"])),
        size_fraction=draw(st.floats(1e-9, 1.0, allow_nan=False, allow_infinity=False)),
        order_type=order_type,
        limit_price=draw(price) if order_type == "limit" else draw(st.none() | price),
        stop_price=draw(price) if order_type == "stop" else draw(st.none() | price),
        timeframe=draw(st.integers(1, 10**7)),
        created_ms=draw(st.integers(1, 10**15)),
    )


@given(signals())
def test_sep_3_3_signal_json_round_trip_is_lossless(sig: Signal) -> None:
    again = Signal.model_validate_json(sig.model_dump_json())
    assert again == sig
    assert again.model_dump_json() == sig.model_dump_json()


REQUIRED_SIGNAL_FIELDS = ["instrument", "side", "action", "size_fraction", "created_ms"]


@pytest.mark.parametrize("missing", REQUIRED_SIGNAL_FIELDS)
def test_sep_3_2_signal_missing_a_required_field_is_rejected(missing: str) -> None:
    data = Provider().next_signal().model_dump()
    del data[missing]
    with pytest.raises(ValidationError, match=missing):
        Signal.model_validate(data)


@given(
    sig=signals(),
    field=st.sampled_from(["instrument", "side", "action", "size_fraction", "created_ms"]),
)
def test_sep_3_2_property_any_required_field_missing(sig: Signal, field: str) -> None:
    data = sig.model_dump()
    del data[field]
    with pytest.raises(ValidationError):
        Signal.model_validate(data)


def test_sep_3_2_provider_identity_and_signature_belong_to_the_event() -> None:
    """Identity and signature are the Nostr event's pubkey / sig; an event without them fails."""
    from sa_core.models import NostrEvent

    ev = Provider().publish().model_dump()
    for missing in ("pubkey", "sig", "created_at"):
        data = dict(ev)
        del data[missing]
        with pytest.raises(ValidationError, match=missing):
            NostrEvent.model_validate(data)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"seq": 1, "prev": "a" * 64}, "seq 1 must have prev=null"),
        ({"seq": 2, "prev": None}, "must carry the previous"),
        ({"order_type": "limit", "limit_price": None}, "requires limit_price"),
        ({"size_fraction": 0.0}, "greater than 0"),
        ({"size_fraction": 1.5}, "less than or equal to 1"),
        ({"side": "long"}, "buy"),
        ({"instrument": "btcusdt"}, "pattern"),
        ({"timeframe": 0}, "greater than 0"),
        ({"extra": 1}, "Extra inputs"),
        ({"size_fraction": float("nan")}, "finite"),
    ],
)
def test_signal_rejects_invalid(patch: dict[str, object], message: str) -> None:
    data = Provider().next_signal().model_dump() | patch
    with pytest.raises(ValidationError, match=message):
        Signal.model_validate(data)


def test_commitment_chain_slot_must_be_consistent() -> None:
    Commitment(strategy_id="s", seq=1, prev=None, digest="a" * 64)
    Commitment(strategy_id="s", seq=2, prev="b" * 64, digest="a" * 64)
    with pytest.raises(ValidationError, match="disagree"):
        Commitment(strategy_id="s", seq=2, prev=None, digest="a" * 64)


@given(
    address=ADDR,
    pk=HEX64,
    exp=st.integers(1, 10**10),
    sig=SIG,
)
def test_delegation_and_proof_round_trip(address: str, pk: str, exp: int, sig: str) -> None:
    d = Delegation(
        evm_address=address.upper().replace("0X", "0x"),
        nostr_pubkey=pk,
        expires_at=exp,
        signature=sig,
    )
    assert d.evm_address == address.lower()
    assert Delegation.model_validate_json(d.model_dump_json()) == d
    p = AnchorProof(kind="ots", digest=pk, data="AAEC")
    assert AnchorProof.model_validate_json(p.model_dump_json()) == p


@given(
    address=ADDR,
    nonce=st.integers(0, 10**6),
    gov=st.integers(0, 10_000),
    providers=st.dictionaries(HEX64, st.integers(1, 10_000), max_size=4),
    sig=SIG,
)
def test_allocation_round_trip_and_sum_rule(
    address: str, nonce: int, gov: int, providers: dict[str, int], sig: str
) -> None:
    if gov + sum(providers.values()) > 10_000:
        with pytest.raises(ValidationError, match="more than 10000"):
            PowerAllocation(
                address=address, nonce=nonce, governance=gov, providers=providers, sig=sig
            )
        return
    a = PowerAllocation(address=address, nonce=nonce, governance=gov, providers=providers, sig=sig)
    assert PowerAllocation.model_validate_json(a.model_dump_json()) == a
    assert a.total_bps() <= 10_000


@given(
    provider=HEX64,
    address=ADDR,
    tf=st.integers(1, 10**6),
    powers=st.lists(st.integers(0, 10**9), min_size=1, max_size=5, unique=True),
    sig=SIG,
)
def test_ruleset_round_trip(
    provider: str, address: str, tf: int, powers: list[int], sig: str
) -> None:
    ordered = sorted(powers)
    tiers = tuple(
        Tier(min_power=p, delay_fraction=round(2.0 - i * 0.1, 4)) for i, p in enumerate(ordered)
    )
    rs = ProviderRuleSet(provider=provider, sa_address=address, timeframe=tf, tiers=tiers, sig=sig)
    # delays fall as min_power rises (index grows with power) -> valid
    assert ProviderRuleSet.model_validate_json(rs.model_dump_json()) == rs
    priv = ProviderRuleSet(
        provider=provider,
        sa_address=address,
        timeframe=tf,
        private=PrivateRule(min_power=5),
        sig=sig,
    )
    assert ProviderRuleSet.model_validate_json(priv.model_dump_json()) == priv


def test_ruleset_shape_rules() -> None:
    base = {
        "provider": "a" * 64,
        "sa_address": "0x" + "1" * 40,
        "timeframe": 60,
        "sig": "0x" + "0" * 130,
    }
    with pytest.raises(ValidationError, match="exactly one"):
        ProviderRuleSet.model_validate(base)
    with pytest.raises(ValidationError, match="exactly one"):
        ProviderRuleSet.model_validate(
            base | {"tiers": [{"min_power": 0, "delay_fraction": 1}], "private": {"min_power": 1}}
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        ProviderRuleSet.model_validate(base | {"tiers": []})
    with pytest.raises(ValidationError, match="duplicate min_power"):
        ProviderRuleSet.model_validate(
            base
            | {
                "tiers": [
                    {"min_power": 5, "delay_fraction": 1},
                    {"min_power": 5, "delay_fraction": 0},
                ]
            }
        )
    with pytest.raises(ValidationError, match="never wait longer"):
        ProviderRuleSet.model_validate(
            base
            | {
                "tiers": [
                    {"min_power": 0, "delay_fraction": 0.5},
                    {"min_power": 9, "delay_fraction": 1},
                ]
            }
        )
    with pytest.raises(ValidationError, match="at most 4 decimals"):
        Tier(min_power=0, delay_fraction=0.12345)


def test_proposal_vote_delegation_and_bundle_round_trip() -> None:
    prop = Proposal(
        title="t",
        options=("yes", "no"),
        deadline=200,
        snapshot_time=100,
        snapshot_blocks={56: 10},
        relays=("ws://a",),
    )
    assert Proposal.model_validate_json(prop.model_dump_json()) == prop
    with pytest.raises(ValidationError, match="after deadline"):
        Proposal(title="t", options=("a", "b"), deadline=5, snapshot_time=6, relays=("ws://a",))
    vote = Vote(proposal="a" * 64, option=1, nonce=3)
    assert Vote.model_validate_json(vote.model_dump_json()) == vote
    sig = "0x" + "0" * 130
    gd = GovDelegation(address="0x" + "1" * 40, nonce=1, to={"0x" + "2" * 40: 5000}, sig=sig)
    assert GovDelegation.model_validate_json(gd.model_dump_json()) == gd
    with pytest.raises(ValidationError, match="itself"):
        GovDelegation(address="0x" + "1" * 40, nonce=1, to={"0x" + "1" * 40: 1}, sig=sig)
    with pytest.raises(ValidationError, match="more than 10000"):
        GovDelegation(
            address="0x" + "1" * 40,
            nonce=1,
            to={"0x" + "2" * 40: 9000, "0x" + "3" * 40: 2000},
            sig=sig,
        )
    p = Provider()
    p.publish()
    p.publish(at_ms=T0_MS + 1000)
    b = p.bundle()
    assert Bundle.model_validate_json(b.model_dump_json()) == b


def test_norm_addr_names_the_bad_value() -> None:
    assert norm_addr("0x" + "AB" * 20) == "0x" + "ab" * 20
    with pytest.raises(ValueError, match="'nope'"):
        norm_addr("nope")


def test_stop_orders_need_a_stop_price() -> None:
    ok = Provider().next_signal(order_type="stop", stop_price=105.0)
    assert ok.order_type == "stop"
    assert ok.stop_price == 105.0
    with pytest.raises(ValidationError, match="'stop' requires stop_price"):
        Provider().next_signal(order_type="stop")
    with pytest.raises(ValidationError, match="order_type"):
        Provider().next_signal(order_type="stop-limit")


# SEP-1.13: every wire model is a closed schema. A secret field in signal, commitment, allocation, rule
# set or delegation content is a validation error that names the field (the relay does the same in Go).
SECRET_FIELDS = ["api_secret", "private_key", "mnemonic", "apiKey", "secret_key"]


def _wire_contents() -> dict[str, tuple[type[BaseModel], dict[str, object]]]:
    sig = "0x" + "ab" * 65
    addr = "0x" + "11" * 20
    return {
        "signal": (Signal, Provider().next_signal().model_dump()),
        "commitment": (
            Commitment,
            {"strategy_id": "s", "seq": 1, "prev": None, "digest": "a" * 64},
        ),
        "allocation": (
            PowerAllocation,
            {"address": addr, "nonce": 1, "governance": 100, "providers": {}, "sig": sig},
        ),
        "rule set": (
            ProviderRuleSet,
            {
                "provider": "b" * 64,
                "sa_address": addr,
                "timeframe": 60,
                "tiers": [{"min_power": 0, "delay_fraction": 1.0}],
                "sig": sig,
            },
        ),
        "governance delegation": (
            GovDelegation,
            {"address": addr, "nonce": 1, "to": {"0x" + "22" * 20: 100}, "sig": sig},
        ),
    }


@pytest.mark.parametrize("field", SECRET_FIELDS)
@pytest.mark.parametrize("name", list(_wire_contents()))
def test_wire_models_reject_secret_fields(name: str, field: str) -> None:
    model, data = _wire_contents()[name]
    model.model_validate(data)  # the clean content is valid, so the field is the only difference
    with pytest.raises(ValidationError, match=f"{field}\n.*Extra inputs"):
        model.model_validate(data | {field: "hunter2"})
