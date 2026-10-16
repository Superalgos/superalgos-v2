// Package protocol parses and validates the Superalgos v2 Nostr wire contracts the relay enforces:
// provider rule sets (37318), power allocations (9332) and signals (9317/9318).
// Everything here is pure: no clocks, no I/O.
package protocol

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/evmsig"
)

// Event kinds.
const (
	KindSignal     = 9317
	KindCommitment = 9318
	KindAllocation = 9332
	KindRuleSet    = 37318
	KindProposal   = 9330
	KindVote       = 9331
	KindGovDeleg   = 9333
	KindRevocation = 9334
	KindAnchor     = 1040
)

// MaxBPS is 100% in basis points.
const MaxBPS = 10000

// maxSafeInt is the largest integer every JSON implementation (JS, Python floats) represents exactly.
const maxSafeInt = 1<<53 - 1

var pubkeyRe = regexp.MustCompile(`^[0-9a-f]{64}$`)

// ValidPubkey reports whether s is a lowercase 32-byte hex Nostr public key.
func ValidPubkey(s string) bool { return pubkeyRe.MatchString(s) }

// Limits bound what the parsers accept.
type Limits struct {
	MinTimeframe     int64 // seconds
	MaxTimeframe     int64 // seconds
	MaxTiers         int
	MaxDelayFraction float64
	MaxAllocations   int // provider entries in one allocation
	// MaxReleaseDelay caps delay_fraction x timeframe of every tier at rule-set acceptance (0: no cap).
	MaxReleaseDelay time.Duration
}

// DefaultLimits are the production bounds (1 minute to 1 week).
func DefaultLimits() Limits {
	return Limits{MinTimeframe: 60, MaxTimeframe: 7 * 24 * 3600, MaxTiers: 16, MaxDelayFraction: 100, MaxAllocations: 256, MaxReleaseDelay: 7 * 24 * time.Hour}
}

// ---- rule sets (kind 37318) ----

// Tier is one rung of a provider's ladder. A follower whose allocated power is at least MinWei
// receives each signal Fraction x timeframe after the relay received it.
type Tier struct {
	MinPower string // whole SA tokens, canonical
	MinWei   *big.Int
	Fraction string // canonical decimal, at most 4 decimals
	FracRat  *big.Rat
}

// RuleSet is a provider's tier rules, accepted only when signed via its SA identity (SEP-3.24).
type RuleSet struct {
	Provider     string // Nostr pubkey (hex); also the event's d tag and signer
	SAAddress    string // SA address (lowercase 0x) that signed it
	TimeframeSec int64
	Tiers        []Tier // strongest (highest min_power) first; empty when Private
	Private      bool
	PrivateMin   string // whole SA tokens
	PrivateWei   *big.Int
	Sig          string
	EventID      string
	CreatedAt    int64 // event created_at; the newest rule set of a provider wins
}

type tierJSON struct {
	MinPower      Num `json:"min_power"`
	DelayFraction Num `json:"delay_fraction"`
}

type ruleSetJSON struct {
	V         int         `json:"v"`
	Provider  string      `json:"provider"`
	SAAddress string      `json:"sa_address"`
	Timeframe json.Number `json:"timeframe"`
	Tiers     []tierJSON  `json:"tiers"`
	Private   *struct {
		MinPower Num `json:"min_power"`
	} `json:"private"`
	Sig string `json:"sig"`
}

// Message is the exact text the SA address signs (EIP-191), as sa_core.power.ruleset_message:
//
//	sa-ruleset:{provider}:{timeframe}:{min@delay,...}   tiers ascending by min_power, delay as %.4f
//	sa-ruleset:{provider}:{timeframe}:private@{min}
func (r *RuleSet) Message() string {
	if r.Private {
		return RuleSetMessage(r.Provider, r.TimeframeSec, "private@"+r.PrivateMin)
	}
	parts := make([]string, 0, len(r.Tiers))
	for i := len(r.Tiers) - 1; i >= 0; i-- { // r.Tiers is strongest first; the text is ascending
		t := r.Tiers[i]
		parts = append(parts, t.MinPower+"@"+fixed4(t.FracRat))
	}
	return RuleSetMessage(r.Provider, r.TimeframeSec, strings.Join(parts, ","))
}

// RuleSetMessage builds the signing text from its parts.
func RuleSetMessage(provider string, timeframe int64, rules string) string {
	return fmt.Sprintf("sa-ruleset:%s:%d:%s", provider, timeframe, rules)
}

