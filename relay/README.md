# sarelay: the Superalgos v2 tiered relay

A standalone Go binary (khatru v0.19.1, Apache-2.0 repo) that stores and forwards signed Nostr events and delivers each
signal to each subscriber at `receipt + delay_fraction(tier with that provider) x timeframe`.
It never holds a key, and it never reads a private signal.

## Build and run

```
make build                       # bin/sarelay (needs Go 1.27)
bin/sarelay -config sarelay.toml # see sarelay.example.toml; -check validates and exits; -version
make check                       # gofmt + go vet + go test -race (the quality gate)
```

- Relay: `ws://127.0.0.1:7781` (config `listen`). NIP-11 document on the same port. Put TLS in front for public use.
- Admin: `http://127.0.0.1:7782/healthz` (JSON) and `/metrics` (Prometheus text), `Authorization: Bearer <token>`.
  The token is `$SARELAY_ADMIN_TOKEN`, else `token_file`, else generated once into `<storage_path>/admin.token` (mode 0600; never logged).
  Without the token every admin path answers 401; there is no state-changing HTTP endpoint (SEP-1.8).
- Logs: structured JSON (or text) on stderr via `log/slog`.
- `khatru` is vendored in `third_party/khatru` with `khatru.patch` (exposes the NIP-42 AUTH event under a lock; snapshots listeners in
  `notifyListeners`, which fixes a data race that `go test -race` finds in stock v0.19.1). `./prepare.sh` rebuilds it from the module cache.

## Test relay image (`deploy/relay.Dockerfile`, `deploy/testrelay/`)

The relay alone as a container image (about 37 MB, linux/arm64 or amd64; pinned Alpine base, static binary, non-root uid 10001, no Python).
Build context is the repo root:

```
docker buildx build --platform linux/arm64 -f deploy/relay.Dockerfile -t sarelay:test --load .
docker compose -f deploy/testrelay/docker-compose.yml up -d --build
curl -H 'Accept: application/nostr+json' http://127.0.0.1:7781     # NIP-11
docker run --rm -v "$PWD/deploy/testrelay/sarelay.toml:/etc/sarelay/sarelay.toml:ro" -v "$PWD/deploy/testrelay/balances:/etc/sarelay/balances:ro" sarelay:test -check -config /etc/sarelay/sarelay.toml
```

- The compose file publishes `127.0.0.1:${SARELAY_PORT:-7781}` only; the host's Kong TLS proxy reaches the relay there and serves
  `wss://relay.example.org`. The store lives in the named volume `relay-data` (`/data`). Restart policy `unless-stopped`; the healthcheck
  fetches the NIP-11 document with busybox `wget`.
- `deploy/testrelay/sarelay.toml` sets `public_url = "wss://relay.example.org"`: a client's NIP-42 AUTH event carries the URL it
  connected to as its `relay` tag and the relay compares it with `public_url`, so clients must connect through the public hostname
  (a client dialing `ws://127.0.0.1:...` gets no AUTH and is served as zero-power). `forwarded_header = "X-Forwarded-For"` makes the per-IP
  connection cap count the address Kong appends. Resource caps keep their public defaults.
- Chain: paper round, so a `fixture` chain reads `/etc/sarelay/balances/balances.json` (a directory mounted read-only from
  `${SARELAY_BALANCES_DIR:-deploy/testrelay/balances}`). Power is whatever the operator lists there (`{"0xaddress": "tokens"}`, read on every lookup);
  `min_provider_power` keeps its default of 10000, so a provider needs an entry of at least 10000. Allocation delays are `0s` (as in the test relay).
- The image build is gated in CI (`relay-image` job): linux/arm64, under 100 MB, no Python, non-root, `-check` accepts the config.

## Configuration (TOML)

`sarelay.example.toml` documents every key. Unknown keys are errors. The ones that shape behaviour:

