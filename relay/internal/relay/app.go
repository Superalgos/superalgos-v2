// Package relay is the sarelay engine: khatru hooks that enforce provider rules, power
// allocations, tiered delayed delivery and private delivery.
package relay

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/fiatjaf/khatru"
	"github.com/nbd-wtf/go-nostr"
	"github.com/nbd-wtf/go-nostr/nip11"

	"github.com/superalgos/sarelay/internal/chains"
	"github.com/superalgos/sarelay/internal/config"
	"github.com/superalgos/sarelay/internal/protocol"
	"github.com/superalgos/sarelay/internal/state"
)

// Engine owns the relay's state and hooks.
type Engine struct {
	cfg   config.Config
	log   *slog.Logger
	m     *engineMetrics
	lim   protocol.Limits
	store *state.Store
	dir   *state.Directory
	power *chains.Power
	rl    *khatru.Relay

	extraKinds map[int]bool
	maxStored  int
	anon       *conn

	conns      sync.Map // *khatru.WebSocket -> *conn
	fires      sync.Map // *nostr.Event (the copy being fired) -> *fire
	pending    sync.Map // event id -> *state.Meta, for events with releases still to deliver (use trackPending)
	pendMu     sync.Mutex
	pendByProv map[string]int // provider -> events with releases still to deliver
	pendTotal  int
	timersMu   sync.Mutex
	timers     map[uint64]*time.Timer // armed, not yet fired
	timerSeq   uint64
	lanesMu    sync.Mutex
	lanes      map[laneKey]*lane
	recvSeq    atomic.Uint64 // receipt counter, assigned under writeMu
	writeMu    sync.Mutex
	closed     atomic.Bool

	budgets     map[int]*budget // storage budgets of the kinds anyone may publish
	ips         ipCounter
	quarantined int
}

// New opens storage and builds the engine. Call Start to resume pending releases.
func New(cfg config.Config, log *slog.Logger) (*Engine, error) {
	store, err := state.Open(cfg.StoragePath)
	if err != nil {
		return nil, err
	}
	lim := protocol.DefaultLimits()
	lim.MinTimeframe, lim.MaxTimeframe = cfg.Limits.MinTimeframe, cfg.Limits.MaxTimeframe
	lim.MaxDelayFraction = cfg.Limits.MaxDelayFraction
	lim.MaxReleaseDelay = cfg.Limits.MaxReleaseDelay.D()

	var cs []chains.Chain
	for _, c := range cfg.Chains {
		if c.Disabled {
			continue
		}
		switch c.Kind {
		case "evm":
			cs = append(cs, chains.Chain{ID: c.ID, Oracle: chains.EVM{RPCURL: c.RPC, Contract: c.Contract, Decimals: c.Decimals}})
		case "fixture":
			cs = append(cs, chains.Chain{ID: c.ID, Oracle: chains.Fixture{Path: c.File}})
		}
	}
	e := &Engine{
		cfg: cfg, log: log, m: newEngineMetrics(), lim: lim, store: store,
		power: &chains.Power{Chains: cs, TTL: cfg.Oracle.CacheTTL.D(), MaxStale: cfg.Oracle.MaxStale.D(),
			MaxLookupsPerSec: cfg.Oracle.MaxLookupsPerSec, LookupBurst: cfg.Oracle.LookupBurst,
			BackoffBase: cfg.Oracle.BackoffBase.D(), BackoffMax: cfg.Oracle.BackoffMax.D()},
		pendByProv: map[string]int{},
		extraKinds: map[int]bool{}, maxStored: 1000, anon: &conn{}, timers: map[uint64]*time.Timer{},
		lanes: map[laneKey]*lane{},
	}
	for _, k := range cfg.Policy.ExtraKinds {
		e.extraKinds[k] = true
	}
	e.dir = state.NewDirectory(lim, cfg.AllocationDelay.D(), cfg.Limits.MaxProviders, cfg.Limits.MinAllocInterval.D(), store)
	e.dir.Skew = cfg.Limits.Skew.D()
	if err := e.restore(); err != nil {
		store.Close()
		return nil, fmt.Errorf("cannot restore provider and allocation state: %w", err)
	}
	e.registerGauges()
	e.buildRelay()
	return e, nil
}

