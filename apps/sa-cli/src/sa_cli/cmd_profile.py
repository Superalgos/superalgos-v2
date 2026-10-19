"""``sa profile show`` / ``sa profile set``: the follower's own settings (SEP-14.5).

The profile is ``<home>/profile.json``: plain JSON, a closed schema, no secrets. A missing file means the defaults.
"""

from __future__ import annotations

import argparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sa_cli.home import CliError, Home
from sa_engine.protection import DISASTER_STOP_MAX_PCT, check_disaster_stop_pct

DEFAULT_DISASTER_STOP_PCT = 10.0
SETTINGS = {"disaster-stop-pct": "disaster_stop_pct"}  # CLI name -> profile field


class FollowerProfile(BaseModel):
    """What this follower does by itself, whoever it follows. Unknown fields are refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    disaster_stop_pct: float = Field(
        default=DEFAULT_DISASTER_STOP_PCT,
        description="percent below the average entry where one stop-limit rests at the exchange; 0 = off",
    )

    @field_validator("disaster_stop_pct")
    @classmethod
    def _in_range(cls, value: float) -> float:
        return check_disaster_stop_pct(value)


def load_profile(home: Home) -> FollowerProfile:
    path = home.profile_path
    if not path.exists():
        return FollowerProfile()
    try:
        return FollowerProfile.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, OSError) as exc:
        raise CliError(
            f"{path} is not a valid follower profile ({_first_line(exc)}); fix it or delete it to use the defaults"
        ) from exc


def save_profile(home: Home, profile: FollowerProfile) -> None:
    home.root.mkdir(parents=True, exist_ok=True)
    home.profile_path.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _first_line(exc: Exception) -> str:
    """One line for the first problem pydantic found, without its documentation link."""
    if isinstance(exc, ValidationError) and exc.errors():
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return f"{where}: {first['msg']}" if where else str(first["msg"])
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _describe(profile: FollowerProfile, home: Home) -> list[str]:
    pct = profile.disaster_stop_pct
    source = "profile" if home.profile_path.exists() else "default"
    meaning = (
        "off: no stop rests at the exchange while you are offline"
        if pct == 0
        else f"one stop-limit rests {pct:g}% below your average entry"
    )
    return [f"disaster-stop-pct {pct:g} ({source}): {meaning}"]


def show(args: argparse.Namespace, home: Home) -> int:
    for line in _describe(load_profile(home), home):
        print(line)
    return 0


def set_value(args: argparse.Namespace, home: Home) -> int:
    field = SETTINGS.get(args.setting)
    if field is None:
        raise CliError(f"unknown setting {args.setting!r}; the settings are: {', '.join(SETTINGS)}")
    try:
        number = float(args.value)
    except ValueError:
        raise CliError(
            f"{args.setting} {args.value!r} is not a number; use a percent in (0, {DISASTER_STOP_MAX_PCT:g}], or 0 to turn it off"
        ) from None
    try:
        profile = load_profile(home).model_copy(update={field: number})
        profile = FollowerProfile.model_validate(
            profile.model_dump()
        )  # validate what is about to be written
    except ValidationError as exc:
        raise CliError(f"{args.setting} {args.value}: {_first_line(exc)}") from None
    save_profile(home, profile)
    for line in _describe(profile, home):
        print(line)
    return 0
