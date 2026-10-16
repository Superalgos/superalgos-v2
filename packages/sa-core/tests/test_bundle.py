"""Bundles: SEP-3.14 (offline recompute equals relay recompute), SEP-3.15 (omit/reorder/edit is flagged)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core_helpers import FIXTURE_CANDLES, MINUTE, T0_MS, Provider, anchor_all
from sa_core.bundle import (
    BundleError,
    bundle_from_json,
    bundle_to_json,
    check_bundle,
    dump_check,
    export_bundle,
    read_bundle,
    write_bundle,
)
from sa_core.delegation import delegation_tag, sign_delegation
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import NostrEvent, Reveal
from sa_core.nostr import build_event
from sa_core.trackrecord import compute

N = 7


def _chain() -> Provider:
    p = Provider()
    for i in range(N):
        kw = {
            "at_ms": T0_MS + i * MINUTE,
            "side": "buy" if i % 2 == 0 else "sell",
            "action": "open" if i % 2 == 0 else "close",
            "size": 0.5 if i % 2 == 0 else 1.0,
        }
        if i == 3:
            p.commit(reveal=True, **kw)
        elif i == 5:
            p.commit(reveal=False, **kw)
        else:
            p.publish(**kw)
    return p


PROVIDER = _chain()
BUNDLE = PROVIDER.bundle()
HEAD = {PROVIDER.strategy_id: (N, PROVIDER.events[-1].id)}


def test_complete_bundle_passes_and_round_trips_through_json_and_files(tmp_path: Path) -> None:
    check = check_bundle(BUNDLE)
    assert check.complete and not check.problems and not check.gaps
    assert [i.seq for i in check.items] == list(range(1, N + 1))
    assert len(check.unrevealed) == 1 and check.unrevealed[0] == PROVIDER.events[5].id
    assert not check.head_checked and check_bundle(BUNDLE, expected_heads=HEAD).head_checked
    text = bundle_to_json(BUNDLE)
    assert bundle_from_json(text) == BUNDLE and bundle_to_json(bundle_from_json(text)) == text
    path = tmp_path / "b.json"
    write_bundle(path, BUNDLE)
    assert read_bundle(path) == BUNDLE
    assert json.loads(dump_check(check))["complete"] is True


def test_bundle_io_errors_say_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="not found"):
        read_bundle(tmp_path / "nope.json")
    with pytest.raises(BundleError, match="not a Superalgos bundle"):
        bundle_from_json("{}")


def test_export_selects_orders_and_filters() -> None:
    other = Provider()
    other.publish()
    junk = build_event(PROVIDER.device, 9317, "ciphertext-looking junk", [])
    shuffled = [*PROVIDER.events, *other.events, junk]
    random.Random(1).shuffle(shuffled)
    b = export_bundle(
        PROVIDER.pubkey,
        shuffled,
        [],
        [*PROVIDER.reveals, Reveal(commitment_id="a" * 64, plaintext="x")],
    )
    assert b.events == BUNDLE.events  # foreign + unreadable events dropped, seq order restored
    assert b.reveals == BUNDLE.reveals


# ---------------------------------------------------------------- SEP-3.15


def _mutate(kind: str, i: int, j: int) -> tuple[list[NostrEvent], str]:
    evs = list(BUNDLE.events)
    if kind == "drop":
        del evs[i]
    elif kind == "swap":
        evs[i], evs[j] = evs[j], evs[i]
    elif kind == "edit":
        e = evs[i]
        evs[i] = e.model_copy(
            update={"content": e.content.replace("0.5", "0.4").replace("1.0", "0.9")}
        )
    elif kind == "duplicate":
        evs.insert(i, evs[j])
    elif kind == "replace-with-forged":
        # a validly signed event by the provider key that is NOT the original (fork at that slot)
        sig = PROVIDER.next_signal()  # wrong slot, but signed by the provider: chain must expose it
        from sa_core.signals import build_signal_event

        evs[i] = build_signal_event(PROVIDER.device, sig, PROVIDER.delegation, created_at=1)
    return evs, kind


@given(
    kind=st.sampled_from(["drop", "swap", "edit", "duplicate", "replace-with-forged"]),
    i=st.integers(0, N - 1),
    j=st.integers(0, N - 1),
)
def test_sep_3_15_dropped_swapped_or_edited_signals_are_never_verified_as_complete(
    kind: str, i: int, j: int
) -> None:
    if kind == "swap" and i == j:
        return
    if kind == "edit" and not any(s in BUNDLE.events[i].content for s in ("0.5", "1.0")):
        return  # committed slots hold only a digest; the edit would be a no-op
    if kind == "duplicate" and i == j:
        return
    evs, _ = _mutate(kind, i, j)
    bad = BUNDLE.model_copy(update={"events": tuple(evs)})
    check = check_bundle(bad, expected_heads=HEAD)  # head known: even a dropped tail is caught
    assert not check.complete, f"{kind} {i} {j} went undetected: {dump_check(check)}"


@given(i=st.integers(0, N - 2))
def test_sep_3_15_dropping_any_non_tail_item_is_caught_without_an_external_head(i: int) -> None:
    evs = list(BUNDLE.events)
    del evs[i]
    check = check_bundle(BUNDLE.model_copy(update={"events": tuple(evs)}))
    assert not check.complete and f"{PROVIDER.strategy_id}:{i + 1}" in check.gaps
    assert not check.head_checked


def test_sep_3_15_tail_truncation_needs_an_external_head() -> None:
    cut = BUNDLE.model_copy(update={"events": BUNDLE.events[:-1]})
    assert check_bundle(cut).complete  # honest limit: nothing inside the bundle shows the tail
    caught = check_bundle(cut, expected_heads=HEAD)
    assert not caught.complete and any("tail truncated" in p for p in caught.problems)
    ahead = check_bundle(
        BUNDLE, expected_heads={PROVIDER.strategy_id: (N - 1, PROVIDER.events[-2].id)}
    )
    assert any("ahead" in p for p in ahead.problems)
    wrong = check_bundle(BUNDLE, expected_heads={PROVIDER.strategy_id: (N, "f" * 64)})
    assert any("head ref" in p for p in wrong.problems)


def test_sep_3_15_problems_are_specific() -> None:
    evs = list(BUNDLE.events)
    reordered = BUNDLE.model_copy(update={"events": (evs[1], evs[0], *evs[2:])})
    assert any("not in seq order" in p for p in check_bundle(reordered).problems)
    forked = BUNDLE.model_copy(update={"events": (*evs, evs[2])})
    assert any("appears 2 times" in p for p in check_bundle(forked).problems)
    no_start = BUNDLE.model_copy(update={"events": tuple(evs[1:])})
    assert f"{PROVIDER.strategy_id}:1" in check_bundle(no_start).gaps
    edited = evs[0].model_copy(update={"content": evs[0].content.replace("0.5", "0.6")})
    assert any(
        "invalid event" in p
        for p in check_bundle(BUNDLE.model_copy(update={"events": (edited, *evs[1:])})).problems
    )
    assert check_bundle(BUNDLE).signature_failures == 0
    assert (
        check_bundle(BUNDLE.model_copy(update={"events": (edited, *evs[1:])})).signature_failures
        == 1
    )
    stranger = DeviceKey.generate()
    from sa_core.signals import build_signal_event

    foreign = build_signal_event(
        stranger,
        PROVIDER.next_signal(),
        sign_delegation(SAKey.generate(), stranger.pubkey_hex, 10**10),
    )
    assert any(
        "not the provider" in p
        for p in check_bundle(BUNDLE.model_copy(update={"events": (*evs, foreign)})).problems
    )
    assert (
        check_bundle(BUNDLE.model_copy(update={"events": (*evs, foreign)})).signature_failures == 1
    )


def test_bundle_rejects_bad_delegation_unknown_kinds_and_bad_reveals() -> None:
    evs = list(BUNDLE.events)
    no_tag = build_event(PROVIDER.device, 9317, evs[0].content, [])
    no_tag_check = check_bundle(BUNDLE.model_copy(update={"events": (no_tag, *evs[1:])}))
    assert any("delegation invalid" in p for p in no_tag_check.problems)
    assert no_tag_check.signature_failures == 1
    forged = sign_delegation(SAKey.generate(), PROVIDER.pubkey, 10**10)
    wrong_tag = build_event(
        PROVIDER.device,
        9317,
        evs[0].content,
        [delegation_tag(forged.model_copy(update={"nostr_pubkey": PROVIDER.pubkey}))],
    )
    assert not check_bundle(BUNDLE.model_copy(update={"events": (wrong_tag, *evs[1:])})).complete
    other_kind = build_event(PROVIDER.device, 1, "hi", [delegation_tag(PROVIDER.delegation)])
    assert any(
        "does not carry" in p
        for p in check_bundle(BUNDLE.model_copy(update={"events": (*evs, other_kind)})).problems
    )
    junk_signal = build_event(PROVIDER.device, 9317, "{}", [delegation_tag(PROVIDER.delegation)])
    assert any(
        "not a valid signal" in p
        for p in check_bundle(BUNDLE.model_copy(update={"events": (*evs, junk_signal)})).problems
    )
    junk_commit = build_event(PROVIDER.device, 9318, "{}", [delegation_tag(PROVIDER.delegation)])
    assert any(
        "commitment" in p
        for p in check_bundle(BUNDLE.model_copy(update={"events": (*evs, junk_commit)})).problems
    )
    bad_reveal = BUNDLE.model_copy(
        update={"reveals": (Reveal(commitment_id=PROVIDER.events[3].id, plaintext='{"x": 1}'),)}
    )
    c = check_bundle(bad_reveal)
    assert (
        any("reveal of commitment" in p for p in c.problems)
        and PROVIDER.events[3].id in c.unrevealed
    )
    ghost = BUNDLE.model_copy(
        update={"reveals": (*BUNDLE.reveals, Reveal(commitment_id="e" * 64, plaintext="x"))}
    )
    assert any("not in the bundle" in p for p in check_bundle(ghost).problems)
    revoked = check_bundle(
        BUNDLE,
        revocations=[
            __import__("sa_core.delegation", fromlist=["x"]).sign_revocation(
                PROVIDER.sa, PROVIDER.pubkey, 0
            )
        ],
    )
    assert not revoked.complete


# ---------------------------------------------------------------- SEP-3.14


def test_sep_3_14_report_from_an_imported_bundle_equals_the_report_from_relay_events(
    tmp_path: Path,
) -> None:
    relay_events = list(PROVIDER.events)
    random.Random(7).shuffle(relay_events)  # relays return events in any order
    from_relays = export_bundle(PROVIDER.pubkey, relay_events, [], PROVIDER.reveals, exported_at=5)
    write_bundle(tmp_path / "history.json", from_relays)
    offline = read_bundle(tmp_path / "history.json")  # no relay involved from here on
    anchored = anchor_all(list(offline.events), delay_s=0)
    candles = {"BINANCE:BTCUSDT": FIXTURE_CANDLES}
    a = compute(from_relays, candles, anchored=anchored).to_json()
    b = compute(offline, candles, anchored=anchored).to_json()
    assert a == b and a.endswith("\n")
