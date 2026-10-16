package state

import (
	"encoding/json"
	"fmt"
	"math/big"
	"strings"
	"testing"
	"time"

	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/dgraph-io/badger/v4"
	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/evmsig/evmsigtest"
	"github.com/superalgos/sarelay/internal/protocol"
)

type who struct {
	k    *secp256k1.PrivateKey
	addr string
	sk   string
	pub  string
}

func newWho() who {
	k, _ := secp256k1.GeneratePrivateKey()
	sk := nostr.GeneratePrivateKey()
	pub, _ := nostr.GetPublicKey(sk)
	return who{k, evmsigtest.AddressOf(k), sk, pub}
}

// ruleSet builds a signed rule set; version orders the rule sets of one provider (it is added to created_at).
func (w who) ruleSet(t *testing.T, version uint64, tf int64) *nostr.Event {
	t.Helper()
	sig := evmsigtest.Sign(w.k, protocol.RuleSetMessage(w.pub, tf, "0@1.0000"))
	c, _ := json.Marshal(map[string]any{"v": 1, "provider": w.pub, "sa_address": w.addr, "timeframe": tf,
		"tiers": []map[string]any{{"min_power": 0, "delay_fraction": 1.0}}, "sig": sig})
	ev := &nostr.Event{PubKey: w.pub, CreatedAt: nostr.Timestamp(1_000_000 + int64(version)), Kind: protocol.KindRuleSet, Tags: nostr.Tags{{"d", w.pub}}, Content: string(c)}
	_ = ev.Sign(w.sk)
	return ev
}

func (w who) alloc(t *testing.T, nonce uint64, ps map[string]uint32) *nostr.Event {
	t.Helper()
	a := &protocol.Allocation{Address: w.addr, Nonce: nonce, Providers: ps}
	c, _ := json.Marshal(map[string]any{"v": 1, "address": w.addr, "nonce": nonce, "governance": 0, "providers": ps, "sig": evmsigtest.Sign(w.k, a.Message())})
	ev := &nostr.Event{PubKey: w.pub, CreatedAt: nostr.Now(), Kind: protocol.KindAllocation, Content: string(c),
		Tags: nostr.Tags{evmsigtest.SignDelegation(w.k, w.pub, 4_000_000_000)}}
	_ = ev.Sign(w.sk)
	return ev
}

type mem struct{ rs, al []Saved }

func (m *mem) SaveRuleSet(_ string, sv Saved) error { m.rs = append(m.rs, sv); return nil }
func (m *mem) DeleteRuleSet(string) error           { return nil }
func (m *mem) SaveAllocation(_ string, _ uint64, _ string, sv Saved) error {
	m.al = append(m.al, sv)
	return nil
}
func (m *mem) DeleteAllocation(string, uint64, string) error { return nil }
func (m *mem) SaveRevocation(string, string, Saved) error    { return nil }
func (m *mem) DeleteRevocation(string, string) error         { return nil }
func (m *mem) SaveActivity(string, time.Time) error          { return nil }
func (m *mem) DeleteActivity(string) error                   { return nil }

// at is a relay clock far after the fixture created_at values.
var at0 = time.Unix(2_000_000, 0)

func TestRuleSetRegistration(t *testing.T) {
	d := NewDirectory(protocol.DefaultLimits(), 0, 2, 0, &mem{})
	a, b, c := newWho(), newWho(), newWho()
	commit := func(w who, nonce uint64) error {
		ev := w.ruleSet(t, nonce, 60)
		rs, err := d.CheckRuleSet(ev, at0)
		if err != nil {
			return err
		}
		return d.CommitRuleSet(ev, rs, at0, nil)
	}
	if err := commit(a, 1); err != nil {
		t.Fatal(err)
	}
	if _, ok := d.Provider(a.pub); !ok {
		t.Fatal("not registered")
	}
	if p, ok := d.ProviderForAddress(a.addr); !ok || p != a.pub {
		t.Fatal("address index missing")
	}
	if err := commit(a, 1); err == nil || !strings.Contains(err.Error(), "not newer") {
		t.Errorf("same nonce: %v", err)
	}
	if err := commit(a, 2); err != nil {
		t.Errorf("higher nonce: %v", err)
	}
	// the same SA address cannot register a second provider key
	second := newWho()
	second.k, second.addr = a.k, a.addr
	if _, err := d.CheckRuleSet(second.ruleSet(t, 1, 60), at0); err == nil || !strings.Contains(err.Error(), "already bound") {
		t.Errorf("second key for one SA address: %v", err)
	}
	if err := commit(b, 1); err != nil {
		t.Fatal(err)
	}
	if err := commit(c, 1); err == nil || !strings.Contains(err.Error(), "maximum") {
		t.Errorf("provider cap: %v", err)
	}
}

