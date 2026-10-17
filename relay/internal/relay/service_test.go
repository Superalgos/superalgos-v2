package relay_test

import (
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/superalgos/sarelay/internal/protocol"
)

// SEP-3.43: a client with zero token power receives every signal it subscribes to, at the last tier.
func TestZeroPowerClientsReceiveEverything(t *testing.T) {
	r := startRelay(t, "")
	prov := registerProvider(t, r, 60, defaultRules)
	anon := dial(t, r.URL)
	zero := newActor()
	authed := dial(t, r.URL)
	authed.login(r.URL, zero) // authenticated, no SA balance
	for _, c := range []*client{anon, authed} {
		c.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	}
	pub := dial(t, r.URL)
	const n = 10
	var ids []string
	var sentAt []time.Time
	for i := 0; i < n; i++ {
		ev := prov.signal(60, i)
		sentAt = append(sentAt, time.Now())
		pub.mustPublish(ev)
		ids = append(ids, ev.ID)
		time.Sleep(30 * time.Millisecond)
	}
	for name, c := range map[string]*client{"anonymous": anon, "authenticated, zero SA": authed} {
		for i, id := range ids {
			if !c.waitFor(id, harnessWait) {
				t.Fatalf("%s client missed signal %d/%d", name, i+1, n)
			}
			at, _ := c.firstArrival(id)
			if d := at.Sub(sentAt[i]); d < scaledDelay("2", 60)-2*time.Millisecond {
				t.Errorf("%s client got signal %d after %s, before the last tier's %s", name, i, d, scaledDelay("2", 60))
			}
		}
	}
	t.Logf("zero-power clients (anonymous and authenticated) received %d/%d signals each, at the last tier", n, n)
}

// SEP-3.44: over the documented rate, a zero-power client is answered with rate-limited replies and
// stays connected; it is served again once it slows down. Powered clients get a higher rate.
func TestZeroPowerOverRateIsThrottledNotDisconnected(t *testing.T) {
	r := startRelay(t, "") // default limits: zero-power 5 req/s, burst 20; powered 50 req/s, burst 200
	prov := registerProvider(t, r, 60, defaultRules)
	bal := map[string]string{}
	rich := newActor()
	fundFollower(t, r, bal, rich, 2_000_000, 1, map[string]uint32{prov.pub: 10000})

	flood := func(c *client, n int) (served, throttled int) {
		for i := 0; i < n; i++ {
			c.send("REQ", fmt.Sprintf("f%d", i), map[string]any{"kinds": []int{protocol.KindSignal}, "limit": 1})
		}
		for got := 0; got < n; {
			select {
			case m := <-c.ctl:
				var ty, sub, reason string
				_ = jsonString(m[0], &ty)
				if len(m) > 1 {
					_ = jsonString(m[1], &sub)
				}
				if !strings.HasPrefix(sub, "f") {
					continue
				}
				switch ty {
				case "EOSE":
					served++
					got++
				case "CLOSED":
					if len(m) > 2 {
						_ = jsonString(m[2], &reason)
					}
					if !strings.HasPrefix(reason, "rate-limited:") {
						t.Fatalf("CLOSED with unexpected reason %q", reason)
					}
					throttled++
					got++
				}
			case <-time.After(harnessWait):
				t.Fatalf("no reply to flooded requests after %d/%d", got, n)
			}
		}
		return
	}
	zero := dial(t, r.URL)
	s, th := flood(zero, 100)
	t.Logf("zero-power flood of 100 REQ: %d served, %d throttled with rate-limited CLOSED", s, th)
	if th < 60 || s < 15 {
		t.Errorf("expected about 20 served and the rest throttled, got served=%d throttled=%d", s, th)
	}
	time.Sleep(1200 * time.Millisecond)
	if ids, closed := zero.query(map[string]any{"kinds": []int{protocol.KindSignal}}); closed != "" {
		t.Errorf("after slowing down the client is still refused: %q (ids=%v)", closed, ids)
	}
	// COUNT is throttled the same way
	var noticed bool
	for i := 0; i < 60 && !noticed; i++ {
		_, why := zero.count(map[string]any{"kinds": []int{protocol.KindSignal}})
		noticed = strings.HasPrefix(why, "rate-limited:")
	}
	if !noticed {
		t.Error("COUNT flood was never throttled")
	}

	powered := dial(t, r.URL)
	powered.login(r.URL, rich)
	s, th = flood(powered, 100)
	t.Logf("powered flood of 100 REQ: %d served, %d throttled", s, th)
	if th != 0 {
		t.Errorf("powered client throttled %d/100 within its burst of 200", th)
	}
	// both connections are still open
	if _, closed := powered.query(map[string]any{"kinds": []int{protocol.KindSignal}}); closed != "" && !strings.HasPrefix(closed, "rate-limited") {
		t.Errorf("powered connection broken: %q", closed)
	}
}

