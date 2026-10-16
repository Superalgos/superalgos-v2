"""Anchor verifiers from the same ``--tsa-config`` JSON the ``sa`` CLI reads (trust bundle, k)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from sa_api.config import ConfigError
from sa_core.anchors import Anchor, verifier_map
from sa_core.tsa import TrustStore, TsaAnchor, TsaError


class _TsaFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    k: int = 2
    trust_dir: Path | None = None


def tsa_verifiers(config: Path | None) -> dict[str, Anchor]:
    """kind -> verifier for TSA proofs: the bundled public roots, or the trust directory of ``config``."""
    k, trust_dir = 2, None
    if config is not None:
        try:
            parsed = _TsaFile.model_validate_json(Path(config).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read the TSA config {config}: {exc.strerror}") from exc
        except ValidationError as exc:
            raise ConfigError(f"TSA config {config} is invalid: {exc.errors()[0]['msg']}") from exc
        k, trust_dir = parsed.k, parsed.trust_dir
    try:
        trust = TrustStore.load(trust_dir) if trust_dir is not None else TrustStore.load()
    except TsaError as exc:
        raise ConfigError(f"cannot load the TSA trust bundle: {exc}") from exc
    return verifier_map(TsaAnchor(trust, k=k))
