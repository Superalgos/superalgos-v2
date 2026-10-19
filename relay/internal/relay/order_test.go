package relay_test

import (
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/protocol"
)

// stopSignal builds an `action: close` stop signal with the given created_ms (same content shape as signalBy).
func stopSignal(a actor, tf int64, seq int, createdMS int64, kind string) *nostr.Event {
	content := fmt.Sprintf(`{"v":2,"strategy_id":"s1","seq":%d,"prev":null,"instrument":"BINANCE:BTCUSDT","side":"sell","action":"%s","size_fraction":1,"order_type":"stop","stop_price":59000,"timeframe":%d,"created_ms":%d,"bar_open_ms":%d}`, seq, kind, tf, createdMS, createdMS-tf*1000)
	ev := &nostr.Event{PubKey: a.pub, CreatedAt: nostr.Now(), Kind: protocol.KindSignal, Tags: nostr.Tags{a.delegation(time.Now().Add(time.Hour).Unix())}, Content: content}
	_ = ev.Sign(a.sk)
	return ev
}

// arrivalOrder is the ids of a client's received events in arrival order, once each.
func arrivalOrder(cl *client) []string {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	seen := map[string]bool{}
	var out []string
	for _, e := range cl.events {
		if !seen[e.id] {
			seen[e.id] = true
			out = append(out, e.id)
		}
	}
	return out
}

// TestSameMillisecondOrder: signals of one provider that the relay received in the same millisecond (an
// entry and its protective stop) reach every tier in relay receipt order, so a follower never sees the stop
// before the order it protects, and does not depend on its own reorder window for correctness.
func TestSameMillisecondOrder(t *testing.T) {
	r := startRelay(t, "", noThrottle())
	prov := registerProvider(t, r, 60, defaultRules)
	bal := map[string]string{}
	split := map[string]uint32{prov.pub: 10000}
	var fs []*follower
	for tier, n := range fixedTiers {
		for copy := 0; copy < 2; copy++ { // two subscribers per tier: the order must hold for each
			a := newActor()
			fundFollower(t, r, bal, a, n, 1, split)
			f := &follower{name: fmt.Sprintf("T%d.%d", tier, copy), tier: tier, cl: dial(t, r.URL), a: a}
			f.cl.login(r.URL, a)
			fs = append(fs, f)
		}
	}
	fs = append(fs, &follower{name: "T3 anon", tier: 3, cl: dial(t, r.URL)})
	for _, f := range fs {
		f.cl.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	}
	pub := dial(t, r.URL)

	const pairs = 60
	var want []string
	same := 0
	for i := 0; i < pairs; i++ {
		ms := time.Now().UnixMilli()
		open := signalBy(prov.sk, nostr.Tags{prov.delegation(time.Now().Add(time.Hour).Unix())}, 60, 2*i+1, ms)
		stop := stopSignal(prov, 60, 2*i+2, ms, "close")
		pub.mustPublish(open)
		pub.mustPublish(stop)
		want = append(want, open.ID, stop.ID)
		if time.Now().UnixMilli() == ms {
			same++
		}
	}
	t.Logf("%d pairs published, %d fully inside one millisecond", pairs, same)

	last := want[len(want)-1]
	for _, f := range fs {
		if !f.cl.waitFor(last, harnessWait) {
			t.Fatalf("%s did not receive the last signal", f.name)
		}
	}
	for _, f := range fs {
		got := arrivalOrder(f.cl)
		if len(got) != len(want) {
			t.Errorf("%s received %d/%d signals", f.name, len(got), len(want))
			continue
		}
		for i := range want {
			if got[i] != want[i] {
				t.Errorf("%s: delivery %d is %s, relay receipt order says %s (open+stop swapped)", f.name, i, got[i][:8], want[i][:8])
				break
			}
		}
	}
}

// TestConcurrentBurstSameOrderAcrossTiers: signals published at once from several connections arrive in
// whatever order the relay received them, and every tier sees that same order.
func TestConcurrentBurstSameOrderAcrossTiers(t *testing.T) {
	r := startRelay(t, "", noThrottle())
	prov := registerProvider(t, r, 60, defaultRules)
	bal := map[string]string{}
	split := map[string]uint32{prov.pub: 10000}
	var fs []*follower
	for tier, n := range fixedTiers {
		a := newActor()
		fundFollower(t, r, bal, a, n, 1, split)
		f := &follower{name: fmt.Sprintf("T%d", tier), tier: tier, cl: dial(t, r.URL), a: a}
		f.cl.login(r.URL, a)
		fs = append(fs, f)
	}
	fs = append(fs, &follower{name: "T3 anon", tier: 3, cl: dial(t, r.URL)})
	for _, f := range fs {
		f.cl.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	}

	const n = 40
	conns := make([]*client, 8)
	for i := range conns {
		conns[i] = dial(t, r.URL)
	}
	ms := time.Now().UnixMilli()
	ids := make([]string, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		ev := signalBy(prov.sk, nostr.Tags{prov.delegation(time.Now().Add(time.Hour).Unix())}, 60, i+1, ms)
		ids[i] = ev.ID
		wg.Add(1)
		go func(c *client) { defer wg.Done(); c.mustPublish(ev) }(conns[i%len(conns)])
	}
	wg.Wait()
	for _, f := range fs {
		for _, id := range ids {
			if !f.cl.waitFor(id, harnessWait) {
				t.Fatalf("%s missed a signal of the burst", f.name)
			}
		}
	}
	ref := arrivalOrder(fs[0].cl)
	for _, f := range fs[1:] {
		got := arrivalOrder(f.cl)
		for i := range ref {
			if got[i] != ref[i] {
				t.Errorf("%s saw delivery %d as %s, T0 saw %s: tiers disagree on the order", f.name, i, got[i][:8], ref[i][:8])
				break
			}
		}
	}
}
