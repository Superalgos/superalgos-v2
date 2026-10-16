package relay_test

import (
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/superalgos/sarelay/internal/evmsig/evmsigtest"
	"github.com/superalgos/sarelay/internal/protocol"
)

var fixedTiers = []int{2_000_000, 200_000, 20_000} // balances for tiers 0..2; tier 3 is anonymous

type follower struct {
	name string
	tier int
	cl   *client
	a    actor
}

// connectTiers opens one follower per tier: authenticated T0..T2 with fixture balances fully allocated
// to prov, T3 anonymous, plus two T3 variants (authenticated with zero SA, a forged delegation).
func connectTiers(t *testing.T, r *testRelay, prov actor) []*follower {
	t.Helper()
	bal := map[string]string{}
	var fs []*follower
	for tier, n := range fixedTiers {
		a := newActor()
		fundFollower(t, r, bal, a, n, 1, map[string]uint32{prov.pub: 10000})
		f := &follower{name: fmt.Sprintf("T%d", tier), tier: tier, cl: dial(t, r.URL), a: a}
		f.cl.login(r.URL, a)
		fs = append(fs, f)
	}
	fs = append(fs, &follower{name: "T3 anon", tier: 3, cl: dial(t, r.URL)})
	zero := newActor()
	z := &follower{name: "T3 zero-SA", tier: 3, cl: dial(t, r.URL), a: zero}
	z.cl.login(r.URL, zero)
	fs = append(fs, z)

	rich := newActor()
	bal[rich.addr] = tokens(5_000_000)
	r.SetBalances(bal)
	mallory := newActor()
	forged := evmsigtest.SignDelegation(mallory.evm, rich.pub, time.Now().Add(time.Hour).Unix())
	forged[1] = rich.addr // claims the rich address, signed by an unrelated key
	fg := &follower{name: "T3 forged", tier: 3, cl: dial(t, r.URL), a: rich}
	if ok, why := fg.cl.auth(r.URL, rich.sk, forged); !ok {
		t.Logf("forged AUTH refused by the relay: %s", why)
	}
	return append(fs, fg)
}

type sent struct {
	at time.Time
	tf int64
	id string
}

