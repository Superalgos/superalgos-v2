# sarelay alone: the Nostr relay as its own small image (build context = repo root).
#
#   docker buildx build --platform linux/arm64 -f deploy/relay.Dockerfile -t sarelay:test --load .
#
# Contents: the static sarelay binary on a pinned Alpine base (busybox supplies the healthcheck tools), a non-root user,
# and /data for the store. No Python, no Nautilus. The config is mounted at /etc/sarelay/sarelay.toml (see deploy/testrelay).
FROM golang:1.27-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS build
WORKDIR /src/relay
COPY relay/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/sarelay ./cmd/sarelay

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN adduser -D -u 10001 sarelay && mkdir -p /data /etc/sarelay && chown sarelay /data
COPY --from=build /out/sarelay /usr/local/bin/sarelay
USER sarelay
VOLUME /data
EXPOSE 7781
ENTRYPOINT ["sarelay"]
CMD ["-config", "/etc/sarelay/sarelay.toml"]
