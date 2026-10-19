package protocol

import (
	"encoding/json"
	"fmt"
	"math/big"
	"math/rand"
	"strings"
	"testing"
	"time"

	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/nbd-wtf/go-nostr"
	"github.com/nbd-wtf/go-nostr/nip44"

	"github.com/superalgos/sarelay/internal/evmsig/evmsigtest"
)

// Golden vectors from Python eth_account (key 0x11*32); see the evmsig tests.
const (
	gAddr  = "0x19e7e376e7c213b7e7e7e46cc70a5dd086daff2a"
	gProv  = "abababababababababababababababababababababababababababababababab"
	gAlloc = "0x09a0f30b7aa8921fee536a8f724df7a0cf055cb27e3976407653d48addb5c6a0352b582a818888c9100ce7114ec8f5092166a967198be45765fd8963993b2dd11b"
	gEmpty = "0xcce5a809b2092b58b2139a729f7c9f771387d4f0ef8454a790b4477313c6303f60eb6deb888bba4e377bb279eaef9839895f3c26b9178da32765b43d38c5163c1c"
)

func ruleEvent(pub, content string, extra ...nostr.Tag) *nostr.Event {
	return &nostr.Event{PubKey: pub, Kind: KindRuleSet, CreatedAt: 1000, Tags: append(nostr.Tags{{"d", pub}}, extra...), Content: content}
}

