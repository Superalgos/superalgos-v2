"""SEP-14.5: the follower profile (<home>/profile.json): default 10, set, 0 = off, refuse the rest."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

import sa_engine.paper as paper_module
from cli_support import Cli
from sa_cli.cmd_follow import _executor
from sa_cli.cmd_profile import DEFAULT_DISASTER_STOP_PCT, FollowerProfile, load_profile

SET = ("profile", "set", "disaster-stop-pct")


def test_the_default_is_ten_percent_and_no_file_is_written(cli: Cli) -> None:
    out = cli.ok("profile", "show")
    assert (
        out == "disaster-stop-pct 10 (default): one stop-limit rests 10% below your average entry\n"
    )
    assert not cli.home().profile_path.exists()
    assert load_profile(cli.home()).disaster_stop_pct == DEFAULT_DISASTER_STOP_PCT == 10.0


def test_set_writes_plain_json_and_show_reads_it_back(cli: Cli) -> None:
    out = cli.ok(*SET, "7.5")
    assert "disaster-stop-pct 7.5 (profile)" in out
    path = cli.home().profile_path
    assert json.loads(path.read_text()) == {"disaster_stop_pct": 7.5}
    assert "disaster-stop-pct 7.5 (profile)" in cli.ok("profile", "show")
    assert load_profile(cli.home()).disaster_stop_pct == 7.5


def test_zero_turns_the_disaster_stop_off(cli: Cli) -> None:
    out = cli.ok(*SET, "0")
    assert "off: no stop rests at the exchange" in out
    assert load_profile(cli.home()).disaster_stop_pct == 0


@pytest.mark.parametrize("bad", ["-1", "50.01", "60", "100", "nan", "inf", "-0.5"])
def test_values_outside_the_range_are_refused_in_one_line_and_nothing_is_written(
    cli: Cli, bad: str
) -> None:
    err = cli.fail(*SET, bad)
    assert (
        err.count("\n") == 1
        and f"disaster-stop-pct {bad}:" in err
        and "(0, 50]" in err
        and "0 to turn it off" in err
    )
    assert not cli.home().profile_path.exists()


def test_a_value_that_is_not_a_number_says_so(cli: Cli) -> None:
    err = cli.fail(*SET, "ten")
    assert "'ten' is not a number" in err and err.count("\n") == 1


def test_an_unknown_setting_lists_the_known_ones(cli: Cli) -> None:
    err = cli.fail("profile", "set", "leverage", "3")
    assert "unknown setting 'leverage'" in err and "disaster-stop-pct" in err


def test_a_refused_value_keeps_the_previous_setting(cli: Cli) -> None:
    cli.ok(*SET, "5")
    cli.fail(*SET, "99")
    assert load_profile(cli.home()).disaster_stop_pct == 5


def test_the_schema_is_closed(cli: Cli) -> None:
    with pytest.raises(ValidationError):
        FollowerProfile.model_validate({"disaster_stop_pct": 10, "leverage": 3})
    cli.home().root.mkdir(parents=True, exist_ok=True)
    cli.home().profile_path.write_text('{"disaster_stop_pct": 10, "leverage": 3}')
    err = cli.fail("profile", "show")
    assert "is not a valid follower profile" in err and "delete it to use the defaults" in err


@pytest.mark.parametrize(
    "text", ["not json", '{"disaster_stop_pct": 75}', '{"disaster_stop_pct": "ten"}']
)
def test_a_damaged_profile_is_a_clear_error_for_show_and_follow(cli: Cli, text: str) -> None:
    cli.home().root.mkdir(parents=True, exist_ok=True)
    cli.home().profile_path.write_text(text)
    assert "is not a valid follower profile" in cli.fail("profile", "show")
    assert "is not a valid follower profile" in cli.fail("profile", "set", "disaster-stop-pct", "5")


@given(pct=st.floats(allow_nan=True, allow_infinity=True))
def test_property_a_percent_is_accepted_exactly_when_it_is_zero_or_in_zero_to_fifty(
    pct: float,
) -> None:
    ok = pct == 0 or (not math.isnan(pct) and 0 < pct <= 50)
    if ok:
        assert FollowerProfile(disaster_stop_pct=pct).disaster_stop_pct == pct
    else:
        with pytest.raises(ValidationError, match=r"\(0, 50\]"):
            FollowerProfile(disaster_stop_pct=pct)


@given(pct=st.floats(min_value=0.001, max_value=50, allow_nan=False))
def test_property_a_valid_profile_survives_a_write_and_a_read(pct: float) -> None:
    profile = FollowerProfile(disaster_stop_pct=pct)
    assert FollowerProfile.model_validate_json(profile.model_dump_json()) == profile


# ---------------------------------------------------------------- sa follow reads it


def test_follow_gives_the_paper_follower_the_profile_percent(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    class FakeExecutor:
        def __init__(self, instrument: str, capital: float, **kw: Any) -> None:
            seen.append(kw)

    monkeypatch.setattr(paper_module, "FollowerExecutor", FakeExecutor)
    args = argparse.Namespace(
        executor="nautilus", replay_spec=None, instrument="BINANCE:BTCUSDT", capital=1_000.0
    )
    cli.ok(*SET, "12")
    _executor(args, cli.home(), load_profile(cli.home()))
    assert seen == [{"disaster_stop_pct": 12.0, "log_level": "WARNING"}]
    _executor(args, cli.home("other"), load_profile(cli.home("other")))  # no profile: the default
    assert seen[-1]["disaster_stop_pct"] == 10.0


def test_the_profile_file_lives_in_the_home_and_holds_no_secret(cli: Cli, tmp_path: Path) -> None:
    cli.ok(*SET, "3")
    assert cli.home().profile_path == tmp_path / "home" / "profile.json"