// restore rebuilds the directory from storage. A record that is corrupt or no longer validates is
// quarantined (logged, counted in sarelay_quarantined_records, left in the database) instead of
// stopping the relay: one bad record must not take every provider and allocation down with it.
func (e *Engine) restore() error {
	rules, badR, err := e.store.LoadRuleSets()
	if err != nil {
		return err
	}
	allocs, badA, err := e.store.LoadAllocations()
	if err != nil {
		return err
	}
	revs, badV, err := e.store.LoadRevocations()
	if err != nil {
		return err
	}
	act, err := e.store.LoadActivity()
	if err != nil {
		return err
	}
	bad := append(append(append([]state.Quarantined(nil), badR...), badA...), badV...)
	bad = append(bad, e.dir.Load(rules, allocs, revs, act)...)
	for _, q := range bad {
		e.log.Error("quarantined a stored directory record; it is ignored until restored from backup", "record", q.Key, "reason", q.Reason)
	}
	e.quarantined = len(bad)
	return nil
}

func (e *Engine) registerGauges() {
	e.m.reg.Gauge("sarelay_quarantined_records", "Stored directory records skipped at start-up because they were corrupt or no longer validate.", func() float64 { return float64(e.quarantined) })
	e.m.reg.Gauge("sarelay_oracle_throttled_total", "Chain reads skipped by the relay-wide lookup cap or a chain back-off.", func() float64 { return float64(e.power.Throttled.Load()) })
	e.m.reg.Gauge("sarelay_providers", "Registered providers (rule sets).", func() float64 { p, _ := e.dir.Counts(); return float64(p) })
	e.m.reg.Gauge("sarelay_allocations", "Accepted power allocations (history).", func() float64 { _, a := e.dir.Counts(); return float64(a) })
	e.m.reg.Gauge("sarelay_oracle_lookups_total", "Chain balance reads attempted.", func() float64 { return float64(e.power.Lookups.Load()) })
	e.m.reg.Gauge("sarelay_oracle_errors_total", "Chain balance reads that failed.", func() float64 { return float64(e.power.Errors.Load()) })
}

func (e *Engine) buildRelay() {
	rl := khatru.NewRelay()
	rl.Info.Name = "sarelay"
	rl.Info.Description = "Superalgos v2 tiered signal relay"
	rl.Info.Software = "https://github.com/superalgos/superalgos-v2"
	rl.Info.Version = "0.1.0"
	rl.Info.SupportedNIPs = []any{1, 11, 42, 45}
	rl.Info.Limitation = &nip11.RelayLimitationDocument{RestrictedWrites: true}
	rl.MaxMessageSize = e.cfg.Limits.MaxMessageBytes
	if e.cfg.PublicURL != "" {
		rl.ServiceURL = e.cfg.PublicURL
	}
	rl.Log = slog.NewLogLogger(e.log.Handler(), slog.LevelWarn)
	rl.RejectEvent = append(rl.RejectEvent, e.rejectEvent)
	rl.StoreEvent = append(rl.StoreEvent, e.storeEvent)
	rl.ReplaceEvent = append(rl.ReplaceEvent, e.replaceEvent)
	rl.OnEventSaved = append(rl.OnEventSaved, e.onSaved)
	rl.RejectFilter = append(rl.RejectFilter, e.rejectRead)
	rl.RejectCountFilter = append(rl.RejectCountFilter, e.rejectRead)
	rl.QueryEvents = append(rl.QueryEvents, e.queryEvents)
	rl.CountEvents = append(rl.CountEvents, e.countEvents)
	rl.PreventBroadcast = append(rl.PreventBroadcast, e.preventBroadcast)
	rl.OnConnect = append(rl.OnConnect, e.onConnect)
	rl.OnDisconnect = append(rl.OnDisconnect, e.onDisconnect)
	// Negentropy (NIP-77) stays off: it would be one more read path to gate.
	e.rl = rl
}

// Handler is the relay's websocket + NIP-11 handler, behind the per-IP connection cap.
func (e *Engine) Handler() http.Handler { return e.connGuard(e.rl) }

func (e *Engine) onConnect(ctx context.Context) {
	if ws := khatru.GetConnection(ctx); ws != nil {
		e.connOf(ws)
		e.m.conns.Add(1)
		khatru.RequestAuth(ctx) // offer NIP-42 right away; clients that never AUTH are zero-power
	}
}

func (e *Engine) onDisconnect(ctx context.Context) {
	if ws := khatru.GetConnection(ctx); ws != nil {
		if _, ok := e.conns.LoadAndDelete(ws); ok {
			e.m.conns.Add(-1)
		}
	}
}

// syncCtx evaluates the caller's AUTH (if new) and returns its connection state.
func (e *Engine) syncCtx(ctx context.Context) *conn {
	ws := khatru.GetConnection(ctx)
	if ws == nil {
		return e.anon
	}
	c := e.connOf(ws)
	e.sync(ctx, ws, c)
	return c
}

