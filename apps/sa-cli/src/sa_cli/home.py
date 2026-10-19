"""The user's key directory and identity files.

Layout (``--home`` or ``$SA_HOME``, default ``~/.superalgos``)::

    device.key.json   encrypted Nostr device key (scrypt + ChaCha20-Poly1305, mode 0600)
    sa.key.json       encrypted EVM key of a throwaway DEMO identity (absent for read-only or external identities)
    identity.json     public facts: mode, SA address, device pubkey
    delegation.json   the SA identity's signed delegation of the device key
    allocation.json   the last power allocation signed (its nonce only ever grows)
    exchange/<name>.key.json   encrypted exchange API key and secret, one envelope per exchange (mode 0600)
    profile.json      the follower's own settings (plain JSON, no secrets; absent means the defaults)

The passphrase comes from ``$SA_PASSPHRASE`` or an interactive prompt. Nothing here ever prints a secret.
"""

from __future__ import annotations

import getpass
import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sa_core.keys import (
    SCRYPT_N_DEFAULT,
    DeviceKey,
    ExchangeCredentials,
    KeyStoreError,
    SAKey,
    load_device_key,
    load_exchange_credentials,
    load_sa_key,
    save_exchange_credentials,
)
from sa_core.models import Delegation, PowerAllocation


class CliError(Exception):
    """A user-facing failure. ``main`` prints it as ``sa: error: ...`` and exits with ``code``."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


class IdentityInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    v: int = 1
    mode: Literal["demo", "readonly", "external"]
    address: str | None = None  # SA address (lowercase 0x...)
    device_pubkey: str | None = None


class Home:
    def __init__(
        self,
        root: Path,
        *,
        environ: Mapping[str, str] | None = None,
        prompt: Callable[[str], str] = getpass.getpass,
        interactive: Callable[[], bool] = sys.stdin.isatty,
    ) -> None:
        self.root = Path(root).expanduser()
        self.environ = os.environ if environ is None else environ
        self._prompt, self._interactive = prompt, interactive
        self._cached: str | None = None

    # ------------------------------------------------------------------ paths
    @property
    def device_path(self) -> Path:
        return self.root / "device.key.json"

    @property
    def sa_path(self) -> Path:
        return self.root / "sa.key.json"

    @property
    def identity_path(self) -> Path:
        return self.root / "identity.json"

    @property
    def delegation_path(self) -> Path:
        return self.root / "delegation.json"

    @property
    def allocation_path(self) -> Path:
        return self.root / "allocation.json"

    @property
    def profile_path(self) -> Path:
        return self.root / "profile.json"

    def exchange_path(self, name: str) -> Path:
        return self.root / "exchange" / f"{name}.key.json"

    # ------------------------------------------------------------------ secrets
    @property
    def scrypt_n(self) -> int:
        """Key-file KDF cost. ``$SA_SCRYPT_N`` lowers it for tests and demos only; files record the cost used."""
        raw = self.environ.get("SA_SCRYPT_N")
        if raw is None:
            return SCRYPT_N_DEFAULT
        try:
            return int(raw)
        except ValueError as exc:
            raise CliError(
                f"SA_SCRYPT_N={raw!r} is not an integer; unset it or use a power of two such as 131072"
            ) from exc

    def passphrase(self, *, confirm: bool = False) -> str:
        env = self.environ.get("SA_PASSPHRASE")
        if env:
            return env
        if not self._interactive():
            raise CliError(
                "no passphrase: set SA_PASSPHRASE, or run `sa` in a terminal to be asked for it"
            )
        if self._cached is not None and not confirm:
            return self._cached
        first = self._prompt("Key passphrase: ")
        if confirm and self._prompt("Repeat passphrase: ") != first:
            raise CliError("the two passphrases differ; nothing was written")
        self._cached = first
        return first

    def ask_secret(self, label: str) -> str:
        """Prompt for a secret without echo. Needs a terminal."""
        if not self._interactive():
            raise CliError(
                f"no {label}: set its environment variable, or run `sa` in a terminal to be asked for it"
            )
        return self._prompt(f"{label}: ")

    def save_exchange(self, name: str, credentials: ExchangeCredentials) -> Path:
        path = self.exchange_path(name)
        if path.exists():
            raise CliError(
                f"{path} already exists; delete it first to store new {name} credentials"
            )
        try:
            save_exchange_credentials(
                path, name, credentials, self.passphrase(confirm=True), n=self.scrypt_n
            )
        except KeyStoreError as exc:
            raise CliError(str(exc)) from exc
        return path

    def load_exchange(self, name: str) -> ExchangeCredentials:
        path = self.exchange_path(name)
        if not path.exists():
            raise CliError(f"no {name} credentials; run `sa exchange add {name}`")
        try:
            return load_exchange_credentials(path, name, self.passphrase())
        except KeyStoreError as exc:
            raise CliError(str(exc)) from exc

    def load_device(self) -> DeviceKey:
        if not self.device_path.exists():
            raise CliError(f"no device key at {self.device_path}; run `sa keys init` first")
        try:
            return load_device_key(self.device_path, self.passphrase())
        except KeyStoreError as exc:
            raise CliError(str(exc)) from exc

    def load_sa(self) -> SAKey:
        if not self.sa_path.exists():
            info = self.identity()
            what = "no identity" if info is None else f"a {info.mode} identity"
            raise CliError(
                f"this action signs with the SA key, but this home has {what} without a key file; "
                "run `sa identity --demo` for a throwaway demo identity"
            )
        try:
            return load_sa_key(self.sa_path, self.passphrase())
        except KeyStoreError as exc:
            raise CliError(str(exc)) from exc

    # ------------------------------------------------------------------ identity files
    def identity(self) -> IdentityInfo | None:
        if not self.identity_path.exists():
            return None
        try:
            return IdentityInfo.model_validate_json(self.identity_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CliError(
                f"{self.identity_path} is damaged ({exc}); delete it and run `sa identity` again"
            ) from exc

    def save_identity(self, info: IdentityInfo) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity_path.write_text(info.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def load_delegation(self, device: DeviceKey) -> Delegation:
        if not self.delegation_path.exists():
            raise CliError(
                f"no delegation at {self.delegation_path}; run `sa identity --demo`, "
                "or `sa identity --import-delegation FILE` with one signed by your SA wallet"
            )
        try:
            d = Delegation.model_validate_json(self.delegation_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CliError(f"{self.delegation_path} is not a delegation: {exc}") from exc
        if d.nostr_pubkey != device.pubkey_hex:
            raise CliError(
                f"the delegation is for device key {d.nostr_pubkey[:12]}.., this home's key is {device.pubkey_hex[:12]}..; "
                "create a new delegation"
            )
        if d.expires_at <= time.time():
            raise CliError(
                f"the delegation expired at {d.expires_at}; run `sa identity --demo --renew` to sign a new one"
            )
        return d

    def last_allocation(self) -> PowerAllocation | None:
        if not self.allocation_path.exists():
            return None
        try:
            return PowerAllocation.model_validate_json(
                self.allocation_path.read_text(encoding="utf-8")
            )
        except ValueError as exc:
            raise CliError(f"{self.allocation_path} is not an allocation: {exc}") from exc
