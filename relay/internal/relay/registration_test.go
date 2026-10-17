package relay_test

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/protocol"
)

// SEP-3.24: a rule set is accepted only if signed via the provider's SA identity.
func TestRuleSetMustBeSignedBySAIdentity(t *testing.T) {
	r := startRelay(t, "")
	pub := dial(t, r.URL)
	p := newActor()
	thief := newActor()
	day := time.Now().Add(time.Hour).Unix()

	good := p.ruleSet(0, 60, defaultRules)
	msg := fmt.Sprintf("sa-ruleset:%s:60:%s", p.pub, rulesText(defaultRules))
	tiers := []map[string]any{{"min_power": 0, "delay_fraction": 0.0}}
	// the same text, but the SA signature comes from another EVM key while the content names p's address
	badSig := p.signRuleSet(1, map[string]any{"v": 1, "provider": p.pub, "sa_address": p.addr, "timeframe": 60, "tiers": tiers, "sig": signWith(thief, msg)}, true)
	// another key claiming this provider pubkey
	forgedProvider := thief.signRuleSet(1, map[string]any{"v": 1, "provider": p.pub, "sa_address": thief.addr, "timeframe": 60, "tiers": tiers,
		"sig": signWith(thief, fmt.Sprintf("sa-ruleset:%s:60:0@0.0000", p.pub))}, true)
	// rules altered after the SA signed them (still a well-formed ladder)
	tampered := p.ruleSet(2, 60, defaultRules)
	tampered.Content = strings.Replace(tampered.Content, `"delay_fraction":2`, `"delay_fraction":3`, 1)
	_ = tampered.Sign(p.sk)
	// the rule set itself is fine but the provider key carries no delegation, or one from another SA address
	noDeleg := p.signRuleSet(3, map[string]any{"v": 1, "provider": p.pub, "sa_address": p.addr, "timeframe": 60, "tiers": tiers,
		"sig": signWith(p, fmt.Sprintf("sa-ruleset:%s:60:0@0.0000", p.pub))}, false)
	foreignDeleg := p.ruleSet(4, 60, defaultRules)
	foreignDeleg.Tags = nostr.Tags{{"d", p.pub}, evmsigDelegation(thief, p.pub, day)}
	_ = foreignDeleg.Sign(p.sk)
	expiredDeleg := p.ruleSet(5, 60, defaultRules)
	expiredDeleg.Tags = nostr.Tags{{"d", p.pub}, evmsigDelegation(p, p.pub, time.Now().Add(-time.Hour).Unix())}
	_ = expiredDeleg.Sign(p.sk)

	for _, c := range []struct {
		name string
		ev   *nostr.Event
		ok   bool
	}{
		{"rule set signed by its SA address", good, true},
		{"SA signature from a different EVM key", badSig, false},
		{"another key claiming this provider pubkey", forgedProvider, false},
		{"rules altered after the SA signed", tampered, false},
		{"no sa-delegation tag on the provider key", noDeleg, false},
		{"delegation from another SA address", foreignDeleg, false},
		{"expired delegation", expiredDeleg, false},
	} {
		ok, why := pub.publish(c.ev)
		t.Logf("%-45s ok=%v %s", c.name, ok, why)
		if ok != c.ok {
			t.Errorf("%s: ok=%v want %v (%s)", c.name, ok, c.ok, why)
		}
		if !ok && !strings.HasPrefix(why, "invalid:") {
			t.Errorf("%s: reason %q should start with invalid:", c.name, why)
		}
	}
	// an older or equal created_at cannot replace the registered rules; a newer one can
	if ok, why := pub.publish(p.ruleSet(-30, 60, []tierSpec{{"0", "0"}})); ok {
		t.Errorf("older rule set accepted; reason=%s", why)
	}
	if ok, why := pub.publish(p.ruleSet(10, 60, []tierSpec{{"0", "0"}})); !ok {
		t.Errorf("newer rule set refused: %s", why)
	}
}

func signWith(a actor, msg string) string { return evmsigSign(a, msg) }

