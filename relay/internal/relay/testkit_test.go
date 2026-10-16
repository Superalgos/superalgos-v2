package relay_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/config"
	"github.com/superalgos/sarelay/internal/evmsig/evmsigtest"
	"github.com/superalgos/sarelay/internal/protocol"
	"github.com/superalgos/sarelay/internal/relay"
)

// testScale makes a 60 s timeframe release over 1 s.
const testScale = 1.0 / 60.0

// defaultRules is the protocol's suggested starting table: 0 / 0.1x / 0.5x / 2x at 500k / 100k / 10k / anyone (sa_core.power.DEFAULT_TIERS).
var defaultRules = []tierSpec{{"500000", "0"}, {"100000", "0.1"}, {"10000", "0.5"}, {"0", "2"}}

type tierSpec struct{ MinPower, Fraction string }

// ---- relay under test ----

type testRelay struct {
	t         *testing.T
	cfg       config.Config
	eng       *relay.Engine
	URL       string
	AdminURL  string
	Token     string
	bal       string // fixture path
	cancel    context.CancelFunc
	done      chan struct{}
	addr      string
	adminAddr string
}

type opt func(*config.Config)

func withDelay(d time.Duration) opt {
	return func(c *config.Config) {
		c.AllocationDelay, c.CoreDelay, c.MaxAnchorLatency = config.Duration(d), config.Duration(d), 0
	}
}

// noThrottle lifts the read rate limits so probes and load tests are answered, not throttled.
func noThrottle() opt {
	return func(c *config.Config) {
		c.Rate.ZeroPowerPerSec, c.Rate.ZeroPowerBurst = 1e6, 1e6
		c.Rate.PoweredPerSec, c.Rate.PoweredBurst = 1e6, 1e6
	}
}

func quietLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// startRelay starts an in-process relay. dir may be reused across restarts (same storage).
func startRelay(t *testing.T, dir string, opts ...opt) *testRelay {
	t.Helper()
	if dir == "" {
		dir = t.TempDir()
	}
	cfg := config.Default()
	cfg.StoragePath = filepath.Join(dir, "data")
	cfg.TimeScale = testScale
	cfg.AllocationDelay = 0
	cfg.Limits.MinAllocInterval = 0
	cfg.Oracle.CacheTTL = config.Duration(2 * time.Second)
	// Permissive, explicit caps for the functional tests; the hardening tests tighten one at a time.
	cfg.Policy.MinProviderPower, cfg.Policy.MinProposerPower = 0, 0
	cfg.Limits.MaxConnsPerIP = 0
	cfg.Oracle.MaxLookupsPerSec, cfg.Oracle.LookupBurst = 1e6, 1e6
	bal := filepath.Join(dir, "balances.json")
	if _, err := os.Stat(bal); err != nil {
		if err := os.WriteFile(bal, []byte("{}"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	cfg.Chains = []config.Chain{{ID: "fixture", Kind: "fixture", File: bal}}
	for _, o := range opts {
		o(&cfg)
	}
	eng, err := relay.New(cfg, quietLogger())
	if err != nil {
		t.Fatalf("relay.New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	if err := eng.Start(ctx); err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("cannot bind a local port: %v", err)
	}
	aln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	token, err := relay.AdminToken("", cfg.StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	r := &testRelay{t: t, cfg: cfg, eng: eng, bal: bal, cancel: cancel, done: make(chan struct{}), Token: token,
		URL: "ws://" + ln.Addr().String(), AdminURL: "http://" + aln.Addr().String()}
	go func() { defer close(r.done); _ = eng.Serve(ctx, ln, aln, token) }()
	t.Cleanup(r.Stop)
	return r
}

// Stop shuts the relay down (idempotent).
func (r *testRelay) Stop() {
	if r.cancel == nil {
		return
	}
	r.cancel()
	<-r.done
	r.eng.Close()
	r.cancel = nil
}

func (r *testRelay) SetBalances(m map[string]string) {
	b, _ := json.Marshal(m)
	tmp := r.bal + ".tmp"
	_ = os.WriteFile(tmp, b, 0o600)
	_ = os.Rename(tmp, r.bal)
}

// ---- identities ----

type actor struct {
	evm     *secp256k1.PrivateKey
	addr    string
	sk, pub string
}

func newActor() actor {
	k, _ := secp256k1.GeneratePrivateKey()
	sk := nostr.GeneratePrivateKey()
	pub, _ := nostr.GetPublicKey(sk)
	return actor{evm: k, addr: evmsigtest.AddressOf(k), sk: sk, pub: pub}
}

func (a actor) delegation(expires int64) nostr.Tag {
	return evmsigtest.SignDelegation(a.evm, a.pub, expires)
}

func (a actor) device() (sk, pub string) {
	sk = nostr.GeneratePrivateKey()
	pub, _ = nostr.GetPublicKey(sk)
	return
}

// rulesText writes the rule-set signing text the way sa_core.power.ruleset_message does: tiers ascending
// by min_power, delay as %.4f. It is deliberately independent of the production code.
func rulesText(tiers []tierSpec) string {
	type pair struct {
		min   int
		delay float64
	}
	var ps []pair
	for _, t := range tiers {
		m, _ := strconv.Atoi(t.MinPower)
		d, _ := strconv.ParseFloat(t.Fraction, 64)
		ps = append(ps, pair{m, d})
	}
	sort.Slice(ps, func(i, j int) bool { return ps[i].min < ps[j].min })
	parts := make([]string, len(ps))
	for i, p := range ps {
		parts[i] = fmt.Sprintf("%d@%.4f", p.min, p.delay)
	}
	return strings.Join(parts, ",")
}

// ruleSet builds a signed kind-37318 event for provider a (a.pub is the provider key, delegated by a.evm).
// version orders rule sets of one provider: it is added to created_at.
func (a actor) ruleSet(version int64, tf int64, tiers []tierSpec) *nostr.Event {
	ts := make([]map[string]any, len(tiers))
	for i, t := range tiers {
		m, _ := strconv.Atoi(t.MinPower)
		d, _ := strconv.ParseFloat(t.Fraction, 64)
		ts[i] = map[string]any{"min_power": m, "delay_fraction": d}
	}
	msg := fmt.Sprintf("sa-ruleset:%s:%d:%s", a.pub, tf, rulesText(tiers))
	return a.signRuleSet(version, map[string]any{"v": 1, "provider": a.pub, "sa_address": a.addr, "timeframe": tf, "tiers": ts, "sig": evmsigtest.Sign(a.evm, msg)}, true)
}

func (a actor) privateRuleSet(version int64, tf int64, min string) *nostr.Event {
	m, _ := strconv.Atoi(min)
	msg := fmt.Sprintf("sa-ruleset:%s:%d:private@%d", a.pub, tf, m)
	return a.signRuleSet(version, map[string]any{"v": 1, "provider": a.pub, "sa_address": a.addr, "timeframe": tf,
		"private": map[string]any{"min_power": m}, "sig": evmsigtest.Sign(a.evm, msg)}, true)
}

func (a actor) signRuleSet(version int64, content map[string]any, withDelegation bool) *nostr.Event {
	b, _ := json.Marshal(content)
	tags := nostr.Tags{{"d", a.pub}}
	if withDelegation {
		tags = append(tags, a.delegation(time.Now().Add(time.Hour).Unix()))
	}
	ev := &nostr.Event{PubKey: a.pub, CreatedAt: nostr.Timestamp(time.Now().Unix() + version), Kind: protocol.KindRuleSet, Tags: tags, Content: string(b)}
	_ = ev.Sign(a.sk)
	return ev
}

// allocation builds a signed kind-9332 event from this SA identity's device key.
func (a actor) allocation(nonce uint64, gov uint32, providers map[string]uint32) *nostr.Event {
	al := &protocol.Allocation{Address: a.addr, Nonce: nonce, Governance: gov, Providers: providers}
	return a.allocationSignedBy(a.evm, al)
}

func (a actor) allocationSignedBy(signer *secp256k1.PrivateKey, al *protocol.Allocation) *nostr.Event {
	ps := map[string]uint32{}
	for k, v := range al.Providers {
		ps[k] = v
	}
	content, _ := json.Marshal(map[string]any{"v": 1, "address": al.Address, "nonce": al.Nonce, "governance": al.Governance, "providers": ps, "sig": evmsigtest.Sign(signer, al.Message())})
	ev := &nostr.Event{PubKey: a.pub, CreatedAt: nostr.Now(), Kind: protocol.KindAllocation, Tags: nostr.Tags{a.delegation(time.Now().Add(time.Hour).Unix())}, Content: string(content)}
	_ = ev.Sign(a.sk)
	return ev
}

// signal builds a public kind-9317 event signed by this actor's key (provider key itself).
func (a actor) signal(tf int64, seq int) *nostr.Event {
	return signalBy(a.sk, nostr.Tags{a.delegation(time.Now().Add(time.Hour).Unix())}, tf, seq, time.Now().UnixMilli())
}

func signalBy(sk string, tags nostr.Tags, tf int64, seq int, createdMS int64) *nostr.Event {
	pub, _ := nostr.GetPublicKey(sk)
	content := fmt.Sprintf(`{"v":1,"strategy_id":"s1","seq":%d,"prev":null,"instrument":"BINANCE:BTCUSDT","side":"buy","action":"open","size_fraction":0.1,"order_type":"market","timeframe":%d,"created_ms":%d}`, seq, tf, createdMS)
	ev := &nostr.Event{PubKey: pub, CreatedAt: nostr.Now(), Kind: protocol.KindSignal, Tags: append(nostr.Tags{}, tags...), Content: content}
	_ = ev.Sign(sk)
	return ev
}

// ---- websocket client ----

type recv struct {
	sub, id string
	at      time.Time
}

type client struct {
	t    *testing.T
	c    *websocket.Conn
	ctx  context.Context
	stop context.CancelFunc

	mu      sync.Mutex
	events  []recv
	first   map[string]time.Time
	bodies  map[string]string
	raws    map[string]json.RawMessage
	ctl     chan []json.RawMessage
	authCh  chan string
	nextSub int
}

func dial(t *testing.T, url string) *client {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		cancel()
		t.Fatalf("dial %s: %v", url, err)
	}
	c.SetReadLimit(1 << 22)
	cl := &client{t: t, c: c, ctx: ctx, stop: cancel, first: map[string]time.Time{}, bodies: map[string]string{}, raws: map[string]json.RawMessage{},
		ctl: make(chan []json.RawMessage, 4096), authCh: make(chan string, 4)}
	go cl.read()
	t.Cleanup(func() { cancel(); _ = c.CloseNow() })
	return cl
}

func (cl *client) read() {
	for {
		_, data, err := cl.c.Read(cl.ctx)
		if err != nil {
			return
		}
		at := time.Now()
		var m []json.RawMessage
		if json.Unmarshal(data, &m) != nil || len(m) == 0 {
			continue
		}
		var typ string
		_ = json.Unmarshal(m[0], &typ)
		switch typ {
		case "EVENT":
			var sub string
			var ev struct {
				ID      string `json:"id"`
				Content string `json:"content"`
			}
			_ = json.Unmarshal(m[1], &sub)
			_ = json.Unmarshal(m[2], &ev)
			cl.mu.Lock()
			cl.events = append(cl.events, recv{sub, ev.ID, at})
			if _, ok := cl.first[ev.ID]; !ok {
				cl.first[ev.ID] = at
				cl.bodies[ev.ID] = ev.Content
				cl.raws[ev.ID] = m[2]
			}
			cl.mu.Unlock()
		case "AUTH":
			var ch string
			_ = json.Unmarshal(m[1], &ch)
			select {
			case cl.authCh <- ch:
			default:
			}
		default:
			cl.ctl <- m
		}
	}
}

func (cl *client) send(v ...any) {
	b, _ := json.Marshal(v)
	if err := cl.c.Write(cl.ctx, websocket.MessageText, b); err != nil {
		cl.t.Errorf("write: %v", err)
	}
}

func (cl *client) waitCtl(typ, key string, d time.Duration) []json.RawMessage {
	cl.t.Helper()
	deadline := time.After(d)
	for {
		select {
		case m := <-cl.ctl:
			var ty, k string
			_ = json.Unmarshal(m[0], &ty)
			if len(m) > 1 {
				_ = json.Unmarshal(m[1], &k)
			}
			if ty == typ && k == key {
				return m
			}
		case <-deadline:
			cl.t.Fatalf("timeout waiting for %s %s", typ, key)
			return nil
		}
	}
}

// auth answers the relay's NIP-42 challenge with a kind-22242 event; tags are extra (the delegation).
func (cl *client) auth(url, sk string, extra ...nostr.Tag) (bool, string) {
	cl.t.Helper()
	var ch string
	select {
	case ch = <-cl.authCh:
	case <-time.After(5 * time.Second):
		cl.t.Fatal("no AUTH challenge from relay")
	}
	pub, _ := nostr.GetPublicKey(sk)
	ev := &nostr.Event{PubKey: pub, CreatedAt: nostr.Now(), Kind: 22242, Tags: append(nostr.Tags{{"relay", url}, {"challenge", ch}}, extra...)}
	_ = ev.Sign(sk)
	cl.send("AUTH", ev)
	m := cl.waitCtl("OK", ev.ID, 5*time.Second)
	var good bool
	var reason string
	_ = json.Unmarshal(m[2], &good)
	if len(m) > 3 {
		_ = json.Unmarshal(m[3], &reason)
	}
	return good, reason
}

// login connects an SA identity: device key a.sk with a one-hour delegation from a.addr.
func (cl *client) login(url string, a actor) {
	cl.t.Helper()
	if ok, why := cl.auth(url, a.sk, a.delegation(time.Now().Add(time.Hour).Unix())); !ok {
		cl.t.Fatalf("AUTH refused: %s", why)
	}
}

func (cl *client) subID() string {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	cl.nextSub++
	return fmt.Sprintf("s%d", cl.nextSub)
}

func (cl *client) live(f map[string]any) string {
	sub := cl.subID()
	cl.send("REQ", sub, f)
	cl.waitCtl("EOSE", sub, 5*time.Second)
	return sub
}

// query is a one-shot history REQ; closed carries a CLOSED reason.
func (cl *client) query(f map[string]any) (ids []string, closed string) {
	sub := cl.subID()
	cl.send("REQ", sub, f)
	deadline := time.After(5 * time.Second)
	for {
		select {
		case m := <-cl.ctl:
			var ty, k string
			_ = json.Unmarshal(m[0], &ty)
			if len(m) > 1 {
				_ = json.Unmarshal(m[1], &k)
			}
			if k != sub {
				continue
			}
			if ty == "CLOSED" {
				_ = json.Unmarshal(m[2], &closed)
				return nil, closed
			}
			if ty == "EOSE" {
				cl.send("CLOSE", sub)
				cl.mu.Lock()
				defer cl.mu.Unlock()
				for _, e := range cl.events {
					if e.sub == sub {
						ids = append(ids, e.id)
					}
				}
				return ids, ""
			}
		case <-deadline:
			cl.t.Fatalf("timeout on REQ %v", f)
			return nil, ""
		}
	}
}

func (cl *client) count(f map[string]any) (int64, string) {
	sub := cl.subID()
	cl.send("COUNT", sub, f)
	deadline := time.After(5 * time.Second)
	var notice string
	for {
		select {
		case m := <-cl.ctl:
			var ty, k string
			_ = json.Unmarshal(m[0], &ty)
			if len(m) > 1 {
				_ = json.Unmarshal(m[1], &k)
			}
			if ty == "NOTICE" {
				notice = k
				continue
			}
			if k != sub {
				continue
			}
			if ty == "COUNT" {
				var c struct {
					Count int64 `json:"count"`
				}
				_ = json.Unmarshal(m[2], &c)
				return c.Count, notice
			}
			if ty == "CLOSED" {
				var r string
				_ = json.Unmarshal(m[2], &r)
				return 0, r
			}
		case <-deadline:
			cl.t.Fatalf("timeout on COUNT %v", f)
			return 0, ""
		}
	}
}

func (cl *client) publish(ev *nostr.Event) (bool, string) {
	cl.send("EVENT", ev)
	m := cl.waitCtl("OK", ev.ID, 5*time.Second)
	var ok bool
	var reason string
	_ = json.Unmarshal(m[2], &ok)
	if len(m) > 3 {
		_ = json.Unmarshal(m[3], &reason)
	}
	return ok, reason
}

func (cl *client) mustPublish(ev *nostr.Event) {
	cl.t.Helper()
	if ok, why := cl.publish(ev); !ok {
		cl.t.Fatalf("publish kind %d refused: %s", ev.Kind, why)
	}
}

func (cl *client) firstArrival(id string) (time.Time, bool) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	at, ok := cl.first[id]
	return at, ok
}

func (cl *client) body(id string) string {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	return cl.bodies[id]
}

func (cl *client) waitFor(id string, d time.Duration) bool {
	end := time.Now().Add(d)
	for time.Now().Before(end) {
		if _, ok := cl.firstArrival(id); ok {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return false
}

// ---- stats ----

type stats struct{ p50, p95, max float64 }

func summarize(ms []float64) stats {
	if len(ms) == 0 {
		return stats{}
	}
	s := append([]float64(nil), ms...)
	sort.Float64s(s)
	at := func(p float64) float64 { return s[min(len(s)-1, int(p/100*float64(len(s)-1)+0.5))] }
	return stats{at(50), at(95), s[len(s)-1]}
}

func ms(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }

func tokens(n int) string { return strconv.Itoa(n) }

// scaledDelay mirrors the documented formula: fraction x timeframe x test scale.
func scaledDelay(fraction string, tf int64) time.Duration {
	return protocol.DelayFor(protocol.Rat(fraction), tf, testScale)
}

// provider registers a public provider with the default table on r and returns its actor.
func registerProvider(t *testing.T, r *testRelay, tf int64, tiers []tierSpec) actor {
	t.Helper()
	p := newActor()
	c := dial(t, r.URL)
	c.mustPublish(p.ruleSet(1, tf, tiers))
	return p
}

// follower is an SA identity that holds `balance` tokens and puts `bps` of it on each listed provider.
func fundFollower(t *testing.T, r *testRelay, bal map[string]string, f actor, balance int, nonce uint64, split map[string]uint32) {
	t.Helper()
	bal[f.addr] = tokens(balance)
	r.SetBalances(bal)
	c := dial(t, r.URL)
	c.mustPublish(f.allocation(nonce, 0, split))
}

func evmsigSign(a actor, msg string) string { return evmsigtest.Sign(a.evm, msg) }

// evmsigDelegation signs a delegation of device key `devPub` by actor a's SA address.
func evmsigDelegation(a actor, devPub string, expires int64) nostr.Tag {
	return evmsigtest.SignDelegation(a.evm, devPub, expires)
}

func jsonString(b json.RawMessage, s *string) error { return json.Unmarshal(b, s) }

// event returns the raw event as received, parsed.
func (cl *client) event(id string) *nostr.Event {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	var ev nostr.Event
	if raw, ok := cl.raws[id]; ok && json.Unmarshal(raw, &ev) == nil {
		return &ev
	}
	return nil
}

// commitment builds a kind-9318 event of the shape sa_core.signals.build_commitment_event produces.
func (a actor) commitment(seq int, prev *string) *nostr.Event {
	c, _ := json.Marshal(map[string]any{"v": 1, "strategy_id": "alpha", "seq": seq, "prev": prev, "digest": strings.Repeat("ab", 32)})
	ev := &nostr.Event{PubKey: a.pub, CreatedAt: nostr.Now(), Kind: protocol.KindCommitment, Tags: nostr.Tags{a.delegation(time.Now().Add(time.Hour).Unix())}, Content: string(c)}
	_ = ev.Sign(a.sk)
	return ev
}
