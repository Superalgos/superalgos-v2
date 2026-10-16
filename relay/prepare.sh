#!/usr/bin/env bash
# Copy khatru from the Go module cache into third_party/ (gitignored) and apply our small patch (AuthEvent exposure, listener snapshot).
# Needs network once (go mod download). Idempotent.
set -euo pipefail
cd "$(dirname "$0")"
VER=v0.19.1
go mod download github.com/fiatjaf/khatru@$VER
SRC="$(go env GOMODCACHE)/github.com/fiatjaf/khatru@$VER"
rm -rf third_party && mkdir -p third_party/khatru
cp "$SRC"/*.go "$SRC"/go.mod "$SRC"/LICENSE third_party/khatru/
chmod -R u+w third_party
rm -f third_party/khatru/*_test.go
patch -s -p1 -d third_party/khatru < khatru.patch
echo "khatru $VER prepared with khatru.patch"