func TestAllocationGoldenFromPython(t *testing.T) {
	a, b := strings.Repeat("a", 64), strings.Repeat("b", 64)
	ev := &nostr.Event{Kind: KindAllocation, Content: fmt.Sprintf(`{"v":1,"address":"%s","nonce":5,"governance":3000,"providers":{"%s":4000,"%s":3000},"sig":"%s"}`, gAddr, a, b, gAlloc)}
	al, err := ParseAllocation(ev, DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	if al.Total() != 10000 || al.Providers[a] != 4000 {
		t.Fatalf("%+v", al)
	}
	empty := &nostr.Event{Kind: KindAllocation, Content: fmt.Sprintf(`{"v":1,"address":"%s","nonce":6,"governance":10000,"providers":{},"sig":"%s"}`, gAddr, gEmpty)}
	if _, err := ParseAllocation(empty, DefaultLimits()); err != nil {
		t.Fatalf("empty provider list: %v", err)
	}
}

func TestCanonicalDecimal(t *testing.T) {
	for in, want := range map[string]string{"0": "0", "00": "0", "0.0": "0", "0.10": "0.1", "1000000": "1000000", "007.500": "7.5", "2.": ""} {
		got, err := Canonical(in, 30, 18)
		if want == "" {
			if err == nil {
				t.Errorf("%q accepted as %q", in, got)
			}
			continue
		}
		if err != nil || got != want {
			t.Errorf("Canonical(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, bad := range []string{"", "-1", "+1", "1e3", "0x10", "1,5", " 1", "1 ", "NaN", "1.2.3", strings.Repeat("9", 31), "0." + strings.Repeat("1", 19)} {
		if _, err := Canonical(bad, 30, 18); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

// Property: for random signers and random shares, an allocation is accepted iff the shares sum to
// at most 10000 bps (SEP-3.33) and the signature is by the claimed address (SEP-3.32).
func TestAllocationProperty(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	lim := DefaultLimits()
	mk := func(k *secp256k1.PrivateKey, claim string, nonce uint64, gov uint32, ps map[string]uint32) *nostr.Event {
		al := &Allocation{Address: claim, Nonce: nonce, Governance: gov, Providers: ps}
		raw, _ := json.Marshal(map[string]any{"v": 1, "address": claim, "nonce": nonce, "governance": gov, "providers": ps, "sig": evmsigtest.Sign(k, al.Message())})
		return &nostr.Event{Kind: KindAllocation, Content: string(raw)}
	}
	for i := 0; i < 400; i++ {
		k, _ := secp256k1.GeneratePrivateKey()
		other, _ := secp256k1.GeneratePrivateKey()
		addr := evmsigtest.AddressOf(k)
		ps := map[string]uint32{}
		sum := uint32(0)
		gov := uint32(rng.Intn(6000))
		sum += gov
		for j, n := 0, rng.Intn(5); j < n; j++ {
			b := uint32(rng.Intn(4000))
			ps[fmt.Sprintf("%064x", rng.Uint64()+uint64(j))] += b
			sum += b
		}
		wantOK := sum <= MaxBPS
		_, err := ParseAllocation(mk(k, addr, uint64(rng.Intn(1000)), gov, ps), lim)
		if (err == nil) != wantOK {
			t.Fatalf("iter %d: shares sum %d, err=%v", i, sum, err)
		}
		if wantOK { // same shares signed by someone else are rejected
			if _, err := ParseAllocation(mk(other, addr, 1, gov, ps), lim); err == nil {
				t.Fatalf("iter %d: allocation signed by another key accepted", i)
			}
		}
	}
}

func signedRuleSet(k *secp256k1.PrivateKey, tf int64, text string, mut func(map[string]any)) *nostr.Event {
	addr := evmsigtest.AddressOf(k)
	m := map[string]any{"v": 1, "provider": gProv, "sa_address": addr, "timeframe": tf,
		"tiers": []map[string]any{{"min_power": 100, "delay_fraction": 0.0}, {"min_power": 0, "delay_fraction": 1.5}},
		"sig":   evmsigtest.Sign(k, fmt.Sprintf("sa-ruleset:%s:%d:%s", gProv, tf, text))}
	if mut != nil {
		mut(m)
	}
	raw, _ := json.Marshal(m)
	return ruleEvent(gProv, string(raw))
}

// Property: flipping any one field of a signed rule set invalidates it.
func TestSignedContentIsTamperEvident(t *testing.T) {
	k, _ := secp256k1.GeneratePrivateKey()
	text := "0@1.5000,100@0.0000" // ascending min_power, %.4f
	if _, err := ParseRuleSet(signedRuleSet(k, 600, text, nil), DefaultLimits()); err != nil {
		t.Fatalf("untouched rule set rejected: %v", err)
	}
	for name, mut := range map[string]func(map[string]any){
		"timeframe": func(m map[string]any) { m["timeframe"] = 60 },
		"fraction": func(m map[string]any) {
			m["tiers"] = []map[string]any{{"min_power": 100, "delay_fraction": 0.0}, {"min_power": 0, "delay_fraction": 1.4}}
		},
		"threshold": func(m map[string]any) {
			m["tiers"] = []map[string]any{{"min_power": 99, "delay_fraction": 0.0}, {"min_power": 0, "delay_fraction": 1.5}}
		},
		"to private": func(m map[string]any) { delete(m, "tiers"); m["private"] = map[string]any{"min_power": 100} },
		"sa address": func(m map[string]any) { m["sa_address"] = strings.Repeat("1", 1) + "x" },
	} {
		if _, err := ParseRuleSet(signedRuleSet(k, 600, text, mut), DefaultLimits()); err == nil {
			t.Errorf("changed %s but the rule set still verifies", name)
		}
	}
	// the text must be ascending with %.4f: any other rendering of the same rules does not verify
	for _, bad := range []string{"100@0.0000,0@1.5000", "0@1.5,100@0", "0@1.5000;100@0.0000"} {
		if _, err := ParseRuleSet(signedRuleSet(k, 600, bad, nil), DefaultLimits()); err == nil {
			t.Errorf("signing text %q accepted", bad)
		}
	}
}

func TestRuleSetValidation(t *testing.T) {
	k, _ := secp256k1.GeneratePrivateKey()
	mk := func(tf int64, tiers, text string) *nostr.Event {
		addr := evmsigtest.AddressOf(k)
		sig := evmsigtest.Sign(k, fmt.Sprintf("sa-ruleset:%s:%d:%s", gProv, tf, text))
		return ruleEvent(gProv, fmt.Sprintf(`{"v":1,"provider":"%s","sa_address":"%s","timeframe":%d,%s"sig":"%s"}`, gProv, addr, tf, tiers, sig))
	}
	t1 := `"tiers":[{"min_power":100,"delay_fraction":0},{"min_power":0,"delay_fraction":1}],`
	for _, c := range []struct {
		name string
		ev   *nostr.Event
		sub  string
	}{
		{"ok", mk(600, t1, "0@1.0000,100@0.0000"), ""},
		{"any tier order", mk(600, `"tiers":[{"min_power":0,"delay_fraction":1.0},{"min_power":100,"delay_fraction":0.0}],`, "0@1.0000,100@0.0000"), ""},
		{"string numbers", mk(600, `"tiers":[{"min_power":"100","delay_fraction":"0.25"},{"min_power":"0","delay_fraction":"1"}],`, "0@1.0000,100@0.2500"), ""},
		{"timeframe too small", mk(30, t1, "0@1.0000,100@0.0000"), "outside the allowed"},
		{"timeframe too large", mk(999999999, t1, "0@1.0000,100@0.0000"), "outside the allowed"},
		{"weaker tier earlier", mk(600, `"tiers":[{"min_power":100,"delay_fraction":2},{"min_power":0,"delay_fraction":1}],`, "0@1.0000,100@2.0000"), "never wait longer"},
		{"duplicate threshold", mk(600, `"tiers":[{"min_power":5,"delay_fraction":0},{"min_power":5,"delay_fraction":1}],`, "5@0.0000,5@1.0000"), "duplicate"},
		{"fractional min_power", mk(600, `"tiers":[{"min_power":1.5,"delay_fraction":0}],`, "1.5@0.0000"), "whole number"},
		{"exponent", mk(600, `"tiers":[{"min_power":1e3,"delay_fraction":0}],`, "1000@0.0000"), "plain decimal"},
		{"negative", mk(600, `"tiers":[{"min_power":-1,"delay_fraction":0}],`, "-1@0.0000"), "plain decimal"},
		{"5 decimals", mk(600, `"tiers":[{"min_power":0,"delay_fraction":0.12345}],`, "0@0.1235"), "4 decimals"},
		{"huge fraction", mk(600, `"tiers":[{"min_power":0,"delay_fraction":101}],`, "0@101.0000"), "limit"},
		{"both", mk(600, t1+`"private":{"min_power":1},`, "0@1.0000,100@0.0000"), "both"},
		{"neither", mk(600, "", ""), "non-empty"},
	} {
		_, err := ParseRuleSet(c.ev, DefaultLimits())
		switch {
		case c.sub == "" && err != nil:
			t.Errorf("%s: %v", c.name, err)
		case c.sub != "" && (err == nil || !strings.Contains(err.Error(), c.sub)):
			t.Errorf("%s: err=%v, want it to mention %q", c.name, err, c.sub)
		}
	}
	// the d tag must be the signer, and the signer must be the provider
	ev := mk(600, t1, "0@1.0000,100@0.0000")
	ev.Tags = nostr.Tags{{"d", "other"}}
	if _, err := ParseRuleSet(ev, DefaultLimits()); err == nil || !strings.Contains(err.Error(), "d\"") {
		t.Errorf("d tag mismatch: %v", err)
	}
	ev = mk(600, t1, "0@1.0000,100@0.0000")
	ev.PubKey, ev.Tags = strings.Repeat("cd", 32), nostr.Tags{{"d", strings.Repeat("cd", 32)}}
	if _, err := ParseRuleSet(ev, DefaultLimits()); err == nil || !strings.Contains(err.Error(), "signing pubkey") {
		t.Errorf("provider/signer mismatch: %v", err)
	}
}

// Property (SEP-3.21): more allocated power never means a later release, for any valid rule set.
func TestMoreAllocatedPowerNeverReleasesLater(t *testing.T) {
	rng := rand.New(rand.NewSource(11))
	for i := 0; i < 300; i++ {
		n := 1 + rng.Intn(6)
		thr := rng.Perm(1000)[:n]
		sortDesc(thr)
		frac := 0.0
		rs := &RuleSet{TimeframeSec: int64(60 + rng.Intn(100000))}
		for _, th := range thr {
			frac += float64(rng.Intn(300)) / 100
			f, _ := Canonical(fmt.Sprintf("%.2f", frac), 4, 9)
			rs.Tiers = append(rs.Tiers, Tier{MinPower: fmt.Sprint(th), MinWei: TokensToWei(fmt.Sprint(th)), Fraction: f, FracRat: Rat(f)})
		}
		prev := time.Duration(-1)
		for p := 1100; p >= 0; p -= 7 { // descending power: delay must not decrease
			d := rs.Delay(rs.TierIndex(TokensToWei(fmt.Sprint(p))), 1)
			if prev >= 0 && d < prev {
				t.Fatalf("iter %d: power %d released at %s, earlier than a stronger holder's %s", i, p, d, prev)
			}
			prev = d
		}
	}
}

func sortDesc(a []int) {
	for i := range a {
		for j := i + 1; j < len(a); j++ {
			if a[j] > a[i] {
				a[i], a[j] = a[j], a[i]
			}
		}
	}
}

func TestDelayMath(t *testing.T) {
	for _, c := range []struct {
		frac  string
		tf    int64
		scale float64
		want  time.Duration
	}{
		{"0", 3600, 1, 0}, {"0.1", 3600, 1, 6 * time.Minute}, {"0.5", 14400, 1, 2 * time.Hour}, {"2", 60, 1, 2 * time.Minute},
		{"2", 60, 1.0 / 60, 2 * time.Second}, {"0.333", 3, 1, 999 * time.Millisecond},
	} {
		if got := DelayFor(Rat(c.frac), c.tf, c.scale); got < c.want-time.Microsecond || got > c.want+time.Microsecond {
			t.Errorf("%s x %ds x %v = %s, want %s", c.frac, c.tf, c.scale, got, c.want)
		}
	}
	if w := TokensToWei("1.5"); w.Cmp(new(big.Int).Mul(big.NewInt(15), new(big.Int).Exp(big.NewInt(10), big.NewInt(17), nil))) != 0 {
		t.Errorf("1.5 tokens = %s wei", w)
	}
}

func TestPublicSignalParsing(t *testing.T) {
	ok := `{"v":2,"strategy_id":"s","seq":1,"prev":null,"instrument":"BINANCE:BTCUSDT","side":"buy","action":"open","size_fraction":0.5,"order_type":"market","timeframe":3600,"created_ms":1700000000123,"bar_open_ms":1699996400123}`
	s, err := ParsePublicSignal(ok)
	if err != nil || s.TimeframeSec != 3600 || s.CreatedAt.UnixMilli() != 1700000000123 {
		t.Fatalf("%+v %v", s, err)
	}
	for _, bad := range []string{`not json`, `{"timeframe":"1h","created_ms":1}`, `{"timeframe":60}`, `{"timeframe":-5,"created_ms":1}`, `{"timeframe":60.5,"created_ms":1}`, `[]`,
		`{"v":2,"timeframe":3600,"created_ms":1700000000123,"bar_open_ms":1699996400123,"api_secret":"s3cr3t"}`, ok + ` {"trailing":1}`} {
		if _, err := ParsePublicSignal(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

// SEP-3.18: a signal must be v2 and name its bar; v1 content and a missing or malformed bar_open_ms are refused.
func TestPublicSignalNamesItsBar(t *testing.T) {
	const body = `"strategy_id":"s","seq":1,"prev":null,"instrument":"BINANCE:BTCUSDT","side":"buy","action":"open","size_fraction":0.5,"order_type":"market","timeframe":3600,"created_ms":1700000000123`
	s, err := ParsePublicSignal(`{"v":2,` + body + `,"bar_open_ms":1699996400000}`)
	if err != nil || s.BarOpenAt.UnixMilli() != 1699996400000 {
		t.Fatalf("v2 signal: %+v %v", s, err)
	}
	cases := map[string]struct{ content, want string }{
		"v1 with the field": {`{"v":1,` + body + `,"bar_open_ms":1699996400000}`, "v is 1"},
		"v1 without":        {`{"v":1,` + body + `}`, "v is 1"},
		"no v":              {`{` + body + `,"bar_open_ms":1699996400000}`, "v is 0"},
		"v3":                {`{"v":3,` + body + `,"bar_open_ms":1699996400000}`, "v is 3"},
		"missing bar":       {`{"v":2,` + body + `}`, "bar_open_ms is missing"},
		"negative bar":      {`{"v":2,` + body + `,"bar_open_ms":-1}`, "bar_open_ms"},
		"fractional bar":    {`{"v":2,` + body + `,"bar_open_ms":1.5}`, "bar_open_ms"},
	}
	for name, c := range cases {
		if _, err := ParsePublicSignal(c.content); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err=%v, want a refusal containing %q", name, err, c.want)
		}
	}
}

func TestLooksLikeNIP44(t *testing.T) {
	a, b := nostr.GeneratePrivateKey(), nostr.GeneratePrivateKey()
	bp, _ := nostr.GetPublicKey(b)
	ck, _ := nip44.GenerateConversationKey(bp, a)
	ct, err := nip44.Encrypt(`{"timeframe":60}`, ck)
	if err != nil {
		t.Fatal(err)
	}
	if !LooksLikeNIP44(ct) {
		t.Error("a real NIP-44 payload is not recognised")
	}
	for _, bad := range []string{``, `{"timeframe":60,"created_ms":1}`, `hello world`, `AAAA`, strings.Repeat("A", 200)} {
		if LooksLikeNIP44(bad) {
			t.Errorf("%q recognised as NIP-44", bad)
		}
	}
}

func TestRecipient(t *testing.T) {
	pk := strings.Repeat("c", 64)
	if got, err := Recipient(nostr.Tags{{"p", pk}, {"e", "x"}}); err != nil || got != pk {
		t.Fatalf("%q %v", got, err)
	}
	for _, tags := range []nostr.Tags{nil, {{"p", pk}, {"p", pk}}, {{"p", "short"}}, {{"p", strings.ToUpper(pk)}}} {
		if _, err := Recipient(tags); err == nil {
			t.Errorf("%v accepted", tags)
		}
	}
}

// SEP-1.13: every content schema is closed. A secret pasted into a signal, commitment, allocation or
// rule set is an error that names the field; nothing is stored or forwarded.
func TestContentSchemasRejectSecretFields(t *testing.T) {
	sig := `{"v":2,"strategy_id":"s","seq":1,"prev":null,"instrument":"BINANCE:BTCUSDT","side":"buy","action":"open","size_fraction":0.5,"order_type":"market","timeframe":3600,"created_ms":1700000000123,"bar_open_ms":1699996400123`
	com := `{"v":1,"strategy_id":"s","seq":1,"prev":null,"digest":"` + strings.Repeat("ab", 32) + `"`
	if _, err := ParsePublicSignal(sig + `}`); err != nil {
		t.Fatalf("baseline signal refused: %v", err)
	}
	if _, err := ParseCommitment(com + `}`); err != nil {
		t.Fatalf("baseline commitment refused: %v", err)
	}
	for _, field := range []string{"api_secret", "private_key", "mnemonic", "apiKey", "secret_key"} {
		extra := `,"` + field + `":"hunter2"}`
		if _, err := ParsePublicSignal(sig + extra); err == nil || !strings.Contains(err.Error(), field) {
			t.Errorf("signal with %s: err=%v, want a refusal naming the field", field, err)
		}
		if _, err := ParseCommitment(com + extra); err == nil || !strings.Contains(err.Error(), field) {
			t.Errorf("commitment with %s: err=%v, want a refusal naming the field", field, err)
		}
		gd := `{"v":1,"address":"0x` + strings.Repeat("11", 20) + `","nonce":1,"to":{},"sig":"0x00"` + extra
		if err := ValidateGovDelegation(gd); err == nil || !strings.Contains(err.Error(), field) {
			t.Errorf("governance delegation with %s: err=%v", field, err)
		}
	}
}

// Kind 9334: the signature is checked against a vector produced by sa_core (key 0x11*32,
// sa_core.delegation.revocation_message); the signer of the event itself does not matter.
func TestRevocationGoldenFromPython(t *testing.T) {
	const sig = "0x12bd019d8e66b2fac58db21ed911d1c747896549c19ce839ddbc9658197e93ef63db3d77c9c8475ce2221e96752b113478faabb999122eb1d9ec81d63697d1c61b"
	mk := func(device string, at int64, address, sig string) *nostr.Event {
		return &nostr.Event{Kind: KindRevocation, PubKey: strings.Repeat("c", 64),
			Content: fmt.Sprintf(`{"v":1,"device_pubkey":"%s","effective_at":%d,"address":"%s","sig":"%s"}`, device, at, address, sig)}
	}
	r, err := ParseRevocation(mk(gProv, 1700000000, gAddr, sig))
	if err != nil {
		t.Fatal(err)
	}
	if r.Address != gAddr || r.DevicePub != gProv || r.EffectiveAt != 1700000000 {
		t.Errorf("%+v", r)
	}
	for name, ev := range map[string]*nostr.Event{
		"other effective_at": mk(gProv, 1700000001, gAddr, sig),
		"other device":       mk(strings.Repeat("d", 64), 1700000000, gAddr, sig),
		"other address":      mk(gProv, 1700000000, "0x"+strings.Repeat("1", 40), sig),
		"short device":       mk("abcd", 1700000000, gAddr, sig),
		"unknown field":      {Kind: KindRevocation, Content: `{"v":1,"device_pubkey":"` + gProv + `","effective_at":1,"address":"` + gAddr + `","sig":"` + sig + `","api_secret":"x"}`},
		"wrong kind":         {Kind: KindVote, Content: "{}"},
	} {
		if _, err := ParseRevocation(ev); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if RevocationMessage("ab", 7) != "sa-revocation:ab:7" {
		t.Error("message text drifted from sa_core")
	}
}