// rejectRead throttles reads per connection. Zero-power clients get a lower rate than powered ones,
// and are answered with rate-limited replies; they are never disconnected (SEP-3.44).
func (e *Engine) rejectRead(ctx context.Context, _ nostr.Filter) (bool, string) {
	ws := khatru.GetConnection(ctx)
	if ws == nil {
		return false, ""
	}
	c := e.connOf(ws)
	e.sync(ctx, ws, c)
	now := time.Now()
	perSec, burst := e.cfg.Rate.PoweredPerSec, e.cfg.Rate.PoweredBurst
	class := "powered"
	if e.ident(c, now).total.Sign() == 0 {
		perSec, burst, class = e.cfg.Rate.ZeroPowerPerSec, e.cfg.Rate.ZeroPowerBurst, "zero-power"
	}
	if ok, wait := c.read.allow(now, perSec, burst); !ok {
		e.m.rateLimited("read").Add(1)
		return true, fmt.Sprintf("rate-limited: %s clients may send %.0f requests/s (burst %.0f); retry in %dms", class, perSec, burst, wait.Milliseconds()+1)
	}
	return false, ""
}

// Start resumes pending releases from storage and starts the balance recheck loop.
func (e *Engine) Start(ctx context.Context) error {
	if err := e.initBudgets(ctx); err != nil {
		return err
	}
	if err := e.resume(); err != nil {
		return err
	}
	go e.recheckLoop(ctx)
	go e.maintenanceLoop(ctx)
	return nil
}

// resume re-arms every release that was pending when the relay last stopped (SEP-3.41). Releases
// that came due during the downtime need no timer: their events are simply visible now.
func (e *Engine) resume() error {
	pend, err := e.store.Pending(time.Unix(0, 0))
	if err != nil {
		return fmt.Errorf("cannot read pending releases: %w", err)
	}
	rearmed, orphans := 0, 0
	sort.SliceStable(pend, func(i, j int) bool { // receipt order: (seq, received time)
		if pend[i].Seq != pend[j].Seq {
			return pend[i].Seq < pend[j].Seq
		}
		return pend[i].Received < pend[j].Received
	})
	for _, m := range pend {
		if m.Seq > e.recvSeq.Load() {
			e.recvSeq.Store(m.Seq)
		}
		ch, err := e.store.QueryEvents(context.Background(), nostr.Filter{IDs: []string{m.ID}})
		if err != nil {
			return fmt.Errorf("cannot look up pending event %s: %w", m.ID, err)
		}
		var ev *nostr.Event
		for x := range ch {
			ev = x
		}
		if ev == nil { // metadata written, event never saved (crash in between)
			_ = e.store.DropMeta(m)
			orphans++
			continue
		}
		e.trackPending(m)
		e.arm(ev, m)
		rearmed++
	}
	e.log.Info("pending releases resumed", "events", rearmed, "orphans_dropped", orphans)
	return nil
}

// recheckLoop picks up late AUTH events and re-reads balances so power changes reach live subscribers.
func (e *Engine) recheckLoop(ctx context.Context) {
	tick := min(e.cfg.Oracle.CacheTTL.D()/4, 500*time.Millisecond)
	if tick < 20*time.Millisecond {
		tick = 20 * time.Millisecond
	}
	tk := time.NewTicker(tick)
	defer tk.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tk.C:
			var wg sync.WaitGroup
			sem := make(chan struct{}, 16) // balance reads can be slow RPC calls: bounded parallelism
			e.conns.Range(func(k, v any) bool {
				ws, c := k.(*khatru.WebSocket), v.(*conn)
				wg.Add(1)
				sem <- struct{}{}
				go func() {
					defer func() { <-sem; wg.Done() }()
					e.sync(ctx, ws, c)
					e.refresh(ctx, c)
				}()
				return true
			})
			wg.Wait()
		}
	}
}

// Close stops timers and closes storage.
func (e *Engine) Close() {
	if !e.closed.CompareAndSwap(false, true) {
		return
	}
	e.timersMu.Lock()
	for id, t := range e.timers {
		t.Stop()
		delete(e.timers, id)
	}
	e.timersMu.Unlock()
	e.writeMu.Lock()
	defer e.writeMu.Unlock()
	e.store.Close()
}

// Metrics exposes the registry for the admin endpoint.
func (e *Engine) Metrics() *engineMetrics { return e.m }

// Counts reports providers and allocations.
func (e *Engine) Counts() (providers, allocations int) { return e.dir.Counts() }

// Pending is the number of scheduled releases not yet fired.
func (e *Engine) Pending() int64 { return e.m.armed.Load() }

// StoreHealthy does a cheap read of the database.
func (e *Engine) StoreHealthy() error {
	_, err := e.store.Meta("00")
	return err
}

func short(h string) string {
	if len(h) > 12 {
		return h[:12] + ".."
	}
	return h
}