| Key | Default | Meaning |
|---|---|---|
| `listen`, `public_url` | `127.0.0.1:7781` | websocket address; `public_url` is what NIP-42 `relay` tags must match behind a proxy |
| `storage_path` | `./sarelay-data` | badger database: events, pending releases, rule sets, allocations (synced writes) |
| `allocation_delay` | `192h` | D as the relay applies it, counted from relay receipt. Must be at least `core_delay + max_anchor_latency`, else start-up fails (see "Two clocks"). `0s` for demos, together with the next two |
| `core_delay` | `168h` | the D that sa-core applies, counted from the allocation's anchored time |
| `max_anchor_latency` | `24h` | the longest an allocation's anchor proof may lag the relay's receipt |
| `time_scale` | `1.0` | multiplies every delay; tests use `0.0166667` so a 60 s timeframe releases over 1 s |
| `[[chains]]` | none (required) | registry: `kind = "evm"` (`rpc`, `contract`, `decimals`) or `kind = "fixture"` (`file`); `disabled = true` skips one |
| `[rate]` | 5/s zero-power, 50/s powered | per-connection token buckets |
| `[limits]` | skew 10 s, 60 s to 1 week | signal clock skew, timeframe bounds, caps (next table) |

**Resource limits and registration cost** (added after the independent security review; every key is in `sarelay.example.toml`).
Over a cap the relay answers `blocked: ...` naming the key. Demos (`deploy/sarelay.compose.toml`, `sa demo`, the e2e config) set the
caps they must lift explicitly and permissively; a public relay keeps the defaults.

| Key | Default | Meaning |
|---|---|---|
| `limits.max_release_delay` | `168h` | `delay_fraction x timeframe` of every tier may not exceed it (rule set refused otherwise); the private commitment delay is checked against it at start-up |
| `limits.max_pending_per_provider`, `max_pending_total` | `5000`, `100000` | events whose releases have not fired (each is a timer, a lane entry and a metadata record); a signal over the cap is `blocked:` |
| `limits.max_scan_events` | `20000` | stored events one REQ or COUNT may examine; a request that reaches it ends there. Each 250 events examined also cost the connection one request token |
| `limits.max_conns_per_ip`, `forwarded_header` | `64`, empty | simultaneous websockets per client address, in-process (HTTP 429 over it). Behind a trusted proxy set `forwarded_header = "X-Forwarded-For"`: the right-most value is the client. `0` disables |
| `budget.max_anchor_events`, `max_proposal_events`, `max_vote_events`, `max_govdelegation_events`, `max_revocation_events` | `50000`, `5000`, `50000`, `20000`, `20000` | stored events of kinds 1040, 9330, 9331, 9333, 9334; at the budget a new one is `blocked:` |
| `budget.retention` | `2160h` | kinds 1040, 9330, 9331 older than this (by `created_at`) are deleted by an hourly sweep, freeing budget. Governance delegations and revocations are state: count budget only. `0s` keeps everything |
| `policy.min_provider_power` | `10000` | whole SA tokens the provider's SA address must hold for a rule set to be accepted; re-checked every `provider_recheck_interval` (a provider whose address falls below it is deregistered; a failed balance read removes nobody). `0` = free, for demos |
| `policy.min_proposer_power` | `10000` | the same for kind 9330 proposals (which also need a valid `sa-delegation`). `0` = delegation only |
| `policy.provider_idle_expiry`, `provider_recheck_interval` | `720h`, `10m` | a provider with no signal for this long is deregistered (it may register again), which frees its `max_providers` slot. There is no state-changing admin endpoint, so expiry is the removal path. `0s` never expires |
| `oracle.max_lookups_per_sec`, `lookup_burst` | `20`, `40` | relay-wide token bucket over chain reads. Over it a chain is not read: the last good balance serves for `max_stale`, an address never read counts as 0 |
| `oracle.backoff_base`, `backoff_max` | `2s`, `5m` | a chain that fails is not read again for `base x 2^(failures-1)` (at most `max`), for every address |

Content size limits (not configurable): 16 KiB for most kinds, 70,000 bytes for signals and commitments (NIP-44 payloads), 64 KiB for kind-1040 anchor attestations (a TSA proof with three tokens is about 27 KB).

Token power is the sum of the SA balance over all enabled chains, normalised to 18 decimals. A chain that errors keeps its last good
value for `oracle.max_stale`, then counts as 0, so errors never raise power. zkSync (chain 324, contract `0xe3d85bd3363b6f0f91351591f9a8fd0d0a1145ed`, symbol `SA`, read on-chain) is enabled in `sarelay.example.toml` like BSC and Ethereum, so the relay and `sa_core.chains` sum the same three chains; `tests/e2e/test_online_oracles.py` (`SA_ONLINE=1`) reads the same addresses through this relay's oracle (`cmd/sabalances`) and the Python one and asserts equal per-chain values and sums.