// fixed4 renders a rational with at most 4 decimals as Python's "%.4f" does.
func fixed4(r *big.Rat) string {
	u := new(big.Int).Quo(new(big.Int).Mul(r.Num(), big.NewInt(10000)), r.Denom()).Int64()
	return fmt.Sprintf("%d.%04d", u/10000, u%10000)
}

func parseUint(n json.Number, field string) (uint64, error) {
	if n == "" {
		return 0, fmt.Errorf("%s is missing", field)
	}
	v, err := strconv.ParseUint(string(n), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%s %q must be a non-negative integer", field, string(n))
	}
	if v > maxSafeInt {
		return 0, fmt.Errorf("%s %d exceeds 2^53-1, which JSON clients cannot represent exactly", field, v)
	}
	return v, nil
}

// wholeTokens canonicalises a min_power given in whole SA tokens (an integer, possibly written 5.0).
func wholeTokens(n Num) (string, error) {
	c, err := Canonical(string(n), 30, 18)
	if err != nil {
		return "", err
	}
	if strings.Contains(c, ".") {
		return "", fmt.Errorf("%q must be a whole number of SA tokens", string(n))
	}
	return c, nil
}

// ParseRuleSet validates the content of a kind-37318 event: shape, the d tag, the event signer being
// the provider, and the EIP-191 signature by sa_address. It does not look at the clock: the
// delegation tag is checked by the caller (CheckRuleSetDelegation) at publish time only.
func ParseRuleSet(ev *nostr.Event, lim Limits) (*RuleSet, error) {
	if ev.Kind != KindRuleSet {
		return nil, fmt.Errorf("kind %d is not a rule set (%d)", ev.Kind, KindRuleSet)
	}
	var d []string
	for _, t := range ev.Tags {
		if len(t) >= 2 && t[0] == "d" {
			d = append(d, t[1])
		}
	}
	if len(d) != 1 || d[0] != ev.PubKey {
		return nil, fmt.Errorf("rule set needs exactly one [\"d\", <provider pubkey>] tag equal to the signer %s, got %v", ev.PubKey, d)
	}
	var c ruleSetJSON
	if err := decodeStrict(ev.Content, &c); err != nil {
		return nil, fmt.Errorf("rule set content is not the expected JSON: %v", err)
	}
	if c.V != 1 {
		return nil, fmt.Errorf("rule set v=%d unsupported, want 1", c.V)
	}
	if c.Provider != ev.PubKey {
		return nil, fmt.Errorf("rule set provider %q must equal the signing pubkey %s", c.Provider, ev.PubKey)
	}
	addr, err := evmsig.NormalizeAddress(c.SAAddress)
	if err != nil {
		return nil, err
	}
	tf, err := parseUint(c.Timeframe, "timeframe")
	if err != nil {
		return nil, fmt.Errorf("%w (seconds)", err)
	}
	if int64(tf) < lim.MinTimeframe || int64(tf) > lim.MaxTimeframe {
		return nil, fmt.Errorf("timeframe %ds is outside the allowed %ds to %ds", tf, lim.MinTimeframe, lim.MaxTimeframe)
	}
	rs := &RuleSet{Provider: ev.PubKey, SAAddress: addr, TimeframeSec: int64(tf), Sig: c.Sig, EventID: ev.ID, CreatedAt: int64(ev.CreatedAt)}
	switch {
	case c.Private != nil && len(c.Tiers) > 0:
		return nil, fmt.Errorf("rule set has both \"tiers\" and \"private\"; choose one")
	case c.Private != nil:
		m, err := wholeTokens(c.Private.MinPower)
		if err != nil {
			return nil, fmt.Errorf("private.min_power: %w", err)
		}
		rs.Private, rs.PrivateMin, rs.PrivateWei = true, m, TokensToWei(m)
	case len(c.Tiers) > 0:
		if len(c.Tiers) > lim.MaxTiers {
			return nil, fmt.Errorf("%d tiers exceeds the limit of %d", len(c.Tiers), lim.MaxTiers)
		}
		for i, t := range c.Tiers {
			m, err := wholeTokens(t.MinPower)
			if err != nil {
				return nil, fmt.Errorf("tier %d min_power: %w", i, err)
			}
			f, err := Canonical(string(t.DelayFraction), 4, 4)
			if err != nil {
				return nil, fmt.Errorf("tier %d delay_fraction: %w (at most 4 decimals)", i, err)
			}
			tier := Tier{MinPower: m, MinWei: TokensToWei(m), Fraction: f, FracRat: Rat(f)}
			if fl, _ := tier.FracRat.Float64(); fl > lim.MaxDelayFraction {
				return nil, fmt.Errorf("tier %d delay_fraction %s exceeds the limit of %v", i, f, lim.MaxDelayFraction)
			}
			rs.Tiers = append(rs.Tiers, tier)
		}
		sort.SliceStable(rs.Tiers, func(i, j int) bool { return rs.Tiers[i].MinWei.Cmp(rs.Tiers[j].MinWei) > 0 })
		for i := 1; i < len(rs.Tiers); i++ {
			hi, lo := rs.Tiers[i-1], rs.Tiers[i]
			if hi.MinWei.Cmp(lo.MinWei) == 0 {
				return nil, fmt.Errorf("duplicate min_power %s in tiers", hi.MinPower)
			}
			if lo.FracRat.Cmp(hi.FracRat) < 0 {
				return nil, fmt.Errorf("tier min_power=%s waits %sx but min_power=%s waits %sx: more power must never wait longer", hi.MinPower, hi.Fraction, lo.MinPower, lo.Fraction)
			}
		}
	default:
		return nil, fmt.Errorf("rule set needs a non-empty \"tiers\" list or a \"private\" object")
	}
	if err := evmsig.VerifyEIP191(rs.Message(), c.Sig, addr); err != nil {
		return nil, fmt.Errorf("rule set is not signed by its SA address: %w", err)
	}
	return rs, nil
}

