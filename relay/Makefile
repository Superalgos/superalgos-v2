.PHONY: build check test vendor
build:
	go build -o bin/sarelay ./cmd/sarelay

# The repo's Go quality gate (ARCHITECTURE.md): gofmt, go vet, go test -race.
check:
	@test -z "$$(gofmt -l cmd internal)" || (gofmt -l cmd internal; echo "gofmt needed"; exit 1)
	go vet ./...
	go test -race -count=1 ./...

test:
	go test -race -count=1 -cover ./...

# Re-create third_party/khatru from the module cache plus khatru.patch (needs network once).
vendor:
	./prepare.sh