## Wire contract

Kinds (see `ARCHITECTURE.md` and `packages/sa-core/README.md`, which fixes the details below): 9317 signal, 9318 commitment, 9332
allocation, 37318 rule set, 9330 proposal (valid `sa-delegation` from an address holding `policy.min_proposer_power`), 9331 vote (needs a
well-formed `sa-delegation` tag), 9333 governance delegation (EIP-191 checked), 9334 delegation revocation (below), 1040 anchor
attestation (anyone, but its single `e` tag must name an event this relay stores), plus `policy.extra_kinds`. Everything else is refused (`blocked:`). NIP-42 AUTH is offered on
connect; negentropy (NIP-77) is off because it would be another read path.

**Delegation tag** `["sa-delegation", evm_address, expires_at, sig]`, sig = EIP-191 over `sa-delegation:{nostr_pubkey_hex}:{expires_at}`.
Signals, commitments, rule sets **and allocations** must carry a valid one (followers reject them otherwise); on the kind-22242 AUTH event
it binds the connection's device key to an SA address. On a kind 9332 it must name `content.address` itself: an allocation is accepted only
from a device key that address delegates. (The EIP-191 signature in the content is public once published, so without this rule a stranger
could re-wrap a victim's allocation in an event of his own, refresh the victim's rate limit and change which device key counts as the
victim's.)

**Revocation (kind 9334).** Content `{"v":1,"device_pubkey":hex,"effective_at":unix_seconds,"address":"0x..","sig":"0x.."}`, `sig` = EIP-191
by `address` over `sa-revocation:{device_pubkey}:{effective_at}` (`sa_core.delegation.revocation_message`; cross-checked in
`TestRevocationGoldenFromPython`). Any signer may publish it (the signature is the authority). The relay stores and persists it; from
`effective_at` on it refuses that device key under that address everywhere a delegation is read: signals, commitments, rule sets,
allocations, proposals, and NIP-42 AUTH. An AUTH by a revoked device is not rejected at the protocol level (khatru has no hook for that); it
simply earns no address, so the connection is served at the zero-power tier, and a connection authenticated before `effective_at` drops to
it at that moment. The earliest `effective_at` for a (address, device) pair wins; a later one is `duplicate:`. Kind 9334 events are stored
and queryable like any other.

**Signing messages** (EIP-191 `personal_sign` by the SA address, as `sa_core.power`). Cross-checked against events signed by the real
Python sa-core (`testdata/pyvectors.json`, regenerate with `testdata/gen_pyvectors.py`), including a Python NIP-44 ciphertext that go-nostr's
`nip44` must decrypt (a hard test failure if it does not).

| Event | Content JSON | Message signed by the SA address |
|---|---|---|
| 37318 rule set (tags `d` = provider, `sa-delegation`; signer = provider) | `{"v":1,"provider":hex,"sa_address":"0x..","timeframe":seconds,"tiers":[{"min_power":int,"delay_fraction":0.1},...],"sig":"0x.."}` or `"private":{"min_power":int}` instead of `tiers` | `sa-ruleset:{provider}:{timeframe}:{min@delay,...}` ascending by min_power, delay `%.4f`; private: `private@{min}` |
| 9332 allocation | `{"v":1,"address":"0x..","nonce":n,"governance":bps,"providers":{pubkey:bps>=1},"sig":"0x.."}` | `sa-allocation:{address}:{nonce}:{governance}:{pk}={bps},...` (sorted by pubkey; empty list leaves the last field empty) |
| 9317 public signal | JSON with `v: 2` and at least `timeframe` (integer seconds), `created_ms` and `bar_open_ms` (SEP-3.18; the relay requires the field, followers check it); event `created_at` near now | none: device key + valid `sa-delegation` from the provider's SA address |
| 9317 private signal | NIP-44 v2 ciphertext, exactly one `["p", recipient_pubkey]` tag (one event per recipient) | none |
| 9318 commitment | `{"v":1,"strategy_id","seq","prev","digest"}` | none |

`min_power` is in whole SA tokens. Thresholds are distinct and `delay_fraction` (at most 4 decimals) never decreases as power falls.
Rule sets have no nonce: the newest `created_at` wins.

**Rules.** A provider is registered once its rule set exists (accepted only when the SA signature verifies, `d` = signer = provider, the
delegation names `sa_address`, and `created_at` is newer than the current one and not more than `limits.skew` + 1 s ahead of the relay clock, since the newest wins and a far-future one would pin the rules; one SA address registers one provider key). Only a device key
delegated by a registered SA address may publish 9317/9318 (SEP-3.39). A public signal's `timeframe` must equal the one bound in the rule set
(SEP-3.40). Allocations: SA signature, a delegation tag from that address (above), shares sum to at most 10000 bps, nonce not below the highest
already accepted for that address; history stays queryable. An equal nonce with *different signed content* is SA-identity equivocation: both
are kept and the smallest event id among the effective ones wins; the same signed content again, in another event, is `duplicate:`. (Same rule as `sa_core.power.allocation_in_force`, tested in `internal/state`.) Equivocation is therefore possible only for the address's own delegated keys. Otherwise the highest-nonce allocation whose
delay D has passed is in force (SEP-3.22, SEP-3.32, SEP-3.33, SEP-3.35). Power with provider P = total power x
bps(P) / 10000. Tier index 0 is the best: the strongest tier whose `min_power` is at most that power, else the lowest-`min_power` tier.
Release delay = `round(fraction x 10000) x timeframe x 1000 / 10000` ms, as `sa_core.power.release_ms`. The `D` clock starts at relay
receipt (no anchor proof is checked here); see "Two clocks" below.

**Delivery.** One timer per distinct delay per signal; each fire goes only to connections of that delay. Fires are queued per lane, a lane being one provider at one delay: a single goroutine drains a lane in relay receipt order (a counter assigned under the write lock, stored with the release schedule), so two signals of one provider received in the same millisecond, an entry and its stop, reach every tier in the order the relay received them, also after a restart (`TestSameMillisecondOrder`, `TestConcurrentBurstSameOrderAcrossTiers`, `TestLane*`). Followers keep a reorder window as defence in depth. Live fan-out, stored REQ, `ids`
lookups and COUNT share one visibility rule, a `limit` counts visible events only, and an event with no recorded metadata is hidden.
**A kind-1040 attestation follows the event it attests**: it is visible to a connection exactly when its `e` target is, on every route, and
live it is sent to each tier together with the signal's release for that tier (not before), so its existence cannot tell a slow tier that a
signal arrived (`TestM6AnchorOfAGatedEventFollowsItsRelease`). Scheduled attestation fires are not re-armed after a restart; a reconnecting
follower reads them from storage.
The receipt time and the rule snapshot are written (synced) before the event becomes queryable, and pending releases are re-armed at
start-up from their original times (SEP-3.41). Private providers: only a connection whose device key equals the event's `p` tag and whose
allocated power meets `private.min_power` receives it; the provider's own SA identity always reads its own events. The relay refuses
private-provider signals that look like plaintext. Commitments of private providers reach entitled followers at once and everyone else
after `policy.private_commitment_delay_fraction` x timeframe.

**Zero power.** No AUTH, an invalid delegation, or no allocated power lands in the weakest public tier: slower, never blocked, never
earlier. Beyond the documented rate a client gets `CLOSED ... rate-limited: ...` (or a NOTICE for COUNT) and stays connected.

## Two clocks (SEP-3.35)

sa-core counts an allocation's delay D from its **anchored** time (`TimeBound.not_after` of its proof, an upper bound on when it was
published). The relay cannot verify anchor proofs and starts D at **receipt**. The anchored time is never before the receipt and
lags it by the anchor latency (seconds for a TSA, hours for OpenTimestamps), so with the same D the relay would put an allocation in
force **earlier** than sa-core: a follower could be served a better tier than the power sa-core credits. The relay is therefore
configured with `allocation_delay >= core_delay + max_anchor_latency` (defaults 192h = 168h + 24h) and refuses to start otherwise; it
may be later than sa-core by up to `max_anchor_latency`, never earlier (`TestRelayDelayIsNeverEarlierThanSaCore`). Demos run D = 0 on
both sides and set all three keys to `0s`.

