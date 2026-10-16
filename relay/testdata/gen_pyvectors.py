"""Regenerate testdata/pyvectors.json with the real sa_core (Python) signing code.

Run from a checkout of the repo root (the uv workspace):
    uv run --frozen --package sa-core python relay/testdata/gen_pyvectors.py > relay/testdata/pyvectors.json

The Go relay tests parse these events with their own code, so a change in either side's wire
details (message texts, field names, tag shapes) fails a test. Keys are fixed throwaways.
"""

from __future__ import annotations

import json
import sys

from sa_core import power
from sa_core.delegation import delegation_tag, sign_delegation
from sa_core.governance import (
    build_gov_delegation_event,
    build_vote_event,
    sign_gov_delegation,
)
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import ALLOCATION_KIND, PrivateRule, Signal, Vote
from sa_core.nostr import build_auth_event, build_event
from sa_core.signals import build_commitment_event, build_private_signal_event, build_signal_event

FAR = 4_102_444_800  # year 2100
T0 = 1_700_000_000


def key(byte: int) -> bytes:
    return bytes([byte]) * 32


sa_pub = SAKey(key(0x11))  # public provider's SA identity
dev_pub = DeviceKey(key(0x22))  # its provider key
sa_priv = SAKey(key(0x55))  # private provider's SA identity
dev_priv = DeviceKey(key(0x44))
sa_fol = SAKey(key(0x66))  # a follower's SA identity
dev_fol = DeviceKey(key(0x33))
sa_other = SAKey(key(0x77))

d_pub = sign_delegation(sa_pub, dev_pub.pubkey_hex, FAR)
d_priv = sign_delegation(sa_priv, dev_priv.pubkey_hex, FAR)
d_fol = sign_delegation(sa_fol, dev_fol.pubkey_hex, FAR)

rules_pub = power.sign_ruleset(sa_pub, dev_pub.pubkey_hex, 3600, tiers=power.DEFAULT_TIERS)
rules_priv = power.sign_ruleset(
    sa_priv, dev_priv.pubkey_hex, 3600, private=PrivateRule(min_power=1000)
)
alloc = power.sign_allocation(
    sa_fol, 5, governance=3000, providers={dev_pub.pubkey_hex: 4000, dev_priv.pubkey_hex: 3000}
)
alloc_empty = power.sign_allocation(sa_other, 6, governance=10000)

sig = Signal(
    strategy_id="alpha",
    seq=1,
    prev=None,
    instrument="BINANCE:BTCUSDT",
    side="buy",
    action="open",
    size_fraction=0.25,
    order_type="market",
    timeframe=3600,
    created_ms=T0 * 1000,
)
gov = sign_gov_delegation(sa_fol, 3, {sa_other.address: 2500})
vote = Vote(proposal="ab" * 32, option=1, nonce=0)


def allocation_event(alloc: power.PowerAllocation, sa: SAKey) -> object:
    """Kind 9332 as the relay now requires it: the p tags of power.allocation_event plus the follower
    device key's sa-delegation tag from the allocating SA key, so only a key delegated by the allocating address can publish it."""
    tags = [("p", pk) for pk in sorted(alloc.providers)] + [
        delegation_tag(sign_delegation(sa, dev_fol.pubkey_hex, FAR))
    ]
    return build_event(dev_fol, ALLOCATION_KIND, alloc.model_dump_json(), tags, T0)


def ev(e: object) -> dict:
    return e.model_dump(mode="json")  # type: ignore[attr-defined]


out = {
    "generated_with": "sa_core (packages/sa-core) via uv run; see gen_pyvectors.py",
    "far_future": FAR,
    "keys": {
        "sa_public_address": sa_pub.address,
        "provider_public_pubkey": dev_pub.pubkey_hex,
        "sa_private_address": sa_priv.address,
        "provider_private_pubkey": dev_priv.pubkey_hex,
        "follower_address": sa_fol.address,
        "follower_pubkey": dev_fol.pubkey_hex,
        "follower_secret_hex": dev_fol.secret_bytes().hex(),
    },
    "events": {
        "ruleset_public": ev(power.ruleset_event(dev_pub, rules_pub, d_pub, created_at=T0)),
        "ruleset_private": ev(power.ruleset_event(dev_priv, rules_priv, d_priv, created_at=T0)),
        "allocation": ev(allocation_event(alloc, sa_fol)),
        "allocation_governance_only": ev(allocation_event(alloc_empty, sa_other)),
        "signal_public": ev(build_signal_event(dev_pub, sig, d_pub)),
        "signal_private": ev(build_private_signal_event(dev_priv, sig, d_priv, dev_fol.pubkey_hex)),
        "commitment": ev(build_commitment_event(dev_priv, sig, d_priv, created_at=T0)),
        "gov_delegation": ev(build_gov_delegation_event(dev_fol, gov, created_at=T0)),
        "vote": ev(build_vote_event(dev_fol, vote, d_fol, created_at=T0)),
        "auth": ev(
            build_auth_event(
                dev_fol, "ws://relay.test", "c0ffee", [delegation_tag(d_fol)], created_at=T0
            )
        ),
    },
}
json.dump(out, sys.stdout, indent=1, sort_keys=True)
print()
