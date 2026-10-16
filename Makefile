# Superalgos v2: shared quality gates. Offline by default (uv --offline works from a warm cache).
UV ?= uv
PKGS := $(notdir $(wildcard packages/*) $(wildcard apps/*))
DIRS := $(wildcard packages/*) $(wildcard apps/*)
PYTHON_DIRS := $(foreach d,$(DIRS),$(if $(wildcard $(d)/pyproject.toml),$(d)))

.PHONY: sync lint typecheck test test-fast check hooks e2e e2e-online relay demo demo-smoke web-check perf

sync:
	$(UV) sync --all-packages

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck:
	@for d in $(PYTHON_DIRS); do \
		echo "mypy --strict $$d"; \
		$(UV) run mypy --strict $$d/src $$d/tests || exit 1; \
	done
	@echo "mypy --strict scripts"
	@$(UV) run mypy --strict scripts/demo_smoke.py
	@echo "mypy --strict tests/e2e"
	@MYPYPATH=packages/sa-core/tests:tests/e2e $(UV) run mypy --strict tests/e2e

# Full tests with the coverage gate (fail_under in pyproject.toml), per package.
test:
	@for d in $(PYTHON_DIRS); do \
		mod=$$(basename $$d | tr - _); \
		echo "pytest $$d (coverage: $$mod)"; \
		$(UV) run pytest $$d -o addopts="--strict-markers -ra -m 'not slow and not online'" --cov=$$mod --cov-report=term-missing:skip-covered || exit 1; \
	done

# Pre-commit speed: no coverage, stop at first failure, skip slow tests.
test-fast:
	@for d in $(PYTHON_DIRS); do \
		$(UV) run pytest $$d -x -q -m "not slow and not online" -p no:cacheprovider || exit 1; \
	done

check: lint typecheck test

# End-to-end across real processes: builds relay/ (Go), starts sarelay with fixture balances, a provider replaying a
# real week of BTCUSDT 1m data (downloaded once into $$SA_DATA_DIR, default ./.sa-data) and three followers. Minutes, not
# seconds; not part of `make check`. The live-TSA test needs the network: `make e2e-online`.
relay:
	$(MAKE) -C relay build

e2e:
	$(UV) run pytest tests/e2e -o addopts="--strict-markers -ra" -m "e2e and not online" -v -p no:cacheprovider

e2e-online:
	SA_ONLINE=1 $(UV) run pytest tests/e2e -o addopts="--strict-markers -ra" -m "e2e and online" -v -p no:cacheprovider

hooks:
	git config core.hooksPath scripts/hooks
	@echo "pre-commit hook enabled (scripts/hooks/pre-commit)"

# The product: the whole network and the dashboard on this machine, one command, Ctrl-C to stop (needs go and bun once).
demo:
	$(UV) run sa demo

# Starts `sa demo` headless for about a minute, hits every API endpoint, asserts real data, and shuts it down.
demo-smoke:
	$(UV) run python scripts/demo_smoke.py

# Dashboard checks (biome, tsc strict, bun tests with coverage, production build).
# Local-only performance tests (machine-speed budgets); never run in CI. See AGENTS.md.
perf:
	cd relay && SA_PERF=1 go test -count=1 -v -run 'TestPerf' ./...
	cd web && bun install --frozen-lockfile && SA_PERF=1 bun test tests/designer-page.test.tsx

web-check:
	cd web && bun install --frozen-lockfile && bun run lint && bun run typecheck && bun test --coverage && bun run build
