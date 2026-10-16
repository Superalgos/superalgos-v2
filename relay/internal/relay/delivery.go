package relay

import (
	"context"
	"math/big"
	"sort"
	"sync"
	"time"

	"github.com/fiatjaf/khatru"
	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/protocol"
	"github.com/superalgos/sarelay/internal/state"
)

// gated reports whether a kind is released on a schedule (signals and commitments).
func gated(kind int) bool { return kind == protocol.KindSignal || kind == protocol.KindCommitment }

// fire marks a broadcast copy as "a scheduled release at this delay" (see preventBroadcast).
type fire struct {
	meta  *state.Meta
	delay time.Duration
}

func (e *Engine) metaFor(id string) *state.Meta {
	if v, ok := e.pending.Load(id); ok {
		return v.(*state.Meta)
	}
	m, err := e.store.Meta(id)
	if err != nil {
		e.log.Error("cannot read signal metadata; hiding the event", "id", short(id), "error", err.Error())
		return nil
	}
	return m
}

// delayFor is how long after receipt this connection may see the event, and whether it may see it
// at all. It does no I/O: it uses the power cached on the connection.
func (e *Engine) delayFor(c *conn, m *state.Meta, now time.Time) (time.Duration, bool) {
	id := e.ident(c, now)
	if id.addr != "" && id.addr == m.ProviderAddr {
		return 0, true // the provider's own identity always reads its own events
	}
	if m.Private {
		min, _ := new(big.Int).SetString(m.PrivateMin, 10)
		entitled := id.addr != "" && e.dir.Power(id.total, id.addr, m.Provider, now).Cmp(min) >= 0
		if m.Kind == protocol.KindCommitment {
			if entitled {
				return 0, true
			}
			return time.Duration(m.CommitDelay), true
		}
		if entitled && id.pubkey == m.Recipient {
			return 0, true
		}
		return 0, false
	}
	if len(m.Tiers) == 0 {
		return 0, false // corrupt metadata: hide rather than guess
	}
	power := e.dir.Power(id.total, id.addr, m.Provider, now)
	for _, t := range m.Tiers {
		min, _ := new(big.Int).SetString(t.MinWei, 10)
		if power.Cmp(min) >= 0 {
			return time.Duration(t.Delay), true
		}
	}
	return time.Duration(m.Tiers[len(m.Tiers)-1].Delay), true
}

// visible is the single read rule: every route (live fan-out, stored REQ, COUNT) asks it.
func (e *Engine) visible(c *conn, id string, now time.Time) bool {
	m := e.metaFor(id)
	if m == nil {
		return false // an event without metadata was never scheduled; hide it
	}
	return e.visibleMeta(c, m, now)
}

func (e *Engine) visibleMeta(c *conn, m *state.Meta, now time.Time) bool {
	d, ok := e.delayFor(c, m, now)
	return ok && !now.Before(m.ReceivedAt().Add(d))
}

// visibleEvent extends the single read rule to kind-1040 attestations: one that names a gated event is
// as visible as that event is to this connection, so its mere existence cannot tell a slow tier that a
// signal has been received before its own release.
func (e *Engine) visibleEvent(c *conn, ev *nostr.Event, now time.Time) bool {
	if gated(ev.Kind) {
		return e.visible(c, ev.ID, now)
	}
	if id, ok := anchorTarget(ev); ok {
		if m := e.metaFor(id); m != nil {
			return e.visibleMeta(c, m, now)
		}
	}
	return true
}

