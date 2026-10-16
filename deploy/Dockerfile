# syntax=docker/dockerfile:1
# One image for every service of the demo network (relay, TSA farm, setup, providers, followers, API).
# Build from the repo root:  docker build -f deploy/Dockerfile -t superalgos-v2:demo .

# Base images are pinned by digest (the tag is only a label); refresh them deliberately, see the README.
FROM oven/bun:1-alpine@sha256:629e17411f1f129dbec3af78d5af9c9f2a937435c80349437206c6b0b7422373 AS web
WORKDIR /src/web
COPY web/package.json web/bun.lock ./
RUN bun install --frozen-lockfile
COPY web/ ./
# the strategy designer imports the engine's schema and examples (web/src/lib/designer)
COPY packages/sa-engine/schema /src/packages/sa-engine/schema
COPY packages/sa-engine/examples /src/packages/sa-engine/examples
RUN bun run build

FROM golang:1.27-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS relay
WORKDIR /src/relay
COPY relay/ ./
RUN CGO_ENABLED=0 go build -o /out/sarelay ./cmd/sarelay

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock .python-version ./
COPY packages packages
COPY apps apps
RUN uv sync --frozen --all-packages --no-dev
COPY --from=relay /out/sarelay /usr/local/bin/sarelay
COPY --from=web /src/web/dist /app/web-dist
ENV PATH="/opt/venv/bin:$PATH" SA_DATA_DIR=/data
RUN useradd --create-home sa && mkdir /work /data && chown sa /work /data
USER sa