// CheckRuleSetDelegation requires the event's sa-delegation tag to be valid at now and to come from
// the rule set's SA address, so the provider key is provably the SA identity's.
func CheckRuleSetDelegation(ev *nostr.Event, rs *RuleSet, now int64) error {
	addr, _, err := evmsig.Delegation(ev.Tags, ev.PubKey, now)
	if err != nil {
		return fmt.Errorf("rule set needs the provider's sa-delegation tag: %w", err)
	}
	if addr != rs.SAAddress {
		return fmt.Errorf("delegation is from %s but the rule set names SA identity %s", addr, rs.SAAddress)
	}
	return nil
}

// TierIndex returns the strongest tier whose threshold the power meets; if none does, the last
// (weakest) tier, as ARCHITECTURE.md specifies. Only valid for public rule sets. Index 0 is best.
func (r *RuleSet) TierIndex(power *big.Int) int {
	for i, t := range r.Tiers {
		if power.Cmp(t.MinWei) >= 0 {
			return i
		}
	}
	return len(r.Tiers) - 1
}

// Delay is the time after relay receipt that tier i may see a signal, scaled by scale (1.0 in production).
func (r *RuleSet) Delay(i int, scale float64) time.Duration {
	return DelayFor(r.Tiers[i].FracRat, r.TimeframeSec, scale)
}

// DelayFor computes fraction x timeframe x scale as sa_core.power.release_ms does: the fraction in
// units of 1/10000, the product in whole milliseconds (rounded down), then scaled.
func DelayFor(fraction *big.Rat, timeframeSec int64, scale float64) time.Duration {
	units := new(big.Int).Quo(new(big.Int).Mul(fraction.Num(), big.NewInt(10000)), fraction.Denom())
	ms := new(big.Int).Mul(units, big.NewInt(timeframeSec*1000))
	ms.Quo(ms, big.NewInt(10000))
	return time.Duration(float64(ms.Int64()) * float64(time.Millisecond) * scale)
}

// ---- allocations (kind 9332) ----

// Allocation is one signed split of an SA identity's token power, in basis points.
type Allocation struct {
	Address    string
	Nonce      uint64
	Governance uint32
	Providers  map[string]uint32 // provider pubkey -> bps
	Sig        string
	EventID    string
}

type allocationJSON struct {
	V          int                    `json:"v"`
	Address    string                 `json:"address"`
	Nonce      json.Number            `json:"nonce"`
	Governance json.Number            `json:"governance"`
	Providers  map[string]json.Number `json:"providers"`
	Sig        string                 `json:"sig"`
}

