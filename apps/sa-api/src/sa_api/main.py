"""``python -m sa_api`` / ``sa-api``: serve the read-only API (127.0.0.1:8787 by default)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from sa_api.app import create_app
from sa_api.config import ApiConfig, ConfigError

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8787


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sa-api",
        description="Superalgos v2 dashboard API: read-only, plus Sign-In with Ethereum under /api/auth.",
    )
    p.add_argument(
        "--config", type=Path, help="JSON file with the ApiConfig fields (see sa_api.config)"
    )
    p.add_argument(
        "--relay", action="append", help="relay URL, repeatable (default ws://127.0.0.1:7781)"
    )
    p.add_argument("--web-dist", type=Path, help="built dashboard to serve at / (web/dist)")
    p.add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST}: local only)"
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--proxy-headers",
        action="store_true",
        help="trust X-Forwarded-For/-Proto from --forwarded-allow-ips. Behind a TLS-terminating proxy this is what "
        "makes the session cookie `Secure` and the per-IP nonce limit see real client IPs; without it a proxied "
        "deployment serves a non-Secure cookie. Never enable it when clients can reach the API directly",
    )
    p.add_argument(
        "--forwarded-allow-ips",
        default=None,
        help="comma-separated proxy IPs trusted for X-Forwarded-* (default 127.0.0.1); only with --proxy-headers",
    )
    return p


def config_from(args: argparse.Namespace) -> ApiConfig:
    cfg = ApiConfig.load(args.config) if args.config else ApiConfig()
    update: dict[str, object] = {}
    if args.relay:
        update["relays"] = tuple(args.relay)
    if args.web_dist:
        update["web_dist"] = args.web_dist
    if args.proxy_headers:
        update["proxy_headers"] = True
    if args.forwarded_allow_ips:
        update["forwarded_allow_ips"] = args.forwarded_allow_ips
    return cfg.model_copy(update=update) if update else cfg


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = config_from(args)
        app = create_app(cfg, bind_host=args.host)
    except ConfigError as exc:
        print(f"sa-api: error: {exc}", file=sys.stderr)
        return 2
    print(
        f"sa-api on http://{args.host}:{args.port} (read-only; POST: /api/auth/* wallet sign-in, /api/strategy/validate, "
        "and /api/backtests/run on a loopback bind only)",
        flush=True,
    )
    if cfg.forwarded_allow_ips != "127.0.0.1" and not cfg.proxy_headers:
        print(
            "sa-api: warning: forwarded_allow_ips is set but proxy_headers is off; X-Forwarded-* is ignored",
            file=sys.stderr,
        )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        proxy_headers=cfg.proxy_headers,
        forwarded_allow_ips=cfg.forwarded_allow_ips if cfg.proxy_headers else None,
    )
    return 0


def run() -> None:  # pragma: no cover - console entry point
    sys.exit(main())