func TestAllocationRules(t *testing.T) {
	d := NewDirectory(protocol.DefaultLimits(), 10*time.Second, 0, 0, &mem{})
	w := newWho()
	p := strings.Repeat("a", 64)
	t0 := time.Unix(10_000, 0)
	put := func(nonce uint64, bps uint32, at time.Time) error {
		ev := w.alloc(t, nonce, map[string]uint32{p: bps})
		a, err := d.CheckAllocation(ev, at)
		if err != nil {
			return err
		}
		return d.CommitAllocation(ev, a, at, nil)
	}
	if got := d.BPS(w.addr, p, t0); got != 0 {
		t.Fatalf("no allocation yet: %d", got)
	}
	if err := put(1, 1000, t0); err != nil {
		t.Fatal(err)
	}
	if err := put(2, 9000, t0.Add(20*time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := put(1, 7000, t0.Add(21*time.Second)); err == nil {
		t.Error("older nonce accepted")
	}
	for _, c := range []struct {
		at   time.Duration
		want uint32
	}{{0, 0}, {9 * time.Second, 0}, {10 * time.Second, 1000}, {29 * time.Second, 1000}, {30 * time.Second, 9000}, {time.Hour, 9000}} {
		if got := d.BPS(w.addr, p, t0.Add(c.at)); got != c.want {
			t.Errorf("t0+%s: bps %d, want %d", c.at, got, c.want)
		}
	}
	// SEP-3.22: power is the allocated share of the total, never the total
	total := new(big.Int).Mul(big.NewInt(1_000_000), protocol.WeiPerToken())
	got := d.Power(total, w.addr, p, t0.Add(time.Hour))
	want := new(big.Int).Mul(big.NewInt(900_000), protocol.WeiPerToken())
	if got.Cmp(want) != 0 {
		t.Errorf("power %s want %s", got, want)
	}
	if d.Power(total, w.addr, strings.Repeat("b", 64), t0.Add(time.Hour)).Sign() != 0 {
		t.Error("an unallocated provider got power")
	}
	if d.Power(nil, w.addr, p, t0).Sign() != 0 {
		t.Error("nil total")
	}
}

func TestAllocationMinInterval(t *testing.T) {
	d := NewDirectory(protocol.DefaultLimits(), 0, 0, 5*time.Second, &mem{})
	w := newWho()
	t0 := time.Unix(1000, 0)
	ev1 := w.alloc(t, 1, nil)
	a, err := d.CheckAllocation(ev1, t0)
	if err != nil {
		t.Fatal(err)
	}
	_ = d.CommitAllocation(ev1, a, t0, nil)
	if _, err := d.CheckAllocation(w.alloc(t, 2, nil), t0.Add(time.Second)); err == nil || !strings.Contains(err.Error(), "rate-limited") {
		t.Errorf("fast second allocation: %v", err)
	}
	if _, err := d.CheckAllocation(w.alloc(t, 2, nil), t0.Add(6*time.Second)); err != nil {
		t.Errorf("after the interval: %v", err)
	}
}

func TestStoreRoundTripAndPending(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	w, p := newWho(), strings.Repeat("c", 64)
	d := NewDirectory(protocol.DefaultLimits(), 0, 0, 0, s)
	rev := w.ruleSet(t, 3, 120)
	rs, _ := d.CheckRuleSet(rev, at0)
	if err := d.CommitRuleSet(rev, rs, time.Unix(50, 0), nil); err != nil {
		t.Fatal(err)
	}
	aev := w.alloc(t, 4, map[string]uint32{p: 2500})
	al, err := d.CheckAllocation(aev, time.Unix(60, 0))
	if err != nil {
		t.Fatal(err)
	}
	if err := d.CommitAllocation(aev, al, time.Unix(60, 0), nil); err != nil {
		t.Fatal(err)
	}
	id1, id2, id3 := strings.Repeat("11", 32), strings.Repeat("22", 32), strings.Repeat("33", 32)
	m1 := &Meta{ID: id1, Kind: 9317, Provider: w.pub, Received: time.Now().Add(-time.Hour).UnixNano(), MaxDelay: 1}
	m2 := &Meta{ID: id2, Kind: 9317, Provider: w.pub, Received: time.Now().UnixNano(), Tiers: []MetaTier{{MinWei: "0", Delay: 5}}, MaxDelay: 5}
	m3 := &Meta{ID: id3, Kind: 9317, Received: time.Now().UnixNano()}
	for _, m := range []*Meta{m1, m2, m3} {
		if err := s.PutMeta(m); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.ClearPending(m1); err != nil {
		t.Fatal(err)
	}
	if err := s.DropMeta(m3); err != nil {
		t.Fatal(err)
	}
	s.Close()

	s, err = Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if got, _ := s.Meta(id1); got == nil || got.Provider != w.pub {
		t.Errorf("meta of a cleared-pending event must stay readable: %+v", got)
	}
	if got, _ := s.Meta(id3); got != nil {
		t.Error("dropped meta still present")
	}
	pend, err := s.Pending(time.Unix(0, 0))
	if err != nil || len(pend) != 1 || pend[0].ID != id2 || pend[0].Tiers[0].Delay != 5 {
		t.Fatalf("pending = %+v, %v", pend, err)
	}
	rules, _, _ := s.LoadRuleSets()
	allocs, _, _ := s.LoadAllocations()
	d2 := NewDirectory(protocol.DefaultLimits(), 0, 0, 0, s)
	if bad := d2.Load(rules, allocs, nil, nil); len(bad) != 0 {
		t.Fatalf("quarantined %v", bad)
	}
	if _, ok := d2.Provider(w.pub); !ok {
		t.Error("rule set lost")
	}
	if got := d2.BPS(w.addr, p, time.Unix(100, 0)); got != 2500 {
		t.Errorf("allocation lost: %d", got)
	}
	if p, a := d2.Counts(); p != 1 || a != 1 {
		t.Errorf("counts %d %d", p, a)
	}
	// corrupting a stored record is detected, not trusted
	rules[0].Event.Content = strings.Replace(rules[0].Event.Content, `"timeframe":120`, `"timeframe":121`, 1)
	d3 := NewDirectory(protocol.DefaultLimits(), 0, 0, 0, s)
	if bad := d3.Load(rules, allocs, nil, nil); len(bad) != 1 || !strings.Contains(bad[0].Key, "ruleset") {
		t.Errorf("tampered rule set not quarantined: %v", bad)
	}
	if _, ok := d3.Provider(w.pub); ok {
		t.Error("a tampered rule set was loaded")
	}
	if got := d3.BPS(w.addr, p, time.Unix(100, 0)); got != 2500 {
		t.Errorf("one bad rule set must not take the allocations down: bps %d", got)
	}
	_ = fmt.Sprint
}

// Equal-nonce allocations (equivocation by the SA identity) resolve to the smallest event id, in
// every arrival order, exactly like sa_core.power.allocation_in_force.
func TestEqualNonceAllocationsResolveBySmallestEventID(t *testing.T) {
	w := newWho()
	p := strings.Repeat("a", 64)
	t0 := time.Unix(10_000, 0)
	evs := []*nostr.Event{
		w.alloc(t, 3, map[string]uint32{p: 1000}),
		w.alloc(t, 3, map[string]uint32{p: 2000}),
		w.alloc(t, 3, map[string]uint32{p: 3000}),
	}
	want := map[string]uint32{}
	smallest := evs[0]
	for _, e := range evs {
		var c struct {
			Providers map[string]uint32 `json:"providers"`
		}
		_ = json.Unmarshal([]byte(e.Content), &c)
		want[e.ID] = c.Providers[p]
		if e.ID < smallest.ID {
			smallest = e
		}
	}
	for _, order := range [][]int{{0, 1, 2}, {2, 1, 0}, {1, 2, 0}} {
		d := NewDirectory(protocol.DefaultLimits(), 0, 0, 0, &mem{})
		for k, i := range order {
			at := t0.Add(time.Duration(k) * time.Second)
			a, err := d.CheckAllocation(evs[i], at)
			if err != nil {
				t.Fatalf("order %v: equal nonce with a different event id must be accepted: %v", order, err)
			}
			if err := d.CommitAllocation(evs[i], a, at, nil); err != nil {
				t.Fatal(err)
			}
		}
		if got := d.BPS(w.addr, p, t0.Add(time.Hour)); got != want[smallest.ID] {
			t.Errorf("order %v: bps %d, want %d (smallest event id %s)", order, got, want[smallest.ID], smallest.ID[:12])
		}
		if _, err := d.CheckAllocation(evs[0], t0.Add(time.Hour)); err == nil {
			t.Errorf("order %v: the same event accepted twice", order)
		}
	}
	// only effective records compete: a smaller id that is still inside D does not win yet
	d := NewDirectory(protocol.DefaultLimits(), 10*time.Second, 0, 0, &mem{})
	hi, lo := evs[0], evs[1]
	if lo.ID > hi.ID {
		hi, lo = lo, hi
	}
	for _, c := range []struct {
		ev *nostr.Event
		at time.Time
	}{{hi, t0}, {lo, t0.Add(20 * time.Second)}} {
		a, err := d.CheckAllocation(c.ev, c.at)
		if err != nil {
			t.Fatal(err)
		}
		_ = d.CommitAllocation(c.ev, a, c.at, nil)
	}
	if got, exp := d.BPS(w.addr, p, t0.Add(15*time.Second)), want[hi.ID]; got != exp {
		t.Errorf("inside D of the smaller id: bps %d, want %d (the larger id, already effective)", got, exp)
	}
	if got, exp := d.BPS(w.addr, p, t0.Add(30*time.Second)), want[lo.ID]; got != exp {
		t.Errorf("after D: bps %d, want %d (the smaller id)", got, exp)
	}
}

type failingSave struct{ err error }

func (f failingSave) save() error { return f.err }

// recorder lets a test see which records the directory wrote and removed.
type recorder struct {
	mem
	puts, dels []string
}

func (r *recorder) SaveAllocation(a string, n uint64, id string, sv Saved) error {
	r.puts = append(r.puts, "alloc:"+id)
	return r.mem.SaveAllocation(a, n, id, sv)
}
func (r *recorder) DeleteAllocation(a string, n uint64, id string) error {
	r.dels = append(r.dels, "alloc:"+id)
	return nil
}
func (r *recorder) SaveRuleSet(p string, sv Saved) error {
	r.puts = append(r.puts, "rs:"+sv.Event.ID)
	return r.mem.SaveRuleSet(p, sv)
}
func (r *recorder) DeleteRuleSet(p string) error { r.dels = append(r.dels, "rs:"+p); return nil }
func (r *recorder) SaveRevocation(a, dev string, sv Saved) error {
	r.puts = append(r.puts, "rev:"+sv.Event.ID)
	return nil
}
func (r *recorder) DeleteRevocation(a, dev string) error {
	r.dels = append(r.dels, "rev:"+dev)
	return nil
}

// H1 at the directory: an allocation is accepted only from a device key delegated by its own address.
func TestAllocationNeedsDelegationFromItsOwnAddress(t *testing.T) {
	d := NewDirectory(protocol.DefaultLimits(), 0, 0, 5*time.Second, &mem{})
	victim, thief := newWho(), newWho()
	p := strings.Repeat("a", 64)
	t0 := time.Unix(10_000, 0)
	orig := victim.alloc(t, 1, map[string]uint32{p: 1000})
	a, err := d.CheckAllocation(orig, t0)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.CommitAllocation(orig, a, t0, nil); err != nil {
		t.Fatal(err)
	}
	// no tag
	bare := &nostr.Event{PubKey: thief.pub, CreatedAt: nostr.Now(), Kind: protocol.KindAllocation, Content: orig.Content}
	_ = bare.Sign(thief.sk)
	if _, err := d.CheckAllocation(bare, t0.Add(time.Hour)); err == nil || !strings.Contains(err.Error(), "sa-delegation") {
		t.Errorf("re-wrap without a tag: %v", err)
	}
	// the thief's own valid delegation names the thief's address
	own := &nostr.Event{PubKey: thief.pub, CreatedAt: nostr.Now(), Kind: protocol.KindAllocation, Content: orig.Content,
		Tags: nostr.Tags{evmsigtest.SignDelegation(thief.k, thief.pub, 4_000_000_000)}}
	_ = own.Sign(thief.sk)
	if _, err := d.CheckAllocation(own, t0.Add(time.Hour)); err == nil || !strings.Contains(err.Error(), "delegation is from") {
		t.Errorf("re-wrap with the thief's own delegation: %v", err)
	}
	// expired delegation
	old := &nostr.Event{PubKey: victim.pub, CreatedAt: nostr.Now(), Kind: protocol.KindAllocation, Content: victim.alloc(t, 2, map[string]uint32{p: 5}).Content,
		Tags: nostr.Tags{evmsigtest.SignDelegation(victim.k, victim.pub, 9_000)}}
	_ = old.Sign(victim.sk)
	if _, err := d.CheckAllocation(old, t0); err == nil {
		t.Error("expired delegation accepted")
	}
	// nothing the thief tried restarted the victim's interval
	if _, err := d.CheckAllocation(victim.alloc(t, 2, map[string]uint32{p: 2000}), t0.Add(6*time.Second)); err != nil {
		t.Errorf("victim blocked after the attempts: %v", err)
	}
}

// L5: when the event write fails, the directory record is removed again and memory is untouched.
func TestCommitRollsBackWhenTheEventWriteFails(t *testing.T) {
	boom := fmt.Errorf("disk full")
	rec := &recorder{}
	d := NewDirectory(protocol.DefaultLimits(), 0, 0, 0, rec)
	w := newWho()
	p := strings.Repeat("a", 64)
	ev := w.alloc(t, 1, map[string]uint32{p: 1000})
	a, err := d.CheckAllocation(ev, at0)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.CommitAllocation(ev, a, at0, failingSave{boom}.save); err != boom {
		t.Fatalf("commit error %v, want the save error", err)
	}
	if len(rec.puts) != 1 || len(rec.dels) != 1 {
		t.Errorf("record writes %v, removals %v: want one of each", rec.puts, rec.dels)
	}
	if _, n := d.Counts(); n != 0 || d.BPS(w.addr, p, at0.Add(time.Hour)) != 0 {
		t.Error("a failed commit left the allocation in memory")
	}

	rs := w.ruleSet(t, 1, 60)
	r, err := d.CheckRuleSet(rs, at0)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.CommitRuleSet(rs, r, at0, failingSave{boom}.save); err != boom {
		t.Fatalf("rule set commit error %v", err)
	}
	if _, ok := d.Provider(w.pub); ok {
		t.Error("a failed rule-set commit registered the provider")
	}
}

// M3 at the directory: earliest effective_at wins, only revoked-as-of-now counts, and Known/Revoked
// are what the oracle and the policy consult.
func TestRevocationsAndKnownAddresses(t *testing.T) {
	d := NewDirectory(protocol.DefaultLimits(), 0, 0, 0, &mem{})
	sa, dev := newWho(), newWho()
	mk := func(at int64) *nostr.Event {
		c, _ := json.Marshal(map[string]any{"v": 1, "device_pubkey": dev.pub, "effective_at": at, "address": sa.addr,
			"sig": evmsigtest.Sign(sa.k, protocol.RevocationMessage(dev.pub, at))})
		ev := &nostr.Event{PubKey: newWho().pub, CreatedAt: nostr.Now(), Kind: protocol.KindRevocation, Content: string(c)}
		_ = ev.Sign(nostr.GeneratePrivateKey())
		return ev
	}
	now := time.Unix(5_000, 0)
	if d.Revoked(sa.addr, dev.pub, now) {
		t.Fatal("revoked before any revocation")
	}
	ev := mk(6_000)
	r, err := d.CheckRevocation(ev)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.CommitRevocation(ev, r, now, nil); err != nil {
		t.Fatal(err)
	}
	if d.Revoked(sa.addr, dev.pub, now) || !d.Revoked(sa.addr, dev.pub, time.Unix(6_000, 0)) {
		t.Error("a revocation must apply from effective_at, not before")
	}
	if _, err := d.CheckRevocation(mk(7_000)); err == nil {
		t.Error("a later effective_at is a duplicate")
	}
	if ev2 := mk(5_500); true {
		r2, err := d.CheckRevocation(ev2)
		if err != nil {
			t.Fatal(err)
		}
		_ = d.CommitRevocation(ev2, r2, now, nil)
	}
	if !d.Revoked(sa.addr, dev.pub, time.Unix(5_500, 0)) {
		t.Error("an earlier effective_at must tighten the revocation")
	}
	if d.Revoked(sa.addr, newWho().pub, time.Unix(9_000, 0)) {
		t.Error("revoking one device revoked another")
	}

	// Known: only addresses with an allocation or a registered provider are worth a lookup
	if d.Known(sa.addr) {
		t.Error("unknown address reported as known")
	}
	al := sa.alloc(t, 1, map[string]uint32{strings.Repeat("a", 64): 1})
	a, _ := d.CheckAllocation(al, now)
	_ = d.CommitAllocation(al, a, now, nil)
	if !d.Known(sa.addr) {
		t.Error("allocating address not known")
	}
	prov := newWho()
	rs := prov.ruleSet(t, 1, 60)
	r3, _ := d.CheckRuleSet(rs, at0)
	_ = d.CommitRuleSet(rs, r3, at0, nil)
	if !d.Known(prov.addr) {
		t.Error("provider address not known")
	}
}

// H2 at the directory: release delay cap, far-future created_at, provider expiry and removal.
func TestRuleSetCapsAndProviderUpkeep(t *testing.T) {
	lim := protocol.DefaultLimits()
	lim.MaxReleaseDelay = time.Hour
	d := NewDirectory(lim, 0, 0, 0, &mem{})
	w := newWho()
	// timeframe 3600 s x delay 1.0 = 1 h: at the cap, accepted; 7200 s x 1.0 = 2 h: refused
	if _, err := d.CheckRuleSet(w.ruleSet(t, 1, 3600), at0); err != nil {
		t.Errorf("exactly max_release_delay: %v", err)
	}
	if _, err := d.CheckRuleSet(w.ruleSet(t, 1, 7200), at0); err == nil || !strings.Contains(err.Error(), "max_release_delay") {
		t.Errorf("2 h delay accepted under a 1 h cap: %v", err)
	}
	// created_at far in the future would pin the provider's rules
	if _, err := d.CheckRuleSet(w.ruleSet(t, 1, 3600), time.Unix(1_000_000-3600, 0)); err == nil || !strings.Contains(err.Error(), "ahead of the relay clock") {
		t.Errorf("future rule set accepted: %v", err)
	}
	if _, err := d.CheckRuleSet(w.ruleSet(t, 1, 3600), time.Unix(1_000_000-5, 0)); err != nil {
		t.Errorf("within skew refused: %v", err)
	}

	ev := w.ruleSet(t, 1, 3600)
	r, _ := d.CheckRuleSet(ev, at0)
	_ = d.CommitRuleSet(ev, r, at0, nil)
	if idle := d.Idle(at0.Add(-time.Hour)); len(idle) != 0 {
		t.Errorf("fresh provider idle: %v", idle)
	}
	d.Touch(w.pub, at0.Add(10*time.Hour))
	if idle := d.Idle(at0.Add(5 * time.Hour)); len(idle) != 0 {
		t.Errorf("a provider with a recent signal was idle: %v", idle)
	}
	if idle := d.Idle(at0.Add(11 * time.Hour)); len(idle) != 1 {
		t.Errorf("idle provider not listed: %v", idle)
	}
	if err := d.Remove(w.pub); err != nil {
		t.Fatal(err)
	}
	if _, ok := d.ProviderForAddress(w.addr); ok {
		t.Error("removed provider still bound to its address")
	}
	if _, ok := d.Provider(w.pub); ok {
		t.Error("removed provider still registered")
	}
}

// L5: a corrupt stored record is quarantined by the store loader, not fatal.
func TestStoreLoadQuarantinesUndecodableRecords(t *testing.T) {
	s, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	w := newWho()
	ev := w.ruleSet(t, 1, 60)
	if err := s.SaveRuleSet(w.pub, Saved{Event: ev, Received: 1}); err != nil {
		t.Fatal(err)
	}
	if err := s.Update(func(tx *badger.Txn) error {
		return tx.Set(append([]byte{prefixRuleSet}, "garbage"...), []byte("{nope"))
	}); err != nil {
		t.Fatal(err)
	}
	rules, bad, err := s.LoadRuleSets()
	if err != nil {
		t.Fatalf("a corrupt record failed the whole load: %v", err)
	}
	if len(rules) != 1 || len(bad) != 1 || !strings.Contains(bad[0].Reason, "undecodable") {
		t.Errorf("rules %d quarantined %v", len(rules), bad)
	}
}