// Message is the exact text the SA address signs (EIP-191):
//
//	sa-allocation:{address}:{nonce}:{governance_bps}:{pubkey}={bps},...
//
// with providers sorted by pubkey (an empty list leaves the final field empty).
func (a *Allocation) Message() string {
	keys := make([]string, 0, len(a.Providers))
	for k := range a.Providers {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = fmt.Sprintf("%s=%d", k, a.Providers[k])
	}
	return fmt.Sprintf("sa-allocation:%s:%d:%d:%s", a.Address, a.Nonce, a.Governance, strings.Join(parts, ","))
}

// Total is the sum of all shares in basis points.
func (a *Allocation) Total() uint32 {
	t := a.Governance
	for _, b := range a.Providers {
		t += b
	}
	return t
}

// ParseAllocation validates a kind-9332 event: shape, share arithmetic (sum <= 10000, SEP-3.33)
// and the SA signature (SEP-3.32). Nonce ordering is the caller's job (it needs history).
func ParseAllocation(ev *nostr.Event, lim Limits) (*Allocation, error) {
	if ev.Kind != KindAllocation {
		return nil, fmt.Errorf("kind %d is not an allocation (%d)", ev.Kind, KindAllocation)
	}
	var c allocationJSON
	if err := decodeStrict(ev.Content, &c); err != nil {
		return nil, fmt.Errorf("allocation content is not the expected JSON: %v", err)
	}
	if c.V != 1 {
		return nil, fmt.Errorf("allocation v=%d unsupported, want 1", c.V)
	}
	addr, err := evmsig.NormalizeAddress(c.Address)
	if err != nil {
		return nil, err
	}
	nonce, err := parseUint(c.Nonce, "nonce")
	if err != nil {
		return nil, err
	}
	gov, err := parseUint(c.Governance, "governance")
	if err != nil {
		return nil, err
	}
	if len(c.Providers) > lim.MaxAllocations {
		return nil, fmt.Errorf("%d provider shares exceeds the limit of %d", len(c.Providers), lim.MaxAllocations)
	}
	a := &Allocation{Address: addr, Nonce: nonce, Governance: uint32(min(gov, 1<<31)), Providers: map[string]uint32{}, Sig: c.Sig, EventID: ev.ID}
	total := gov
	for pk, n := range c.Providers {
		if !ValidPubkey(pk) {
			return nil, fmt.Errorf("provider key %q must be 64 lowercase hex characters", pk)
		}
		b, err := parseUint(n, "providers."+pk[:8])
		if err != nil {
			return nil, err
		}
		if b < 1 {
			return nil, fmt.Errorf("provider %s share must be at least 1 bps, got %d; leave the provider out instead", pk[:8], b)
		}
		a.Providers[pk] = uint32(min(b, 1<<31))
		total += b
	}
	if total > MaxBPS {
		return nil, fmt.Errorf("shares sum to %d bps, more than the %d bps (100%%) of the identity's power", total, MaxBPS)
	}
	if err := evmsig.VerifyEIP191(a.Message(), c.Sig, addr); err != nil {
		return nil, fmt.Errorf("allocation is not signed by its SA address: %w", err)
	}
	return a, nil
}

// decodeStrict decodes one JSON value into v and refuses unknown object keys at any depth of a struct
// and anything after the value. Every event content the relay parses goes through it (SEP-1.13): the
// schemas are closed, so a secret field is an error naming the field, never silently stored.
func decodeStrict(content string, v any) error {
	dec := json.NewDecoder(strings.NewReader(content))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	if _, err := dec.Token(); err != io.EOF {
		return fmt.Errorf("unexpected data after the JSON value")
	}
	return nil
}

// ---- signals (kind 9317 / 9318) ----

// Signal holds the fields of a public signal the relay checks. The rest is the consumer's business.
//
// The content schema is closed (SEP-1.13): the fields below are all sa_core.models.Signal has, and any
// other key (say `api_secret` or `private_key`) is rejected, so a relay never stores or forwards a
// secret a client pasted into a signal by mistake. The unchecked fields are listed so that the closed
// decoder knows them; the consumer validates their values.
type Signal struct {
	V            int         `json:"v"`
	Timeframe    json.Number `json:"timeframe"`
	CreatedMS    json.Number `json:"created_ms"`
	TimeframeSec int64       `json:"-"`
	CreatedAt    time.Time   `json:"-"`

	StrategyID   json.RawMessage `json:"strategy_id"`
	Seq          json.RawMessage `json:"seq"`
	Prev         json.RawMessage `json:"prev"`
	Instrument   json.RawMessage `json:"instrument"`
	Side         json.RawMessage `json:"side"`
	Action       json.RawMessage `json:"action"`
	SizeFraction json.RawMessage `json:"size_fraction"`
	OrderType    json.RawMessage `json:"order_type"`
	LimitPrice   json.RawMessage `json:"limit_price"`
	StopPrice    json.RawMessage `json:"stop_price"`
}

