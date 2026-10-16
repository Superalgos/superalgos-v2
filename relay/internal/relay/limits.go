package relay

import (
	"bufio"
	"context"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/protocol"
	"github.com/superalgos/sarelay/internal/state"
)

// ---- pending releases (H2) ----

// trackPending registers a scheduled release in the pending set and counts it against the caps.
func (e *Engine) trackPending(m *state.Meta) {
	if _, loaded := e.pending.LoadOrStore(m.ID, m); loaded {
		return
	}
	e.pendMu.Lock()
	e.pendByProv[m.Provider]++
	e.pendTotal++
	e.pendMu.Unlock()
}

// untrackPending forgets a pending release once its last fire is certainly done.
func (e *Engine) untrackPending(id string) {
	v, ok := e.pending.LoadAndDelete(id)
	if !ok {
		return
	}
	m := v.(*state.Meta)
	e.pendMu.Lock()
	if e.pendByProv[m.Provider]--; e.pendByProv[m.Provider] <= 0 {
		delete(e.pendByProv, m.Provider)
	}
	e.pendTotal--
	e.pendMu.Unlock()
}

// pendingRoom refuses a new signal when the provider, or the relay, already holds too many releases
// that have not fired yet: every one is a timer, a lane entry and a metadata record in memory.
func (e *Engine) pendingRoom(provider string) error {
	e.pendMu.Lock()
	defer e.pendMu.Unlock()
	if n := e.pendByProv[provider]; n >= e.cfg.Limits.MaxPendingPerProvider {
		e.m.capped("pending_provider").Add(1)
		return fmt.Errorf("blocked: provider %s already has %d unreleased events, the limit (limits.max_pending_per_provider) is %d; wait for earlier signals to release or shorten the delays", short(provider), n, e.cfg.Limits.MaxPendingPerProvider)
	}
	if e.pendTotal >= e.cfg.Limits.MaxPendingTotal {
		e.m.capped("pending_total").Add(1)
		return fmt.Errorf("blocked: the relay holds %d unreleased events, its limit (limits.max_pending_total) is %d; retry later", e.pendTotal, e.cfg.Limits.MaxPendingTotal)
	}
	return nil
}

// PendingEvents reports the number of events whose releases have not finished.
func (e *Engine) PendingEvents() int {
	e.pendMu.Lock()
	defer e.pendMu.Unlock()
	return e.pendTotal
}

// ---- storage budgets (H2) ----

type budget struct {
	max  int
	used atomic.Int64
}

func (e *Engine) initBudgets(ctx context.Context) error {
	b := e.cfg.Budget
	e.budgets = map[int]*budget{
		protocol.KindAnchor: {max: b.MaxAnchor}, protocol.KindProposal: {max: b.MaxProposal}, protocol.KindVote: {max: b.MaxVote},
		protocol.KindGovDeleg: {max: b.MaxGovDelegation}, protocol.KindRevocation: {max: b.MaxRevocation},
	}
	for k, bg := range e.budgets {
		n, err := e.store.CountEvents(ctx, nostr.Filter{Kinds: []int{k}})
		if err != nil {
			return fmt.Errorf("cannot count stored kind %d events: %w", k, err)
		}
		bg.used.Store(n)
	}
	return nil
}

// budgetRoom refuses a new event of a budgeted kind once its budget is spent.
func (e *Engine) budgetRoom(kind int) error {
	b := e.budgets[kind]
	if b == nil {
		return nil
	}
	if n := b.used.Load(); n >= int64(b.max) {
		e.m.capped("budget_" + strconv.Itoa(kind)).Add(1)
		return fmt.Errorf("blocked: the storage budget for kind %d is full (%d of %d events); retry after retention (budget.retention) frees space, or ask the operator to raise budget.max_*_events", kind, n, b.max)
	}
	return nil
}

// retained are the kinds budget.retention applies to. Governance delegations and revocations are state,
// not logs: they only have a count budget.
var retained = []int{protocol.KindAnchor, protocol.KindProposal, protocol.KindVote}

// SweepRetention deletes attestations, proposals and votes older than budget.retention (by created_at)
// as of now and returns how many it removed.
func (e *Engine) SweepRetention(now time.Time) int {
	ret := e.cfg.Budget.Retention.D()
	if ret <= 0 || e.closed.Load() {
		return 0
	}
	until := nostr.Timestamp(now.Add(-ret).Unix())
	e.writeMu.Lock()
	defer e.writeMu.Unlock()
	if e.closed.Load() {
		return 0
	}
	ctx := context.Background()
	n := 0
	for _, k := range retained {
		ch, err := e.store.QueryEvents(ctx, nostr.Filter{Kinds: []int{k}, Until: &until, Limit: 5000})
		if err != nil {
			e.log.Error("retention sweep cannot list events", "kind", k, "error", err.Error())
			continue
		}
		var old []*nostr.Event
		for ev := range ch {
			old = append(old, ev)
		}
		for _, ev := range old {
			if err := e.store.DeleteEvent(ctx, ev); err != nil {
				e.log.Error("retention sweep cannot delete an event", "id", short(ev.ID), "error", err.Error())
				continue
			}
			e.budgets[k].used.Add(-1)
			n++
		}
	}
	if n > 0 {
		e.log.Info("retention sweep", "deleted", n, "retention", ret.String())
	}
	return n
}

// ---- provider registration upkeep (H2) ----

