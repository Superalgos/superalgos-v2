"""A local RFC 3161 TSA farm (three organisations, real CMS tokens, throwaway CAs) for offline e2e runs.

The implementation lives in `sa_core.tsa_lab` (the `sa demo` command uses the same farm).
"""

from __future__ import annotations

from sa_core.tsa_lab import FARM_ORGS as ORGS
from sa_core.tsa_lab import FARM_POLICY as POLICY
from sa_core.tsa_lab import tsa_farm

__all__ = ["ORGS", "POLICY", "tsa_farm"]
