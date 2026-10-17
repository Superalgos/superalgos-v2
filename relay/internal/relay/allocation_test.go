package relay_test

import (
	"strings"
	"testing"
	"time"

	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/evmsig/evmsigtest"
	"github.com/superalgos/sarelay/internal/protocol"
)

// SEP-3.32 and SEP-3.33 over the wire: only the SA identity's signature counts, nonces only grow,
// shares never exceed 100%, and the whole history stays queryable.
func TestAllocationAcceptance(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	f, other := newActor(), newActor()
	p1, p2 := strings.Repeat("a", 64), strings.Repeat("b", 64)
	al := func(nonce uint64, gov uint32, ps map[string]uint32) *protocol.Allocation {
		return &protocol.Allocation{Address: f.addr, Nonce: nonce, Governance: gov, Providers: ps}
	}
	for _, tc := range []struct {
		name string
		ev   func() (ok bool, why string)
		ok   bool
	}{
		{"valid, nonce 1", func() (bool, string) { return c.publish(f.allocation(1, 3000, map[string]uint32{p1: 4000})) }, true},
		{"signed by another EVM key", func() (bool, string) {
			return c.publish(f.allocationSignedBy(other.evm, al(2, 0, map[string]uint32{p1: 100})))
		}, false},
		{"same nonce, other event (equivocation)", func() (bool, string) { return c.publish(f.allocation(1, 0, map[string]uint32{p1: 100})) }, true},
		{"older nonce", func() (bool, string) { return c.publish(f.allocation(0, 0, map[string]uint32{p1: 100})) }, false},
		{"sum 10001 bps", func() (bool, string) { return c.publish(f.allocation(5, 5001, map[string]uint32{p1: 5000})) }, false},
		{"sum 100% exactly", func() (bool, string) { return c.publish(f.allocation(6, 0, map[string]uint32{p1: 6000, p2: 4000})) }, true},
		{"provider key not hex", func() (bool, string) { return c.publish(f.allocation(7, 0, map[string]uint32{"nope": 10})) }, false},
		{"newer nonce, smaller", func() (bool, string) { return c.publish(f.allocation(9, 0, map[string]uint32{p2: 1})) }, true},
	} {
		ok, why := tc.ev()
		t.Logf("%-28s ok=%-5v %s", tc.name, ok, why)
		if ok != tc.ok {
			t.Errorf("%s: ok=%v want %v (%s)", tc.name, ok, tc.ok, why)
		}
		if !ok && !strings.HasPrefix(why, "invalid:") {
			t.Errorf("%s: reason %q should start with invalid:", tc.name, why)
		}
	}
	// history kept: all three accepted allocations are stored; rejected ones are not
	ids, _ := c.query(map[string]any{"kinds": []int{protocol.KindAllocation}})
	if len(ids) != 4 {
		t.Errorf("allocation history has %d events, want 4 (nonce 1 twice, 6, 9)", len(ids))
	}
}

// SEP-3.35 relay side: an allocation counts only after D. Until then the previous one stays in force.
func TestAllocationEffectiveAfterD(t *testing.T) { allocationEffectiveAfterD(t) }

// TestPerfAllocationEffectiveAfterD is the machine-speed half: once the allocation counts, the top tier is
// served within 100 ms. Local only (SA_PERF=1, `make perf`).
func TestPerfAllocationEffectiveAfterD(t *testing.T) {
	perfOnly(t)
	if got := allocationEffectiveAfterD(t); got > 100*time.Millisecond {
		t.Errorf("allocation counted after D: delivered after %s, budget 100ms", got)
	}
}

// allocationEffectiveAfterD checks the allocation is ignored before D (last tier, never early) and counted
// after it (served well before the last tier's 2 s); it returns the post-D delivery time.
func allocationEffectiveAfterD(t *testing.T) time.Duration {
	t.Helper()
	const d = 2500 * time.Millisecond
	r := startRelay(t, "", withDelay(d))
	prov := registerProvider(t, r, 60, []tierSpec{{"1000", "0"}, {"0", "2"}}) // top: 0, else 2 s
	bal := map[string]string{}
	f := newActor()
	bal[f.addr] = "1000"
	r.SetBalances(bal)
	pub := dial(t, r.URL)
	pub.mustPublish(f.allocation(1, 0, map[string]uint32{prov.pub: 10000}))
	received := time.Now()

	cl := dial(t, r.URL)
	cl.login(r.URL, f)
	cl.live(map[string]any{"kinds": []int{protocol.KindSignal}})

	early := prov.signal(60, 1)
	t0 := time.Now()
	pub.mustPublish(early)
	if !cl.waitFor(early.ID, harnessWait) {
		t.Fatal("no delivery")
	}
	at, _ := cl.firstArrival(early.ID)
	if got := at.Sub(t0); got < 1950*time.Millisecond {
		t.Errorf("allocation counted before D: delivered after %s, want about 2s (last tier)", got)
	}

	time.Sleep(time.Until(received.Add(d + 200*time.Millisecond)))
	late := prov.signal(60, 2)
	t1 := time.Now()
	pub.mustPublish(late)
	if !cl.waitFor(late.ID, harnessWait) {
		t.Fatal("no delivery after D")
	}
	at, _ = cl.firstArrival(late.ID)
	got := at.Sub(t1)
	// the last tier would take 2 s: anything well under it proves the allocation counted
	if got > 1500*time.Millisecond {
		t.Errorf("allocation not counted after D: delivered after %s, want about 0 (last tier is 2s)", got)
	}
	return got
}

// Allocated power, not balance: dropping the allocation to another provider lowers the tier with this one.
func TestReallocationMovesTier(t *testing.T) {
	r := startRelay(t, "")
	prov := registerProvider(t, r, 60, []tierSpec{{"1000", "0"}, {"0", "2"}})
	bal := map[string]string{}
	f := newActor()
	fundFollower(t, r, bal, f, 1000, 1, map[string]uint32{prov.pub: 10000})
	pub := dial(t, r.URL)
	cl := dial(t, r.URL)
	cl.login(r.URL, f)
	cl.live(map[string]any{"kinds": []int{protocol.KindSignal}})

	first := prov.signal(60, 1)
	pub.mustPublish(first)
	if !cl.waitFor(first.ID, harnessWait) {
		t.Fatal("with 100% allocated the follower should get the signal at once")
	}
	pub.mustPublish(f.allocation(2, 0, map[string]uint32{prov.pub: 5000})) // 500 < 1000
	second := prov.signal(60, 2)
	t0 := time.Now()
	pub.mustPublish(second)
	if !cl.waitFor(second.ID, harnessWait) {
		t.Fatal("no delivery")
	}
	at, _ := cl.firstArrival(second.ID)
	if got := at.Sub(t0); got < 1950*time.Millisecond {
		t.Errorf("after halving the allocation the delay should be the last tier's 2 s, got %s", got)
	}
}

// Delegations: a device key delegated by the SA identity can post allocations; a mismatched delegation cannot.
func TestAllocationDelegationMustMatchAddress(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	f, mallory := newActor(), newActor()
	ev := f.allocation(1, 100, nil)
	// swap the delegation for one from a different SA address
	ev.Tags = nostr.Tags{evmsigtest.SignDelegation(mallory.evm, f.pub, time.Now().Add(time.Hour).Unix())}
	_ = ev.Sign(f.sk)
	ok, why := c.publish(ev)
	if ok || !strings.Contains(why, "delegation") {
		t.Errorf("ok=%v reason=%q, want a delegation mismatch", ok, why)
	}
}