// SweepProviders removes providers that published no signal for policy.provider_idle_expiry and
// providers whose SA address no longer holds policy.min_provider_power. A balance that cannot be read
// right now removes nobody: an RPC outage must not deregister the network.
func (e *Engine) SweepProviders(ctx context.Context, now time.Time) int {
	removed := 0
	drop := func(pk, why string, args ...any) {
		if err := e.dir.Remove(pk); err != nil {
			e.log.Error("cannot remove provider", append([]any{"provider", short(pk), "reason", why, "error", err.Error()}, args...)...)
			return
		}
		removed++
		e.m.providersRemoved(why).Add(1)
		e.log.Info("provider removed", append([]any{"provider", short(pk), "reason", why}, args...)...)
	}
	if idle := e.cfg.Policy.ProviderIdleExpiry.D(); idle > 0 {
		for _, pk := range e.dir.Idle(now.Add(-idle)) {
			drop(pk, "idle", "idle_for", idle.String())
		}
	}
	if min := e.cfg.Policy.MinProviderPower; min > 0 {
		need := protocol.TokensToWei(strconv.FormatInt(min, 10))
		for _, rs := range e.dir.Providers() {
			total, err := e.power.Total(ctx, rs.SAAddress)
			if err != nil || total == nil {
				continue
			}
			if total.Cmp(need) < 0 {
				drop(rs.Provider, "power", "address", rs.SAAddress, "holds", new(big.Int).Quo(total, protocol.TokensToWei("1")).String(), "needs", strconv.FormatInt(min, 10))
			}
		}
	}
	return removed
}

// maintenanceLoop runs the retention and provider sweeps.
func (e *Engine) maintenanceLoop(ctx context.Context) {
	retTick := time.Hour
	if r := e.cfg.Budget.Retention.D(); r > 0 && r/10 < retTick {
		retTick = max(r/10, time.Second)
	}
	provTick := e.cfg.Policy.ProviderRecheck.D()
	rt, pt := time.NewTicker(retTick), time.NewTicker(provTick)
	defer rt.Stop()
	defer pt.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-rt.C:
			e.SweepRetention(time.Now())
		case <-pt.C:
			e.SweepProviders(ctx, time.Now())
		}
	}
}

// ---- per-IP connection cap (H2) ----

// clientIP is the address a connection is counted under: the TCP peer, or, behind a trusted reverse
// proxy (limits.forwarded_header), the right-most value that proxy appended to the header.
func (e *Engine) clientIP(r *http.Request) string {
	if h := e.cfg.Limits.ForwardedHeader; h != "" {
		if v := strings.TrimSpace(r.Header.Get(h)); v != "" {
			parts := strings.Split(v, ",")
			if ip := net.ParseIP(strings.TrimSpace(parts[len(parts)-1])); ip != nil {
				return ip.String()
			}
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

type ipCounter struct {
	mu sync.Mutex
	n  map[string]int
}

func (c *ipCounter) acquire(ip string, max int) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.n == nil {
		c.n = map[string]int{}
	}
	if c.n[ip] >= max {
		return false
	}
	c.n[ip]++
	return true
}

func (c *ipCounter) release(ip string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.n[ip]--; c.n[ip] <= 0 {
		delete(c.n, ip)
	}
}

// connGuard wraps the relay handler: a websocket upgrade from an address that already holds
// limits.max_conns_per_ip connections is answered 429. The slot is freed when the hijacked
// connection closes (or at once if the upgrade fails).
func (e *Engine) connGuard(next http.Handler) http.Handler {
	max := e.cfg.Limits.MaxConnsPerIP
	if max <= 0 {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			next.ServeHTTP(w, r)
			return
		}
		ip := e.clientIP(r)
		if !e.ips.acquire(ip, max) {
			e.m.capped("conns_per_ip").Add(1)
			http.Error(w, fmt.Sprintf("too many connections: %s already holds %d websockets, the limit (limits.max_conns_per_ip) is %d; close some and retry", ip, max, max), http.StatusTooManyRequests)
			return
		}
		g := &guardWriter{ResponseWriter: w, release: sync.OnceFunc(func() { e.ips.release(ip) })}
		next.ServeHTTP(g, r)
		if !g.hijacked.Load() {
			g.release()
		}
	})
}

type guardWriter struct {
	http.ResponseWriter
	release  func()
	hijacked atomic.Bool
}

func (g *guardWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	hj, ok := g.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, fmt.Errorf("response writer cannot be hijacked")
	}
	c, rw, err := hj.Hijack()
	if err != nil {
		return nil, nil, err
	}
	g.hijacked.Store(true)
	return &releaseConn{Conn: c, release: g.release}, rw, nil
}

type releaseConn struct {
	net.Conn
	release func()
}

func (c *releaseConn) Close() error {
	err := c.Conn.Close()
	c.release()
	return err
}

// ---- kind 1040 follows its target's release (M6) ----

// armAnchor schedules, for every tier delay of the attested signal that has not passed, a fire of the
// attestation at the same moment as the signal's, so a tier receives the anchor together with the
// signal and not before it.
func (e *Engine) armAnchor(ev *nostr.Event) {
	id, ok := anchorTarget(ev)
	if !ok {
		return
	}
	v, pend := e.pending.Load(id)
	if !pend {
		return
	}
	m := v.(*state.Meta)
	now := time.Now()
	for _, d := range e.delaysOf(m) {
		if m.ReceivedAt().Add(d).After(now) {
			e.armFire(ev, m, d)
		}
	}
}