## Threat model

1. The relay is trusted for timing and access, not for content: private signals are NIP-44 ciphertext it only stores and forwards.
2. An entitled follower can re-broadcast a signal; a tier sells a head start, not secrecy (accepted risk).
3. No signer, wallet or exchange key ever reaches the relay; it checks EIP-191 signatures and reads balances (`eth_call`) only (SEP-1.13). Every content schema is closed: a signal, commitment, allocation, rule set or governance delegation with a field outside its schema (`api_secret`, `private_key`, `mnemonic`, ...) is refused with `invalid: ... unknown field "..."` (`TestRelayRejectsSecretFieldsInContent`). EVM signing helpers live in `internal/evmsig/evmsigtest`, imported by tests only; `TestBinaryDoesNotLinkSigningHelpers` checks the binary does not link them.
4. A forged, expired or foreign delegation yields zero power, never a higher tier; chain errors never raise power.
5. Anonymous clients are served (SEP-3.42), so the websocket and NIP-11 endpoints are public by design; the admin port is the only HTTP surface and needs a token.
6. Per-connection buckets bound request and bad-publish rates, and a request that scans much of the store is billed for the work. The relay also
   has an in-process cap on websockets per client address (`limits.max_conns_per_ip`); that cap is only as good as the address it sees, so
   behind a reverse proxy set `limits.forwarded_header` and still keep the proxy's own rate and connection limits.