// ParsePublicSignal reads the relay-checked fields of a plaintext signal.
func ParsePublicSignal(content string) (*Signal, error) {
	var s Signal
	if err := decodeStrict(content, &s); err != nil {
		return nil, fmt.Errorf("signal content is not a JSON object: %v", err)
	}
	tf, err := parseUint(s.Timeframe, "timeframe")
	if err != nil {
		return nil, fmt.Errorf("%w (integer seconds)", err)
	}
	ms, err := parseUint(s.CreatedMS, "created_ms")
	if err != nil {
		return nil, err
	}
	s.TimeframeSec = int64(tf)
	s.CreatedAt = time.UnixMilli(int64(ms))
	return &s, nil
}

// NIP-44 v2 payload size bounds (decoded): version(1) + nonce(32) + ciphertext(>=34) + mac(32) .. 65603.
const (
	nip44Min = 99
	nip44Max = 65603
)

// LooksLikeNIP44 reports whether content is a base64 NIP-44 v2 payload. It cannot prove the
// payload decrypts, only that it is not plaintext JSON a provider forgot to encrypt.
func LooksLikeNIP44(content string) bool {
	b, err := base64.StdEncoding.DecodeString(content)
	if err != nil || len(b) < nip44Min || len(b) > nip44Max {
		return false
	}
	return b[0] == 2
}

// Recipient returns the single p-tag pubkey of a private signal.
func Recipient(tags nostr.Tags) (string, error) {
	var ps []string
	for _, t := range tags {
		if len(t) >= 2 && t[0] == "p" {
			ps = append(ps, t[1])
		}
	}
	if len(ps) != 1 || !ValidPubkey(ps[0]) {
		return "", fmt.Errorf("a private signal needs exactly one [\"p\", <recipient pubkey hex>] tag, found %v", ps)
	}
	return ps[0], nil
}

// ---- commitments (kind 9318) ----

// Commitment is the public content of a commitment: the digest of a private signal plus its chain slot.
type Commitment struct {
	V          int     `json:"v"`
	StrategyID string  `json:"strategy_id"`
	Seq        uint64  `json:"seq"`
	Prev       *string `json:"prev"`
	Digest     string  `json:"digest"`
}

// ParseCommitment validates commitment content as sa_core.models.Commitment does.
func ParseCommitment(content string) (*Commitment, error) {
	var c Commitment
	if err := decodeStrict(content, &c); err != nil {
		return nil, fmt.Errorf("commitment content is not the expected JSON {v,strategy_id,seq,prev,digest}: %v", err)
	}
	switch {
	case c.V != 1:
		return nil, fmt.Errorf("commitment v=%d unsupported, want 1", c.V)
	case c.StrategyID == "" || len(c.StrategyID) > 128:
		return nil, fmt.Errorf("commitment strategy_id must be 1 to 128 characters, got %d", len(c.StrategyID))
	case c.Seq < 1 || c.Seq > maxSafeInt:
		return nil, fmt.Errorf("commitment seq %d must be at least 1", c.Seq)
	case !ValidPubkey(c.Digest):
		return nil, fmt.Errorf("commitment digest %q must be 64 lowercase hex characters", c.Digest)
	case (c.Seq == 1) != (c.Prev == nil):
		return nil, fmt.Errorf("commitment seq %d and prev disagree: seq 1 has prev null, later seqs name the previous id", c.Seq)
	case c.Prev != nil && !ValidPubkey(*c.Prev):
		return nil, fmt.Errorf("commitment prev %q must be 64 lowercase hex characters", *c.Prev)
	}
	return &c, nil
}

// ---- governance delegations (kind 9333) ----

type govDelegationJSON struct {
	V       int                    `json:"v"`
	Address string                 `json:"address"`
	Nonce   json.Number            `json:"nonce"`
	To      map[string]json.Number `json:"to"`
	Sig     string                 `json:"sig"`
}