func (e *Engine) delaysOf(m *state.Meta) []time.Duration {
	set := map[time.Duration]bool{0: true}
	for _, t := range m.Tiers {
		set[time.Duration(t.Delay)] = true
	}
	if m.Private && m.Kind == protocol.KindCommitment {
		set[time.Duration(m.CommitDelay)] = true
	}
	out := make([]time.Duration, 0, len(set))
	for d := range set {
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func (e *Engine) after(d time.Duration, f func()) {
	if d < 0 {
		d = 0
	}
	e.timersMu.Lock()
	defer e.timersMu.Unlock()
	if e.closed.Load() {
		return
	}
	e.timerSeq++
	id := e.timerSeq
	e.timers[id] = time.AfterFunc(d, func() {
		e.timersMu.Lock() // waits until the timer is registered below
		delete(e.timers, id)
		e.timersMu.Unlock()
		f()
	})
}

// arm schedules one broadcast per distinct delay of the event's snapshot.
func (e *Engine) arm(ev *nostr.Event, m *state.Meta) {
	for _, d := range e.delaysOf(m) {
		e.armFire(ev, m, d)
	}
	// forget the pending record once the last release is certainly done
	e.after(time.Until(m.ReceivedAt().Add(time.Duration(m.MaxDelay)+2*time.Second)), func() {
		if e.closed.Load() {
			return
		}
		e.untrackPending(m.ID)
		if err := e.store.ClearPending(m); err != nil {
			e.log.Warn("cannot clear pending record", "id", short(m.ID), "error", err.Error())
		}
	})
}

// A lane is the ordered queue of releases of one provider at one delay. Every subscriber in a tier
// reads exactly one lane per provider, so draining a lane with a single goroutine at a time delivers
// signals whose release falls at the same moment (or whose timers fire within microseconds of each
// other) in relay receipt order. Without it each release is its own timer goroutine and two signals
// published in the same millisecond could swap.
type laneKey struct {
	provider string
	delay    time.Duration
}

type laneItem struct {
	seq uint64 // relay receipt order (assigned under the write lock)
	at  time.Time
	run func()
}

type lane struct {
	mu       sync.Mutex
	items    []laneItem // ascending seq
	draining bool
}

func (e *Engine) laneFor(k laneKey) *lane {
	e.lanesMu.Lock()
	defer e.lanesMu.Unlock()
	l, ok := e.lanes[k]
	if !ok {
		l = &lane{}
		e.lanes[k] = l
	}
	return l
}

func (l *lane) push(it laneItem) {
	l.mu.Lock()
	defer l.mu.Unlock()
	i := len(l.items)
	for i > 0 && l.items[i-1].seq > it.seq {
		i--
	}
	l.items = append(l.items, laneItem{})
	copy(l.items[i+1:], l.items[i:])
	l.items[i] = it
}

// drain fires, in receipt order, every item of the lane whose release time has come, then arms a
// timer for the next one. Only one drain runs per lane at a time; a concurrent call returns at once
// and the running drain picks up whatever arrived.
func (e *Engine) drain(l *lane) {
	l.mu.Lock()
	if l.draining {
		l.mu.Unlock()
		return
	}
	l.draining = true
	for len(l.items) > 0 && !time.Now().Before(l.items[0].at) { // never fire early if the clock stepped
		it := l.items[0]
		l.items = l.items[1:]
		l.mu.Unlock()
		it.run()
		l.mu.Lock()
	}
	l.draining = false
	var next time.Time
	more := len(l.items) > 0
	if more {
		next = l.items[0].at
	}
	l.mu.Unlock()
	if more {
		e.after(time.Until(next), func() { e.drain(l) })
	}
}

func (e *Engine) armFire(ev *nostr.Event, m *state.Meta, d time.Duration) {
	cp := *ev // pointer identity marks "this is a scheduled fire"; see preventBroadcast
	release := m.ReceivedAt().Add(d)
	run := func() {
		if e.closed.Load() {
			return
		}
		e.m.releaseLag.Observe(time.Since(release).Seconds())
		e.fires.Store(&cp, &fire{meta: m, delay: d})
		n := e.rl.BroadcastEvent(&cp)
		e.fires.Delete(&cp)
		e.m.deliveries.Add(int64(n))
		e.m.armed.Add(-1)
	}
	e.m.armed.Add(1)
	l := e.laneFor(laneKey{m.Provider, d})
	l.push(laneItem{seq: m.Seq, at: release, run: run})
	e.after(time.Until(release), func() { e.drain(l) })
}

// preventBroadcast blocks khatru's own fan-out of gated events (called right after publish) and
// lets only scheduled fires through, and only to connections whose delay matches the fire.
func (e *Engine) preventBroadcast(ws *khatru.WebSocket, ev *nostr.Event) bool {
	if !gated(ev.Kind) && ev.Kind != protocol.KindAnchor {
		return false
	}
	v, fired := e.fires.Load(ev)
	if !fired {
		if gated(ev.Kind) {
			return true
		}
		// An attestation published normally goes at once only to connections that may already see the
		// event it attests; the others get a scheduled fire at that event's release (armAnchor).
		id, ok := anchorTarget(ev)
		if !ok {
			return false
		}
		m := e.metaFor(id)
		return m != nil && !e.visibleMeta(e.connOf(ws), m, time.Now())
	}
	f := v.(*fire)
	now := time.Now()
	d, ok := e.delayFor(e.connOf(ws), f.meta, now)
	if !ok || d != f.delay {
		return true
	}
	return now.Before(f.meta.ReceivedAt().Add(d)) // belt and braces: re-check the clock
}

// queryEvents serves stored events through the visibility rule. Non-gated kinds pass through.
func (e *Engine) queryEvents(ctx context.Context, f nostr.Filter) (chan *nostr.Event, error) {
	if !mayMatchGated(f) {
		return e.store.QueryEvents(ctx, f)
	}
	c := e.syncCtx(ctx)
	return e.visibleStream(ctx, f, c, e.maxStored), nil
}

func mayMatchGated(f nostr.Filter) bool {
	if len(f.Kinds) == 0 {
		return true
	}
	for _, k := range f.Kinds {
		if gated(k) || k == protocol.KindAnchor {
			return true
		}
	}
	return false
}

// scanCostEvents is how many examined events cost a connection one extra request token.
const scanCostEvents = 250

// visibleStream pages through the store newest-first and emits only events c may see, so a
// `limit` counts visible events (hidden newer ones do not eat the limit). want <= 0 means all.
//
// Work is bounded: it examines at most limits.max_scan_events stored events per request (a request that
// hits the cap simply ends there), keeps only the ids that can repeat at a page boundary in memory, and
// charges the connection one extra request token per scanCostEvents examined, so a client cannot turn
// one cheap request into a full-store scan at line rate.
func (e *Engine) visibleStream(ctx context.Context, f nostr.Filter, c *conn, want int) chan *nostr.Event {
	if f.Limit > 0 && (want <= 0 || f.Limit < want) {
		want = f.Limit
	}
	out := make(chan *nostr.Event)
	go func() {
		defer close(out)
		maxScan := e.cfg.Limits.MaxScanEvents
		page := min(1000, maxScan)
		scanned := 0
		defer func() { e.chargeScan(c, scanned) }()
		var edge map[string]bool // ids seen at the oldest timestamp of the previous page
		inner := f
		sent := 0
		for {
			inner.Limit = min(page, maxScan-scanned)
			ch, err := e.store.QueryEvents(ctx, inner)
			if err != nil {
				e.log.Error("store query failed", "error", err.Error())
				return
			}
			n, fresh := 0, 0
			var oldest nostr.Timestamp
			next := map[string]bool{}
			for ev := range ch {
				n++
				scanned++
				if n == 1 || ev.CreatedAt < oldest {
					oldest = ev.CreatedAt
					next = map[string]bool{}
				}
				if ev.CreatedAt == oldest {
					next[ev.ID] = true
				}
				if edge[ev.ID] {
					continue // the page boundary repeated it
				}
				fresh++
				if want > 0 && sent >= want {
					continue // drain
				}
				if !e.visibleEvent(c, ev, time.Now()) {
					continue
				}
				select {
				case out <- ev:
					sent++
				case <-ctx.Done():
					for range ch {
					}
					return
				}
			}
			if scanned >= maxScan && n >= inner.Limit {
				e.m.capped("scan").Add(1)
				return
			}
			if n < inner.Limit || fresh == 0 || (want > 0 && sent >= want) {
				return
			}
			edge = next
			u := oldest
			inner.Until = &u
		}
	}()
	return out
}

// chargeScan bills a connection for the stored events a request examined, beyond the first block.
func (e *Engine) chargeScan(c *conn, scanned int) {
	extra := float64(scanned / scanCostEvents)
	if extra <= 0 {
		return
	}
	now := time.Now()
	perSec, burst := e.cfg.Rate.PoweredPerSec, e.cfg.Rate.PoweredBurst
	if e.ident(c, now).total.Sign() == 0 {
		perSec, burst = e.cfg.Rate.ZeroPowerPerSec, e.cfg.Rate.ZeroPowerBurst
	}
	c.read.spend(now, perSec, burst, extra)
}

// countEvents counts only what the same connection could read right now.
func (e *Engine) countEvents(ctx context.Context, f nostr.Filter) (int64, error) {
	if !mayMatchGated(f) {
		return e.store.CountEvents(ctx, f)
	}
	var n int64
	for range e.visibleStream(ctx, f, e.syncCtx(ctx), 0) {
		n++
	}
	return n, nil
}
