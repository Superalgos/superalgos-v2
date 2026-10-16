"""Canonical JSON and hashing helpers shared by every module.

Anything that is hashed, signed or compared byte-for-byte goes through `canonical_json`:
sorted keys, no whitespace, UTF-8 (not ASCII-escaped).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()