// SEP-3.39 and SEP-3.40: only registered providers publish, and only with the bound timeframe.
func TestOnlyRegisteredProvidersPublishWithBoundTimeframe(t *testing.T) {
	r := startRelay(t, "")
	prov := registerProvider(t, r, 120, defaultRules)
	stranger := newActor()
	pub := dial(t, r.URL)
	reader := dial(t, r.URL)
	rid := newActor()
	bal := map[string]string{}
	fundFollower(t, r, bal, rid, 5_000_000, 1, map[string]uint32{prov.pub: 10000})
	reader.login(r.URL, rid)

	// a delegated device key of the provider's SA identity
	devSK, devPub := prov.device()
	delegated := func(tf int64, seq int) *nostr.Event {
		return signalBy(devSK, nostr.Tags{evmsigDelegation(prov, devPub, time.Now().Add(time.Hour).Unix())}, tf, seq, time.Now().UnixMilli())
	}
	k1 := prov.signal(120, 9)
	k1.Kind = 1
	_ = k1.Sign(prov.sk)
	expired := signalBy(devSK, nostr.Tags{evmsigDelegation(prov, devPub, time.Now().Add(-time.Hour).Unix())}, 120, 11, time.Now().UnixMilli())
	strangerDelegated := signalBy(devSK, nostr.Tags{evmsigDelegation(stranger, devPub, time.Now().Add(time.Hour).Unix())}, 120, 12, time.Now().UnixMilli())
	now := time.Now().UnixMilli()

	type attempt struct {
		name string
		ev   *nostr.Event
		ok   bool
	}
	for _, a := range []attempt{
		{"provider key, bound timeframe", prov.signal(120, 1), true},
		{"provider key without a delegation tag", signalBy(prov.sk, nil, 120, 13, now), false},
		{"provider's delegated device key", delegated(120, 2), true},
		{"unregistered pubkey, no delegation", signalBy(stranger.sk, nil, 120, 3, now), false},
		{"unregistered SA address delegating a key", strangerDelegated, false},
		{"expired delegation from the provider", expired, false},
		{"provider, shorter timeframe (SEP-3.40)", prov.signal(60, 4), false},
		{"provider, longer timeframe", prov.signal(3600, 5), false},
		{"provider, timeframe zero", signalBy(prov.sk, nostr.Tags{evmsigDelegation(prov, prov.pub, time.Now().Add(time.Hour).Unix())}, 0, 6, now), false},
		{"provider, backdated 1 h", signalBy(prov.sk, nostr.Tags{evmsigDelegation(prov, prov.pub, time.Now().Add(time.Hour).Unix())}, 120, 7, now-3_600_000), false},
		{"provider, future 1 h", signalBy(prov.sk, nostr.Tags{evmsigDelegation(prov, prov.pub, time.Now().Add(time.Hour).Unix())}, 120, 8, now+3_600_000), false},
		{"provider, kind 1", k1, false},
	} {
		ok, why := pub.publish(a.ev)
		t.Logf("%-44s ok=%-5v %s", a.name, ok, why)
		if ok != a.ok {
			t.Errorf("%s: ok=%v want %v (%s)", a.name, ok, a.ok, why)
		}
		if !ok && !strings.HasPrefix(why, "blocked:") && !strings.HasPrefix(why, "invalid:") {
			t.Errorf("%s: reason %q lacks a blocked:/invalid: prefix", a.name, why)
		}
	}
	for i := 0; i < 100; i++ { // a burst from the stranger
		ok, why := pub.publish(signalBy(stranger.sk, nil, 120, 100+i, time.Now().UnixMilli()))
		if ok {
			t.Fatal("stranger burst accepted")
		}
		if !strings.HasPrefix(why, "blocked:") && !strings.HasPrefix(why, "rate-limited:") {
			t.Fatalf("stranger rejection reason %q", why)
		}
	}
	// past the slowest tier everything stored is visible to all: wait for the releases (a loaded runner
	// can lag), then assert on the settled state
	time.Sleep(scaledDelay("2", 120))
	for end := time.Now().Add(harnessWait); time.Now().Before(end); time.Sleep(250 * time.Millisecond) { // under the 5 req/s zero-power rate
		if n, _ := reader.count(map[string]any{"kinds": []int{protocol.KindSignal}}); n >= 2 {
			break
		}
	}
	ids, _ := reader.query(map[string]any{"kinds": []int{protocol.KindSignal}})
	n, _ := reader.count(map[string]any{"kinds": []int{protocol.KindSignal}})
	sids, _ := reader.query(map[string]any{"authors": []string{stranger.pub}})
	t.Logf("stored: REQ=%d COUNT=%d; stranger events REQ=%d", len(ids), n, len(sids))
	if len(ids) != 2 || n != 2 || len(sids) != 0 {
		t.Errorf("want exactly 2 stored signals (provider key + delegated device key) and none from the stranger")
	}
}
