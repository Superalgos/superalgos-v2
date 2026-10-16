"""sa gov propose | vote | tally against an in-process relay and the fixture oracle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_support import Cli, ThreadedRelay
from sa_cli import cmd_gov
from sa_core.models import PROPOSAL_KIND, VOTE_KIND, AnchorProof


def propose(demo: Cli, relay: ThreadedRelay, *extra: str) -> str:
    out = demo.ok(
        "gov",
        "propose",
        "--title",
        "Fund the dashboard",
        "--option",
        "yes",
        "--option",
        "no",
        "--relay",
        relay.url,
        *extra,
    )
    return out.split()[1].rstrip(":")


def test_parse_duration() -> None:
    assert (
        cmd_gov.parse_duration("90s") == 90
        and cmd_gov.parse_duration("2h") == 7200
        and cmd_gov.parse_duration("1.5d") == 129600
    )
    with pytest.raises(Exception, match="90s, 30m, 12h or 3d"):
        cmd_gov.parse_duration("soon")


def test_propose_vote_and_a_demo_tally_weighted_by_fixture_balance(
    demo: Cli, relay: ThreadedRelay, tmp_path: Path
) -> None:
    pid = propose(demo, relay, "--deadline-in", "1h")
    assert len(relay.events(PROPOSAL_KIND)) == 1 and relay.events(PROPOSAL_KIND)[0].id == pid
    demo.ok("gov", "vote", pid, "--option", "1", "--relay", relay.url)
    assert len(relay.events(VOTE_KIND)) == 1
    out = tmp_path / "tally.json"
    text = demo.ok("gov", "tally", pid, "--relay", relay.url, "--balances", demo.root / "balances.json", "--delay-d", "0",
                   "--demo-trust-created-at", "-o", out)  # fmt: skip
    assert "DEMO tally" in text and "tally written" in text
    result = json.loads(out.read_text())
    assert result["totals"] == ["0", str(1000 * 10**18)] and result["complete"] is True
    assert result["voters"][0]["option"] == 1


def test_a_real_tally_counts_nothing_until_a_vote_is_anchored(
    demo: Cli, relay: ThreadedRelay
) -> None:
    pid = propose(demo, relay)
    demo.ok("gov", "vote", pid, "--option", "0", "--relay", relay.url)
    out = demo.ok(
        "gov",
        "tally",
        pid,
        "--relay",
        relay.url,
        "--balances",
        demo.root / "balances.json",
        "--delay-d",
        "0",
    )
    result = json.loads(out)
    assert result["totals"] == ["0", "0"] and result["rejected"]  # unanchored votes never count


def test_stamping_a_vote_publishes_an_attestation(
    demo: Cli, relay: ThreadedRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeOts:
        def stamp(self, digest: bytes) -> AnchorProof:
            return AnchorProof(kind="ots", digest=digest.hex(), data="AAAA")

    monkeypatch.setattr(cmd_gov, "OtsAnchor", FakeOts)
    pid = propose(demo, relay)
    out = demo.ok("gov", "vote", pid, "--option", "0", "--relay", relay.url, "--stamp-ots")
    assert (
        "OpenTimestamps proof submitted" in out
        and len([e for e in relay.events() if e.kind == 1040]) == 1
    )

    class Down:
        def stamp(self, digest: bytes) -> AnchorProof:
            raise OSError("calendar unreachable")

    monkeypatch.setattr(cmd_gov, "OtsAnchor", Down)
    assert "OpenTimestamps stamping failed" in demo.fail(
        "gov", "vote", pid, "--option", "0", "--relay", relay.url, "--stamp-ots"
    )


def test_gov_errors_are_short(demo: Cli, relay: ThreadedRelay) -> None:
    assert "invalid proposal" in demo.fail(
        "gov", "propose", "--title", "x", "--option", "only", "--relay", relay.url
    )
    assert "90s, 30m" in demo.fail(
        "gov",
        "propose",
        "--title",
        "x",
        "--option",
        "a",
        "--option",
        "b",
        "--deadline-in",
        "soon",
        "--relay",
        relay.url,
    )
    assert "invalid vote" in demo.fail(
        "gov", "vote", "nothex", "--option", "0", "--relay", relay.url
    )
    assert "not found" in demo.fail(
        "gov", "tally", "ab" * 32, "--relay", relay.url, "--balances", demo.root / "balances.json"
    )
    assert "not found" in demo.fail(
        "gov",
        "tally",
        "ab" * 32,
        "--relay",
        relay.url,
        "--balances",
        demo.root / "balances.json",
        "--demo-trust-created-at",
    )


def test_m2_a_proposer_below_the_minimum_power_cannot_be_tallied(
    demo: Cli, relay: ThreadedRelay
) -> None:
    pid = propose(demo, relay)
    balances = demo.root / "balances.json"
    # the demo identity holds 1000 SA: exactly the default bar
    demo.ok("gov", "tally", pid, "--relay", relay.url, "--balances", balances, "--delay-d", "0")
    err = demo.fail(
        "gov",
        "tally",
        pid,
        "--relay",
        relay.url,
        "--balances",
        balances,
        "--min-proposer-power",
        "1001",
    )
    assert "holds 1000 SA, below the 1001 SA needed to propose" in err
    err = demo.fail(
        "gov", "tally", pid, "--relay", relay.url, "--balances", balances, "--min-proposer-power", "1001",
        "--demo-trust-created-at",
    )  # fmt: skip
    assert "below the 1001 SA" in err
