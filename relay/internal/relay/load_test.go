package relay_test

import (
	"fmt"
	"os"
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/superalgos/sarelay/internal/protocol"
)

// TestLoad300Subscribers: 300 concurrent subscribers across tiers; release error and memory.
// (Relay and clients share this process, so the memory figures include the 300 test clients.)
func TestLoad300Subscribers(t *testing.T) { load300(t) }

// TestPerfLoad300Subscribers is the machine-speed half: p95 release error under 100 ms. Local only
// (SA_PERF=1, `make perf`); CI never runs it.
func TestPerfLoad300Subscribers(t *testing.T) {
	perfOnly(t)
	if st := load300(t); st.p95 > 100 {
		t.Errorf("p95 release error %.1f ms exceeds 100 ms under load", st.p95)
	}
}

// perfOnly skips a performance test unless SA_PERF=1 asks for it.
func perfOnly(t *testing.T) {
	t.Helper()
	if os.Getenv("SA_PERF") != "1" {
		t.Skip("performance test: run locally with `make perf` (SA_PERF=1)")
	}
}

// load300 checks correctness (nothing missing, nothing early) and returns the release-error summary.
func load300(t *testing.T) stats {
	r := startRelay(t, "", noThrottle())
	prov := registerProvider(t, r, 120, defaultRules)
	const perTier, nSig = 75, 60
	balances := []int{2_000_000, 200_000, 20_000}
	bal := map[string]string{}
	type sub struct {
		tier int
		cl   *client
	}
	var identities [3][]actor
	for tier := 0; tier < 3; tier++ {
		for i := 0; i < perTier; i++ {
			a := newActor()
			bal[a.addr] = tokens(balances[tier])
			identities[tier] = append(identities[tier], a)
		}
	}
	r.SetBalances(bal)
	// allocations first, on one connection
	setup := dial(t, r.URL)
	for tier := 0; tier < 3; tier++ {
		for _, a := range identities[tier] {
			setup.mustPublish(a.allocation(1, 0, map[string]uint32{prov.pub: 10000}))
		}
	}
	var memBefore runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&memBefore)

	var subs []sub
	var wg sync.WaitGroup
	var mu sync.Mutex
	for tier := 0; tier < 4; tier++ {
		for i := 0; i < perTier; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				cl := dial(t, r.URL)
				if tier < 3 {
					cl.login(r.URL, identities[tier][i])
				}
				cl.live(map[string]any{"kinds": []int{protocol.KindSignal}})
				mu.Lock()
				subs = append(subs, sub{tier, cl})
				mu.Unlock()
			}()
		}
	}
	wg.Wait()
	if len(subs) != 4*perTier {
		t.Fatalf("only %d subscribers connected", len(subs))
	}
	var memConn runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&memConn)

	pub := dial(t, r.URL)
	var sents []sent
	for i := 0; i < nSig; i++ {
		ev := prov.signal(120, i)
		at := time.Now()
		pub.mustPublish(ev)
		sents = append(sents, sent{at, 120, ev.ID})
		time.Sleep(40 * time.Millisecond)
	}
	// wait for the last signal to reach every subscriber (slowest tier: 2 s), bounded only to catch a hang
	for end := time.Now().Add(harnessWait); time.Now().Before(end); time.Sleep(50 * time.Millisecond) {
		all := true
		for _, s := range subs {
			if _, ok := s.cl.firstArrival(sents[nSig-1].id); !ok {
				all = false
				break
			}
		}
		if all {
			break
		}
	}
	var memAfter runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&memAfter)

	fractions := []string{"0", "0.1", "0.5", "2"}
	errs := map[int][]float64{}
	var all []float64
	missing := 0
	for _, s := range subs {
		for _, e := range sents {
			at, ok := s.cl.firstArrival(e.id)
			if !ok {
				missing++
				continue
			}
			v := ms(at.Sub(e.at) - scaledDelay(fractions[s.tier], e.tf))
			errs[s.tier] = append(errs[s.tier], v)
			all = append(all, v)
			if v < -2 {
				t.Errorf("tier %d delivery %.1f ms early", s.tier, -v)
			}
		}
	}
	t.Logf("TestLoad300Subscribers: %d subscribers (%d per tier, T3 anonymous), %d signals, scale 1/60, %d deliveries, %d missing", len(subs), perTier, nSig, len(all), missing)
	for tier := 0; tier < 4; tier++ {
		st := summarize(errs[tier])
		t.Logf("  T%d release error ms: p50 %.2f p95 %.2f max %.2f", tier, st.p50, st.p95, st.max)
	}
	st := summarize(all)
	t.Logf("  all release error ms: p50 %.2f p95 %.2f max %.2f", st.p50, st.p95, st.max)
	mb := func(b uint64) float64 { return float64(b) / (1 << 20) }
	t.Logf("  process heap (relay + 300 test clients): %.1f MB before connect, %.1f MB with 300 connected, %.1f MB after %d signals x 300 deliveries; Sys %.1f MB; race build: %v",
		mb(memBefore.HeapInuse), mb(memConn.HeapInuse), mb(memAfter.HeapInuse), nSig, mb(memAfter.Sys), raceEnabled)
	if missing != 0 {
		t.Errorf("%d deliveries missing", missing)
	}
	_ = fmt.Sprint
	return st
}