7. The release clock is the relay's wall clock; a provider cannot move it (`created_ms` and `created_at` must be near it) nor shorten delays (timeframe is bound).
8. A relay operator can read nothing private, but can drop, delay or censor events and can see who requests what; publish to several relays and keep the provider's own archive bundle.
9. A hostile provider key can spam valid signals up to the accepted-publish rate and `max_pending_*`; the SA address that delegated it can
   revoke it on the wire (kind 9334) or by expiry.
10. Storage is not encrypted at rest, but holds only public events and ciphertext.
11. **Resource exhaustion** (security review H2/M1/M5). Everything an unauthenticated client can make the relay keep or compute is bounded by a
    configured cap (see "Resource limits"): release horizon, unreleased events per provider and in total, stored events per
    anyone-may-publish kind with retention, one scan per request, websockets per address, and chain reads relay-wide with back-off. Registering a
    provider costs holding `min_provider_power` SA, re-checked periodically, and idle providers expire. Remaining exposure: an attacker who
    holds the minimum can still register up to `max_providers` identities (one per SA address); the demo values (0) remove that cost.
12. **Allocation and delegation integrity** (H1/M3). An allocation is only accepted from a device key its own address delegates, so re-wrapping
    a public allocation changes nothing and cannot touch the victim's rate limit; a compromised device key can be cut off on the wire by a
    revocation anyone may carry. A revocation is as strong as its signature: it cannot be forged, but a relay operator can still withhold
    it (item 8). Delegation expiry remains the fallback.
13. **Oracle** (M1). Only addresses with an allocation on this relay, or a registered provider's, are looked up; self-made identities cost no
    RPC. The lookup cap and back-off protect the public RPC endpoints the relay shares with its users; a flood of allocated addresses is slowed
    to `max_lookups_per_sec` and served from last-good values, never from a higher balance.
14. **Start-up** (L5). A stored directory record that is corrupt or no longer validates is skipped, logged (`quarantined a stored directory
    record`) and counted in `sarelay_quarantined_records`; the relay still starts. Rule-set, allocation and revocation writes put the
    directory record first and the event second, and undo the record if the event write fails; a crash between the two leaves a record without
    its event (it still applies and is rebuilt on restart).
15. **Supply chain** (M7). `go.mod` requires khatru v0.19.1, the version vendored in `third_party/khatru` (replace directive plus
    `khatru.patch`); the container build pins every base image by digest and `uv` to an exact version.

## Tests

`go test -race ./...` runs unit and property tests (golden EIP-191 vectors from Python), in-process integration tests with real websockets
(leak probes on every read route, tier delays over 120 signals, two providers, allocation rules and delay D, private provider with storage
inspection, zero-power throttling, restart, 300-subscriber load), and end-to-end tests against the built binary (SIGTERM, bad config, `kill -9`
with a pending release, 300 subscribers with relay RSS).
`internal/relay/hardening_test.go` holds one regression test per security-review finding (`TestH1*`, `TestH2*`, `TestM1*` ... `TestL5*`);
each fails on the relay as of commit 8d81319.