// SEP-3.41: pending releases survive a restart and fire at their original release times; releases that
// came due during the downtime are visible on the first query after restart; rules and allocations persist.
func TestPendingReleasesSurviveRestart(t *testing.T) { pendingReleasesSurviveRestart(t) }

// TestPerfPendingReleasesSurviveRestart is the machine-speed half: each restored release lands within
// 150 ms of its original schedule. Local only (SA_PERF=1, `make perf`).
func TestPerfPendingReleasesSurviveRestart(t *testing.T) {
	perfOnly(t)
	if worst := pendingReleasesSurviveRestart(t); worst > 150 {
		t.Errorf("restored release %.1f ms after its schedule, budget 150 ms", worst)
	}
}

// pendingReleasesSurviveRestart checks nothing is lost or early across the restart (releases land at their
// original time, not restart + full delay) and returns the worst lateness in ms.
func pendingReleasesSurviveRestart(t *testing.T) float64 {
	t.Helper()
	dir := t.TempDir()
	r1 := startRelay(t, dir)
	prov := registerProvider(t, r1, 300, []tierSpec{{"1000", "0"}, {"0", "1"}}) // anyone: 5 s at scale 1/60
	fast := registerProvider(t, r1, 60, []tierSpec{{"0", "1"}})                 // anyone: 1 s
	bal := map[string]string{}
	vip := newActor()
	fundFollower(t, r1, bal, vip, 1000, 1, map[string]uint32{prov.pub: 10000})

	pub := dial(t, r1.URL)
	type sig struct {
		id string
		at time.Time
	}
	var sigs []sig
	for i := 0; i < 3; i++ {
		ev := prov.signal(300, i)
		pub.mustPublish(ev)
		sigs = append(sigs, sig{ev.ID, time.Now()})
		time.Sleep(300 * time.Millisecond)
	}
	lapsing := fast.signal(60, 1)
	pub.mustPublish(lapsing)
	r1.Stop() // ~1 s in: all three are still pending; the 1 s signal lapses while we are down
	time.Sleep(1300 * time.Millisecond)

	r2 := startRelay(t, dir)
	anon := dial(t, r2.URL)
	anon.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	vipc := dial(t, r2.URL)
	vipc.login(r2.URL, vip)
	vipc.live(map[string]any{"kinds": []int{protocol.KindSignal}})

	if ids, _ := anon.query(map[string]any{"ids": []string{lapsing.ID}}); len(ids) != 1 {
		t.Errorf("signal released during downtime is not visible after restart")
	}
	for _, s := range sigs {
		if ids, _ := anon.query(map[string]any{"ids": []string{s.id}}); len(ids) != 0 {
			t.Errorf("pending signal visible to anonymous right after restart (early)")
		}
		// the allocation and rule set persisted: the vip's power with prov is still 1000 SA, so it sees them now
		if ids, _ := vipc.query(map[string]any{"ids": []string{s.id}}); len(ids) != 1 {
			t.Errorf("vip lost its allocation across the restart")
		}
	}
	var errs []float64
	for _, s := range sigs {
		if !anon.waitFor(s.id, harnessWait) {
			t.Fatalf("pending release lost after restart")
		}
		at, _ := anon.firstArrival(s.id)
		want := s.at.Add(scaledDelay("1", 300))
		e := ms(at.Sub(want))
		errs = append(errs, e)
		// sentAt is read after the OK round trip, so a few ms "early" is the test clock. A release re-armed
		// from the restart instead of the original time would be 1 s+ late.
		if e < -10 || e > 1000 {
			t.Errorf("released %.1f ms from schedule (negative = early)", e)
		}
	}
	st := summarize(errs)
	t.Logf("restart: 3/3 pending releases delivered at original times, error ms p50 %.1f max %.1f; downtime-lapsed signal visible; allocation and rules restored", st.p50, st.max)
	_ = r2
	return st.max
}

// Concurrent: many publishers and subscribers at once must not race or lose events.
func TestConcurrentPublishAndSubscribe(t *testing.T) {
	r := startRelay(t, "", noThrottle())
	prov := registerProvider(t, r, 60, []tierSpec{{"0", "0.5"}})
	var subs []*client
	for i := 0; i < 20; i++ {
		c := dial(t, r.URL)
		c.live(map[string]any{"kinds": []int{protocol.KindSignal}})
		subs = append(subs, c)
	}
	var wg sync.WaitGroup
	ids := make(chan string, 200)
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c := dial(t, r.URL)
			for i := 0; i < 25; i++ {
				ev := prov.signal(60, w*100+i)
				c.mustPublish(ev)
				ids <- ev.ID
			}
		}()
	}
	wg.Wait()
	close(ids)
	for id := range ids {
		for _, c := range subs {
			if !c.waitFor(id, harnessWait) {
				t.Fatalf("subscriber missed %s", id[:8])
			}
		}
	}
}