// TestTierDelays: SEP-3.21. 120 signals of mixed timeframes; measured release delay per tier vs schedule.
func TestTierDelays(t *testing.T) {
	r := startRelay(t, "")
	// timeframes are bound at registration: one provider per timeframe
	tfs := []int64{60, 120, 180, 300}
	provs := map[int64]actor{}
	for _, tf := range tfs {
		provs[tf] = registerProvider(t, r, tf, defaultRules)
	}
	// followers allocate to every provider (2500 bps each = 100%): their power with each is 25% of balance.
	// Use balances large enough that 25% still lands in the intended tier.
	bal := map[string]string{}
	split := map[string]uint32{}
	for _, p := range provs {
		split[p.pub] = 2500
	}
	var fs []*follower
	for tier, n := range []int{8_000_000, 800_000, 80_000} {
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
	pub := dial(t, r.URL)

	const n = 120
	sents := make([]sent, 0, n)
	for i := 0; i < n; i++ {
		tf := tfs[i%len(tfs)]
		ev := provs[tf].signal(tf, i)
		at := time.Now()
		pub.mustPublish(ev)
		sents = append(sents, sent{at, tf, ev.ID})
		time.Sleep(12 * time.Millisecond)
	}
	deadline := time.Now().Add(14 * time.Second) // slowest: 2 x 300 s x 1/60 = 10 s
	for time.Now().Before(deadline) {
		done := true
		for _, f := range fs {
			if _, ok := f.cl.firstArrival(sents[n-1].id); !ok {
				done = false
			}
		}
		if done {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	fractions := []string{"0", "0.1", "0.5", "2"}
	t.Logf("TestTierDelays: scale 1/60, %d signals, timeframes %v s, table 0/0.1/0.5/2", n, tfs)
	t.Logf("%-8s %5s  %9s %9s %9s  (error = measured delay - scheduled delay, ms)", "follower", "recv", "p50", "p95", "max")
	var all []float64
	for _, f := range fs {
		var errs []float64
		got := 0
		for _, s := range sents {
			at, ok := f.cl.firstArrival(s.id)
			if !ok {
				continue
			}
			got++
			e := ms(at.Sub(s.at) - scaledDelay(fractions[f.tier], s.tf))
			errs = append(errs, e)
			if e < -2 {
				t.Errorf("%s got signal %s %.1f ms EARLY", f.name, s.id[:8], -e)
			}
		}
		if got != n {
			t.Errorf("%s received %d/%d signals", f.name, got, n)
		}
		all = append(all, errs...)
		st := summarize(errs)
		t.Logf("%-8s %5d  %9.2f %9.2f %9.2f", f.name, got, st.p50, st.p95, st.max)
		if st.p95 > 50 || st.max > 250 {
			t.Errorf("%s release error too large: p95 %.1f max %.1f ms", f.name, st.p95, st.max)
		}
	}
	viol := 0
	for _, s := range sents {
		var prev time.Time
		for _, f := range fs {
			at, _ := f.cl.firstArrival(s.id)
			if at.Before(prev) {
				viol++
			}
			prev = at
		}
	}
	st := summarize(all)
	t.Logf("release error over all %d deliveries: p50 %.2f ms, p95 %.2f ms, max %.2f ms; ordering violations T0<=T1<=T2<=T3: %d", len(all), st.p50, st.p95, st.max, viol)
	if viol != 0 {
		t.Errorf("%d ordering violations (SEP-3.21)", viol)
	}
}

// TestAllocationDeterminesTier: SEP-3.22. 1M SA split 5% / 95% between providers A and B (same default table):
// tiers follow the allocated power, not the 1M balance (which alone would be T0 with both).
func TestAllocationDeterminesTier(t *testing.T) {
	r := startRelay(t, "")
	a := registerProvider(t, r, 120, defaultRules)
	b := registerProvider(t, r, 120, defaultRules)
	bal := map[string]string{}
	f := newActor()
	fundFollower(t, r, bal, f, 1_000_000, 1, map[string]uint32{a.pub: 500, b.pub: 9500})
	cl := dial(t, r.URL)
	cl.login(r.URL, f)
	cl.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	pub := dial(t, r.URL)

	evA, evB := a.signal(120, 1), b.signal(120, 1)
	t0 := time.Now()
	pub.mustPublish(evA)
	pub.mustPublish(evB)
	if !cl.waitFor(evA.ID, 6*time.Second) || !cl.waitFor(evB.ID, 6*time.Second) {
		t.Fatal("follower did not receive both signals")
	}
	atA, _ := cl.firstArrival(evA.ID)
	atB, _ := cl.firstArrival(evB.ID)
	// 50,000 SA with A -> tier 10k (0.5x of 120 s = 1 s); 950,000 SA with B -> tier 500k (0x): T2 and T0 as in SEP-3.22
	wantA, wantB := scaledDelay("0.5", 120), scaledDelay("0", 120)
	t.Logf("with A (5%% = 50k SA): %.0f ms (want %.0f); with B (95%% = 950k SA): %.0f ms (want %.0f); a 1M balance alone would be 0 ms",
		ms(atA.Sub(t0)), ms(wantA), ms(atB.Sub(t0)), ms(wantB))
	for name, c := range map[string][3]time.Duration{"A": {atA.Sub(t0), wantA}, "B": {atB.Sub(t0), wantB}} {
		if c[0] < c[1]-2*time.Millisecond || c[0] > c[1]+120*time.Millisecond {
			t.Errorf("provider %s: delivered after %s, want about %s", name, c[0], c[1])
		}
	}
}

// TestTwoProvidersOwnRules: SEP-3.25. Two providers with different rule sets and timeframes; each
// follower's delay with each provider follows that provider's rules.
func TestTwoProvidersOwnRules(t *testing.T) {
	r := startRelay(t, "")
	x := registerProvider(t, r, 120, []tierSpec{{"500", "0"}, {"0", "1"}})   // top at 500 SA: 0; else 1x
	y := registerProvider(t, r, 180, []tierSpec{{"500", "0.5"}, {"0", "3"}}) // top at 500 SA: 0.5x; else 3x
	bal := map[string]string{}
	f1, f2 := newActor(), newActor()
	fundFollower(t, r, bal, f1, 1000, 1, map[string]uint32{x.pub: 5000, y.pub: 5000}) // 500 / 500: top of both
	fundFollower(t, r, bal, f2, 1000, 1, map[string]uint32{x.pub: 4000, y.pub: 6000}) // 400 / 600: last of X, top of Y
	c1, c2 := dial(t, r.URL), dial(t, r.URL)
	c1.login(r.URL, f1)
	c2.login(r.URL, f2)
	for _, c := range []*client{c1, c2} {
		c.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	}
	pub := dial(t, r.URL)
	evX, evY := x.signal(120, 1), y.signal(180, 1)
	t0 := time.Now()
	pub.mustPublish(evX)
	pub.mustPublish(evY)
	want := []struct {
		name string
		c    *client
		id   string
		d    time.Duration
	}{
		{"f1 with X", c1, evX.ID, 0},
		{"f1 with Y", c1, evY.ID, scaledDelay("0.5", 180)},
		{"f2 with X", c2, evX.ID, scaledDelay("1", 120)},
		{"f2 with Y", c2, evY.ID, scaledDelay("0.5", 180)},
	}
	for _, w := range want {
		if !w.c.waitFor(w.id, 8*time.Second) {
			t.Fatalf("%s: never delivered", w.name)
		}
		at, _ := w.c.firstArrival(w.id)
		got := at.Sub(t0)
		t.Logf("%-10s delivered after %7.1f ms, rules say %7.1f ms", w.name, ms(got), ms(w.d))
		if got < w.d-2*time.Millisecond || got > w.d+120*time.Millisecond {
			t.Errorf("%s: %s, want about %s", w.name, got, w.d)
		}
	}
}

// TestNoEarlyLeaks: SEP-3.38. Every read route a lower tier has, polled every 20 ms until its release time.
func TestNoEarlyLeaks(t *testing.T) {
	r := startRelay(t, "", noThrottle())
	prov := registerProvider(t, r, 300, defaultRules)
	fs := connectTiers(t, r, prov)
	for _, f := range fs {
		f.cl.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	}
	pub := dial(t, r.URL)

	ev := prov.signal(300, 1)
	sendAt := time.Now()
	pub.mustPublish(ev)
	since := int64(ev.CreatedAt) - 5
	probes := []struct {
		name string
		f    map[string]any
	}{
		{"REQ kinds", map[string]any{"kinds": []int{protocol.KindSignal}}},
		{"REQ ids", map[string]any{"ids": []string{ev.ID}}},
		{"REQ since", map[string]any{"kinds": []int{protocol.KindSignal}, "since": since}},
		{"REQ authors+limit", map[string]any{"authors": []string{prov.pub}, "limit": 1}},
		{"REQ ids+authors", map[string]any{"ids": []string{ev.ID}, "authors": []string{prov.pub}}},
		{"REQ no kinds", map[string]any{"since": since}},
	}
	type tally struct{ probes, leaks int }
	results := map[string]*tally{}
	var mu sync.Mutex
	var wg sync.WaitGroup
	fractions := []string{"0", "0.1", "0.5", "2"}
	total := 0
	for _, f := range fs[1:] {
		wg.Add(1)
		go func() {
			defer wg.Done()
			release := sendAt.Add(scaledDelay(fractions[f.tier], 300))
			local := map[string]*tally{}
			firstSeen := map[string]time.Time{}
			for time.Now().Before(release.Add(400 * time.Millisecond)) {
				for _, p := range probes {
					ids, closed := f.cl.query(p.f)
					back := time.Now()
					tl := local[p.name]
					if tl == nil {
						tl = &tally{}
						local[p.name] = tl
					}
					tl.probes++
					if strings.HasPrefix(closed, "rate-limited") {
						t.Errorf("%s was throttled in a no-throttle test: %s", f.name, closed)
					}
					if closed != "" {
						t.Errorf("%s %s: CLOSED %q (lower tiers must be served, only later)", f.name, p.name, closed)
					}
					if slices.Contains(ids, ev.ID) { // other kinds (the rule set) are public and expected
						if back.Before(release) {
							tl.leaks++
							t.Errorf("LEAK %s %s returned the signal %.1f ms before release", f.name, p.name, ms(release.Sub(back)))
						}
						if _, ok := firstSeen[p.name]; !ok {
							firstSeen[p.name] = back
						}
					}
				}
				for _, cf := range []map[string]any{{"kinds": []int{protocol.KindSignal}}, {"ids": []string{ev.ID}}} {
					c, why := f.cl.count(cf)
					back := time.Now()
					tl := local["COUNT"]
					if tl == nil {
						tl = &tally{}
						local["COUNT"] = tl
					}
					tl.probes++
					if c > 0 && back.Before(release) {
						tl.leaks++
						t.Errorf("LEAK %s COUNT=%d %.1f ms before release (%s)", f.name, c, ms(release.Sub(back)), why)
					}
					if c > 0 {
						if _, ok := firstSeen["COUNT"]; !ok {
							firstSeen["COUNT"] = back
						}
					}
				}
				time.Sleep(20 * time.Millisecond)
			}
			mu.Lock()
			defer mu.Unlock()
			for name, tl := range local {
				results[f.name+" / "+name] = tl
				total += tl.probes
			}
			if at, ok := f.cl.firstArrival(ev.ID); !ok {
				t.Errorf("%s live subscription never got the signal", f.name)
			} else if at.Before(release) {
				t.Errorf("LEAK %s live subscription %.1f ms early", f.name, ms(release.Sub(at)))
			}
		}()
	}
	wg.Wait()

	// negentropy (NIP-77) must not be an alternative read path
	nc := dial(t, r.URL)
	nc.send("NEG-OPEN", "neg1", map[string]any{"kinds": []int{protocol.KindSignal}}, "61")
	time.Sleep(300 * time.Millisecond)
	select {
	case m := <-nc.ctl:
		var ty string
		_ = jsonString(m[0], &ty)
		if ty == "NEG-MSG" {
			t.Errorf("NEG-OPEN answered with NEG-MSG: negentropy is a read path")
		}
	default:
	}
	// control: T0 sees it immediately by every route
	if ids, _ := fs[0].cl.query(map[string]any{"ids": []string{ev.ID}}); !slices.Contains(ids, ev.ID) {
		t.Errorf("T0 control: ids lookup returned %d", len(ids))
	}
	leaks := 0
	for _, tl := range results {
		leaks += tl.leaks
	}
	t.Logf("TestNoEarlyLeaks: %d probes (REQ x%d routes + COUNT x2, every 20 ms) by %d lower-tier clients until release+0.4 s; leaks: %d", total, len(probes), len(fs)-1, leaks)
}

// signalsInWindow: a `limit` REQ counts visible events only; hidden newer signals do not eat the limit.
func TestLimitCountsVisibleEvents(t *testing.T) {
	r := startRelay(t, "")
	prov := registerProvider(t, r, 60, []tierSpec{{"0", "1"}}) // everyone: 1 s
	pub := dial(t, r.URL)
	old := prov.signal(60, 1)
	pub.mustPublish(old)
	time.Sleep(1200 * time.Millisecond)
	fresh := prov.signal(60, 2)
	time.Sleep(1100 * time.Millisecond) // old is 2.3 s old (visible), fresh 1.1 s (visible)... publish a hidden one
	pub.mustPublish(fresh)
	hidden := prov.signal(60, 3)
	pub.mustPublish(hidden)
	reader := dial(t, r.URL)
	ids, _ := reader.query(map[string]any{"kinds": []int{protocol.KindSignal}, "limit": 1})
	if len(ids) != 1 || ids[0] != old.ID {
		t.Fatalf("limit:1 returned %v, want the newest VISIBLE signal %s", ids, old.ID)
	}
}