// ValidateGovDelegation checks a kind-9333 content: shares sum to at most 10000 bps, no self-delegation,
// and the EIP-191 signature by the delegating address over
// sa-govdelegation:{address}:{nonce}:{addr=bps,...} (addresses sorted).
func ValidateGovDelegation(content string) error {
	var c govDelegationJSON
	if err := decodeStrict(content, &c); err != nil {
		return fmt.Errorf("governance delegation content is not the expected JSON: %v", err)
	}
	if c.V != 1 {
		return fmt.Errorf("governance delegation v=%d unsupported, want 1", c.V)
	}
	addr, err := evmsig.NormalizeAddress(c.Address)
	if err != nil {
		return err
	}
	nonce, err := parseUint(c.Nonce, "nonce")
	if err != nil {
		return err
	}
	to := map[string]uint64{}
	keys := make([]string, 0, len(c.To))
	var total uint64
	for a, n := range c.To {
		na, err := evmsig.NormalizeAddress(a)
		if err != nil {
			return err
		}
		if na == addr {
			return fmt.Errorf("%s cannot delegate to itself", addr)
		}
		b, err := parseUint(n, "to."+a)
		if err != nil {
			return err
		}
		if b < 1 || b > MaxBPS {
			return fmt.Errorf("delegation share to %s must be within 1..%d bps, got %d", na, MaxBPS, b)
		}
		to[na] = b
		keys = append(keys, na)
		total += b
	}
	if total > MaxBPS {
		return fmt.Errorf("delegation shares sum to %d bps, more than %d", total, MaxBPS)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = fmt.Sprintf("%s=%d", k, to[k])
	}
	msg := fmt.Sprintf("sa-govdelegation:%s:%d:%s", addr, nonce, strings.Join(parts, ","))
	if err := evmsig.VerifyEIP191(msg, c.Sig, addr); err != nil {
		return fmt.Errorf("governance delegation is not signed by %s: %w", addr, err)
	}
	return nil
}

// ---- delegation revocations (kind 9334) ----

// Revocation withdraws one device key's delegation from an SA address from EffectiveAt on.
type Revocation struct {
	Address     string
	DevicePub   string
	EffectiveAt int64 // unix seconds
	Sig         string
	EventID     string
}

type revocationJSON struct {
	V           int         `json:"v"`
	DevicePub   string      `json:"device_pubkey"`
	EffectiveAt json.Number `json:"effective_at"`
	Address     string      `json:"address"`
	Sig         string      `json:"sig"`
}

// RevocationMessage is the text the SA address signs (EIP-191), as sa-core uses: sa-revocation:{device_pubkey}:{effective_at}.
func RevocationMessage(devicePub string, effectiveAt int64) string {
	return fmt.Sprintf("sa-revocation:%s:%d", devicePub, effectiveAt)
}

// ParseRevocation validates a kind-9334 event: shape and the EIP-191 signature by `address`. The event
// signer is irrelevant (anyone may relay a revocation); authority comes from the signature in the content.
func ParseRevocation(ev *nostr.Event) (*Revocation, error) {
	if ev.Kind != KindRevocation {
		return nil, fmt.Errorf("kind %d is not a revocation (%d)", ev.Kind, KindRevocation)
	}
	var c revocationJSON
	if err := decodeStrict(ev.Content, &c); err != nil {
		return nil, fmt.Errorf("revocation content is not the expected JSON {v,device_pubkey,effective_at,address,sig}: %v", err)
	}
	if c.V != 1 {
		return nil, fmt.Errorf("revocation v=%d unsupported, want 1", c.V)
	}
	if !ValidPubkey(c.DevicePub) {
		return nil, fmt.Errorf("revocation device_pubkey %q must be 64 lowercase hex characters", c.DevicePub)
	}
	addr, err := evmsig.NormalizeAddress(c.Address)
	if err != nil {
		return nil, err
	}
	at, err := parseUint(c.EffectiveAt, "effective_at")
	if err != nil {
		return nil, err
	}
	if err := evmsig.VerifyEIP191(RevocationMessage(c.DevicePub, int64(at)), c.Sig, addr); err != nil {
		return nil, fmt.Errorf("revocation is not signed by %s: %w", addr, err)
	}
	return &Revocation{Address: addr, DevicePub: c.DevicePub, EffectiveAt: int64(at), Sig: c.Sig, EventID: ev.ID}, nil
}
