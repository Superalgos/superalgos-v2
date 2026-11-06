# AGENTS.md

Instructions for any coding agent (and human) working in this repository. `CLAUDE.md` imports this file; other tools
read it directly. `README.md` says what the project is; `ARCHITECTURE.md` holds the seams between the parts.

## How work is defined: SEPs

Work is specified as **Superalgos Enhancement Proposals** in `seps/`. A SEP is one feature: motivation, design, status.
Its **criteria** (`SEP-N.k`) are the testable statements of done, each with the probe that proves it.

- Before building, find the SEP and criteria you are working on. If none exists, write the criteria first (unchecked,
  each with its probe) and get them reviewed. Code without a criterion is out of scope.
- A criterion is closed only by evidence from a tool run: a test, a command's output, a browser check. Record a one-line
  evidence note with the commit that closes it. "Should work" never closes anything.
- Code, tests and docs cite criteria as `SEP-16.2`. Keep those references accurate when you move code.
- Anti-criteria ("Anti: …") describe things that must never happen; their tests stay forever.
- Change the SEP in the same pull request as the code it describes.

## Setup and commands

Python ≥ 3.12 via [uv](https://docs.astral.sh/uv/), Go ≥ 1.27, [bun](https://bun.sh).

```sh
uv sync --all-packages   # every Python package into the project .venv
make hooks               # enable the pre-commit hook (lint, typecheck, fast tests)
make check               # ruff, mypy --strict, pytest with coverage gate, per package
make -C relay check      # gofmt, go vet, go test -race
make perf                # local-only latency budgets (relay release error, e2e delivery lateness, designer 200 nodes)
make web-check           # biome, tsc strict, bun test with coverage, production build
make e2e                 # real processes: relay, providers, followers, record
make demo-smoke          # `sa demo` headless, every API endpoint asserted
uv run sa demo           # the whole network locally, dashboard on 127.0.0.1:8787
```

- Performance tests (latency budgets that depend on machine speed: relay release error, the e2e delivery lateness, the
  200-node designer) are local only: `make perf` (sets `SA_PERF=1`). Run it before a PR that touches relay delivery or
  the designer. CI never sets `SA_PERF`, so those tests skip there; their correctness halves (nothing missing,
  nothing early, nodes render) stay in the normal suite. Test harness waits (for an OK, a delivery, a release) are a
  flat 30 s so they fire only on a real hang; never size one to the expected latency.
- Python always runs through `uv run` or the project `.venv`. Never a global interpreter, never `pip install`; add
  dependencies with `uv add`.
- Never skip or bypass the pre-commit hook. A change is ready when `make check`, `make -C relay check` and
  `make web-check` pass for the parts it touches, and CI passes.

## Code rules

- **Typed only.** Python: type hints throughout, Pydantic at data and I/O boundaries, `mypy --strict`, Ruff. TypeScript:
  `strict`, Biome. Go: gofmt and vet clean.
- **Tests come with the code.** New code targets 90% unit coverage; below 80% fails the build (the threshold lives in
  each package's config). Each feature also gets at least one integration test through its real boundaries.
- **Property tests for invariants.** Parsers, validators, serializers, signing, tier and tally logic get property tests
  (Hypothesis for Python, fast-check for TypeScript) next to example tests.
- **Replace, don't wrap.** When an internal API changes, move every caller and delete the old one in the same change. No
  compatibility shims.
- **Error messages** say what failed, the value that caused it, and what to do. No stack dumps, no "something went
  wrong".
- **No dynamic code.** No `eval`, `exec` or `new Function`. Strategy logic is the typed expression language.

## Invariants that protect users

These are the product. A change that weakens one is rejected, whatever else it does.

- No server, relay or shared service ever receives an exchange secret, wallet key or device key. Every wire schema is
  closed: unknown fields are refused.
- The SA address never signs hot; device keys sign under a delegation with an expiry.
- A track record is a pure function of signed, anchored history and public candles. Same input, byte-identical output.
- Signal verification runs inside the engine before any order. It is implemented once (`sa-core`), never duplicated in
  another language.
- A signal received as history (created before the follower subscribed) is recorded, never executed.
- Keys at rest are encrypted. Nothing writes a key, passphrase or exchange secret in plaintext, including logs and test
  fixtures.

## Boundaries

- The engine (Python, NautilusTrader) and the client (dashboard, wallet flows) are separate processes. The dashboard
  talks to the engine only through the HTTP API in `apps/sa-api`.
- The relay (`relay/`, Go) is trusted for timing and access, never for content.
- Deployment happens only from `main` through CI. Never deploy from a local checkout.
- Do not commit secrets, `.env` files, real balances, real keys or exchange data. Test identities are throwaway.
- No promises about the SA token's price, in code, docs or commit messages.

## Pull requests

One concern per pull request. The description names the SEP criteria it closes or changes, with their evidence, and
anything left open. Keep commit messages plain and specific.
