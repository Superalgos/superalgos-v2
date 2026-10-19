"""``sa exchange add binance-testnet``: store the follower's own exchange API key, encrypted (SEP-3.17)."""

from __future__ import annotations

import argparse

from pydantic import SecretStr

from sa_cli.home import CliError, Home
from sa_core.keys import ExchangeCredentials, NotEd25519Error, normalize_ed25519_secret

BINANCE_TESTNET = "binance-testnet"
KEY_ENV = "SA_BINANCE_TESTNET_API_KEY"
SECRET_ENV = "SA_BINANCE_TESTNET_API_SECRET"  # noqa: S105 - the name of an environment variable, not a secret


def add(args: argparse.Namespace, home: Home) -> int:
    """Read key and secret from the two environment variables (non-interactive) or prompt, then encrypt both."""
    key, secret = home.environ.get(KEY_ENV), home.environ.get(SECRET_ENV)
    if key and secret:
        source = "environment"
    elif key or secret:
        raise CliError(f"set both {KEY_ENV} and {SECRET_ENV}, or neither to be prompted")
    else:
        key = home.ask_secret("Binance testnet API key")
        secret = home.ask_secret("Binance testnet API secret")
        source = "prompt"
    key, secret = key.strip(), secret.strip()
    if not key or not secret:
        raise CliError("the API key and secret must not be empty")
    try:
        secret = normalize_ed25519_secret(secret)
    except NotEd25519Error as exc:
        raise CliError(str(exc)) from None
    path = home.save_exchange(
        args.exchange, ExchangeCredentials(api_key=SecretStr(key), api_secret=SecretStr(secret))
    )
    print(f"stored {args.exchange} credentials (from {source}), encrypted, in {path}")
    return 0
