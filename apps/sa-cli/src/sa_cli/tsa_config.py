"""Which TSAs to stamp with and which pinned roots to verify against.

Default: the three public endpoints and the roots bundled with sa-core. ``--tsa-config FILE`` replaces
them (tests and private deployments)::

    {"k": 2, "trust_dir": "/path/with/tsas.json+roots", "endpoints": [{"name": "a", "url": "http://...", "operator": "org"}]}
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from sa_cli.home import CliError
from sa_core.anchors import Anchor, verifier_map
from sa_core.tsa import (
    DEFAULT_ENDPOINTS,
    DEFAULT_MIN_GAP_S,
    Endpoint,
    TrustStore,
    TsaAnchor,
    TsaError,
    TsaStamper,
)


class TsaConfigFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    k: int = 2
    min_gap_s: float = DEFAULT_MIN_GAP_S  # below 15 s only loopback TSAs, or "lab": true
    lab: bool = False  # a throwaway TSA farm (the demo's, in a container): allows the short gap
    trust_dir: Path | None = None
    endpoints: tuple[Endpoint, ...] | None = None


@dataclass(frozen=True)
class TsaSettings:
    trust: TrustStore
    endpoints: tuple[Endpoint, ...]
    k: int
    min_gap_s: float = DEFAULT_MIN_GAP_S
    lab: bool = False

    def verifiers(self) -> dict[str, Anchor]:
        return verifier_map(TsaAnchor(self.trust, k=self.k))

    def stamper(self) -> TsaStamper:
        try:
            return TsaStamper(
                self.endpoints,
                self.trust,
                k=self.k,
                min_gap_s=self.min_gap_s,
                allow_fast_gap=self.lab,
            )
        except TsaError as exc:
            raise CliError(f"cannot set up TSA stamping: {exc}") from exc


def load_tsa_settings(path: Path | None) -> TsaSettings:
    cfg = TsaConfigFile()
    if path is not None:
        try:
            cfg = TsaConfigFile.model_validate_json(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CliError(f"TSA config {path} not found") from exc
        except (ValueError, ValidationError) as exc:
            raise CliError(f"TSA config {path} is invalid: {exc}") from exc
    try:
        trust = TrustStore.load(cfg.trust_dir) if cfg.trust_dir is not None else TrustStore.load()
    except TsaError as exc:
        raise CliError(f"cannot load the TSA trust bundle: {exc}") from exc
    return TsaSettings(trust, cfg.endpoints or DEFAULT_ENDPOINTS, cfg.k, cfg.min_gap_s, cfg.lab)
