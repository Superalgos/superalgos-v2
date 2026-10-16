package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/evmsig/evmsigtest"
	"github.com/superalgos/sarelay/internal/protocol"
)

var binPath string

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "sarelay-e2e")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	binPath = filepath.Join(dir, "sarelay")
	args := []string{"build", "-o", binPath}
	if raceOn {
		args = append(args, "-race") // the relay under test is itself race-instrumented
	}
	args = append(args, ".")
	if out, err := exec.Command("go", args...).CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "building sarelay failed: %v\n%s", err, out)
		os.Exit(1)
	}
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

func freeAddr(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("cannot bind a local port: %v", err)
	}
	defer l.Close()
	return l.Addr().String()
}

// lockedBuf is a bytes.Buffer safe for exec's copier goroutine and the test to share.
type lockedBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuf) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}
func (l *lockedBuf) String() string { l.mu.Lock(); defer l.mu.Unlock(); return l.b.String() }

type proc struct {
	cmd             *exec.Cmd
	stderr          *lockedBuf
	relayURL, admin string
	token           string
}

func writeConfig(t *testing.T, dir, relayAddr, adminAddr string, extra string) string {
	t.Helper()
	bal := filepath.Join(dir, "balances.json")
	if _, err := os.Stat(bal); err != nil {
		_ = os.WriteFile(bal, []byte("{}"), 0o600)
	}
	cfg := fmt.Sprintf(`listen = %q
storage_path = %q
allocation_delay = "0s"
core_delay = "0s"
max_anchor_latency = "0s"
time_scale = 0.0166667
%s
[admin]
listen = %q

[log]
format = "json"
level = "info"

[oracle]
cache_ttl = "2s"
max_lookups_per_sec = 100000
lookup_burst = 100000

[limits]
max_conns_per_ip = 100000   # one test host opens hundreds of sockets

[policy]
min_provider_power = 0      # fixture balances: no registration cost in the functional tests
min_proposer_power = 0

[rate]
zero_power_per_sec = 100000
zero_power_burst = 100000

[[chains]]
id = "fixture"
kind = "fixture"
file = %q
`, relayAddr, filepath.Join(dir, "data"), extra, adminAddr, bal)
	p := filepath.Join(dir, "sarelay.toml")
	if err := os.WriteFile(p, []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func start(t *testing.T, dir string, extra string) *proc {
	t.Helper()
	relayAddr, adminAddr := freeAddr(t), freeAddr(t)
	cfg := writeConfig(t, dir, relayAddr, adminAddr, extra)
	p := &proc{cmd: exec.Command(binPath, "-config", cfg), stderr: &lockedBuf{}, relayURL: "ws://" + relayAddr, admin: "http://" + adminAddr}
	p.cmd.Stderr = p.stderr
	if err := p.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 200; i++ {
		if c, err := net.Dial("tcp", relayAddr); err == nil {
			c.Close()
			if b, err := os.ReadFile(filepath.Join(dir, "data", "admin.token")); err == nil {
				p.token = strings.TrimSpace(string(b))
			}
			t.Cleanup(func() { p.kill() })
			return p
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("sarelay did not listen: %s", p.stderr.String())
	return nil
}

func (p *proc) kill() { _ = p.cmd.Process.Kill() } // harmless when it already exited

func (p *proc) rssMB() float64 {
	out, err := exec.Command("ps", "-o", "rss=", "-p", strconv.Itoa(p.cmd.Process.Pid)).Output()
	if err != nil {
		return 0
	}
	kb, _ := strconv.Atoi(strings.TrimSpace(string(out)))
	return float64(kb) / 1024
}

// ---- tiny nostr client ----

type conn struct {
	c      *websocket.Conn
	ctx    context.Context
	cancel context.CancelFunc
	mu     sync.Mutex
	first  map[string]time.Time
	oks    chan string
	eose   chan struct{}
}

func dial(t *testing.T, url string) *conn {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		cancel()
		t.Fatalf("dial: %v", err)
	}
	cn := &conn{c: c, ctx: ctx, cancel: cancel, first: map[string]time.Time{}, oks: make(chan string, 64), eose: make(chan struct{}, 4)}
	go func() {
		for {
			_, data, err := c.Read(ctx)
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
				var ev struct {
					ID string `json:"id"`
				}
				_ = json.Unmarshal(m[2], &ev)
				cn.mu.Lock()
				if _, ok := cn.first[ev.ID]; !ok {
					cn.first[ev.ID] = at
				}
				cn.mu.Unlock()
			case "OK":
				var ok bool
				var reason string
				_ = json.Unmarshal(m[2], &ok)
				if len(m) > 3 {
					_ = json.Unmarshal(m[3], &reason)
				}
				cn.oks <- fmt.Sprintf("%v|%s", ok, reason)
			case "EOSE":
				cn.eose <- struct{}{}
			}
		}
	}()
	t.Cleanup(func() { cancel(); _ = c.CloseNow() })
	return cn
}

func (c *conn) send(v ...any) {
	b, _ := json.Marshal(v)
	_ = c.c.Write(c.ctx, websocket.MessageText, b)
}

func (c *conn) publish(t *testing.T, ev *nostr.Event) {
	t.Helper()
	c.send("EVENT", ev)
	select {
	case r := <-c.oks:
		if !strings.HasPrefix(r, "true|") {
			t.Fatalf("publish kind %d refused: %s", ev.Kind, r)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no OK for publish")
	}
}

func (c *conn) subscribe(t *testing.T) {
	t.Helper()
	c.send("REQ", "live", map[string]any{"kinds": []int{protocol.KindSignal}})
	select {
	case <-c.eose:
	case <-time.After(10 * time.Second):
		t.Fatal("no EOSE")
	}
}

func (c *conn) arrival(id string) (time.Time, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	at, ok := c.first[id]
	return at, ok
}

type provider struct {
	evm     *secp256k1.PrivateKey
	addr    string
	sk, pub string
}

func newProvider() provider {
	k, _ := secp256k1.GeneratePrivateKey()
	sk := nostr.GeneratePrivateKey()
	pub, _ := nostr.GetPublicKey(sk)
	return provider{k, evmsigtest.AddressOf(k), sk, pub}
}

func (p provider) delegation() nostr.Tag {
	return evmsigtest.SignDelegation(p.evm, p.pub, time.Now().Add(time.Hour).Unix())
}

func (p provider) ruleSet(tf int64, fraction float64) *nostr.Event {
	sig := evmsigtest.Sign(p.evm, fmt.Sprintf("sa-ruleset:%s:%d:0@%.4f", p.pub, tf, fraction))
	c, _ := json.Marshal(map[string]any{"v": 1, "provider": p.pub, "sa_address": p.addr, "timeframe": tf,
		"tiers": []map[string]any{{"min_power": 0, "delay_fraction": fraction}}, "sig": sig})
	ev := &nostr.Event{PubKey: p.pub, CreatedAt: nostr.Now(), Kind: protocol.KindRuleSet, Tags: nostr.Tags{{"d", p.pub}, p.delegation()}, Content: string(c)}
	_ = ev.Sign(p.sk)
	return ev
}

func (p provider) signal(tf int64, seq int) *nostr.Event {
	c := fmt.Sprintf(`{"v":1,"seq":%d,"timeframe":%d,"created_ms":%d,"instrument":"BINANCE:BTCUSDT","side":"buy","action":"open","size_fraction":0.1}`, seq, tf, time.Now().UnixMilli())
	ev := &nostr.Event{PubKey: p.pub, CreatedAt: nostr.Now(), Kind: protocol.KindSignal, Tags: nostr.Tags{p.delegation()}, Content: c}
	_ = ev.Sign(p.sk)
	return ev
}

func get(t *testing.T, url, token string) (int, string) {
	t.Helper()
	req, _ := http.NewRequest("GET", url, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

func TestBinaryStartsServesAndStopsCleanly(t *testing.T) {
	dir := t.TempDir()
	p := start(t, dir, "")
	if p.token == "" || len(p.token) < 32 {
		t.Fatalf("admin token was not generated into storage: %q", p.token)
	}
	if fi, err := os.Stat(filepath.Join(dir, "data", "admin.token")); err != nil || fi.Mode().Perm() != 0o600 {
		t.Errorf("admin.token mode: %v %v", fi, err)
	}
	if code, _ := get(t, p.admin+"/metrics", ""); code != 401 {
		t.Errorf("metrics without token: %d", code)
	}
	if code, body := get(t, p.admin+"/metrics", p.token); code != 200 || !strings.Contains(body, "sarelay_connections") {
		t.Errorf("metrics: %d %s", code, body)
	}
	if code, body := get(t, p.admin+"/healthz", p.token); code != 200 || !strings.Contains(body, "ok") {
		t.Errorf("healthz: %d %s", code, body)
	}
	if strings.Contains(p.stderr.String(), p.token) {
		t.Error("the admin token was written to the log")
	}
	if err := p.cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- p.cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("SIGTERM exit: %v\n%s", err, p.stderr.String())
		}
	case <-time.After(8 * time.Second):
		t.Fatal("relay did not stop on SIGTERM")
	}
	for _, line := range strings.Split(strings.TrimSpace(p.stderr.String()), "\n") {
		var m map[string]any
		if json.Unmarshal([]byte(line), &m) != nil || m["msg"] == nil {
			t.Errorf("log line is not structured JSON: %q", line)
		}
	}
}

func TestBinaryRejectsBadConfig(t *testing.T) {
	dir := t.TempDir()
	bad := filepath.Join(dir, "bad.toml")
	_ = os.WriteFile(bad, []byte("time_scale = -1\n"), 0o600)
	out, err := exec.Command(binPath, "-config", bad).CombinedOutput()
	if err == nil || !strings.Contains(string(out), "time_scale") || !strings.Contains(string(out), "no [[chains]]") {
		t.Errorf("bad config: err=%v out=%s", err, out)
	}
	out, err = exec.Command(binPath, "-config", filepath.Join(dir, "missing.toml")).CombinedOutput()
	if err == nil || !strings.Contains(string(out), "cannot read config") {
		t.Errorf("missing config: err=%v out=%s", err, out)
	}
	ok := writeConfig(t, dir, "127.0.0.1:1", "127.0.0.1:2", "")
	if out, err := exec.Command(binPath, "-config", ok, "-check").CombinedOutput(); err != nil || !strings.Contains(string(out), "config ok") {
		t.Errorf("-check: %v %s", err, out)
	}
}

// SEP-3.41 against the real process: SIGKILL (no clean shutdown) with a release pending, then restart.
func TestBinaryKillNineKeepsPendingRelease(t *testing.T) { killNine(t) }

// TestPerfBinaryKillNine is the machine-speed half: delivered within 200 ms of schedule. Local only
// (SA_PERF=1, `make perf`); CI never runs it.
func TestPerfBinaryKillNine(t *testing.T) {
	perfOnly(t)
	if e := killNine(t); e > 200 {
		t.Errorf("release error %.1f ms exceeds 200 ms", e)
	}
}

// perfOnly skips a performance test unless SA_PERF=1 asks for it.
func perfOnly(t *testing.T) {
	t.Helper()
	if os.Getenv("SA_PERF") != "1" {
		t.Skip("performance test: run locally with `make perf` (SA_PERF=1)")
	}
}

// killNine checks the pending release survives and is never early; it returns the release error in ms.
func killNine(t *testing.T) float64 {
	t.Helper()
	dir := t.TempDir()
	p1 := start(t, dir, "")
	prov := newProvider()
	pub := dial(t, p1.relayURL)
	pub.publish(t, prov.ruleSet(300, 1)) // everyone: 5 s at scale 1/60
	sig := prov.signal(300, 1)
	// The reference is taken BEFORE publishing: the relay stamps its receipt time while handling the
	// event, i.e. before it answers OK, so a reference read after publish() returns is later than the
	// relay's and made a correct release look early by the publish round trip (CI saw -7.5 ms).
	sentAt := time.Now()
	pub.publish(t, sig)
	time.Sleep(700 * time.Millisecond)
	_ = p1.cmd.Process.Signal(syscall.SIGKILL)
	_, _ = p1.cmd.Process.Wait()

	p2 := start(t, dir, "")
	sub := dial(t, p2.relayURL)
	sub.subscribe(t)
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		if _, ok := sub.arrival(sig.ID); ok {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	at, ok := sub.arrival(sig.ID)
	if !ok {
		t.Fatalf("pending release lost after kill -9\n%s", p2.stderr.String())
	}
	want := sentAt.Add(5 * time.Second)
	t.Logf("kill -9 restart: delivered %.1f ms from its original release time (negative = early)", ms(at.Sub(want)))
	// Never early (tier fairness, SEP-3.41): the relay's release is receipt+5 s and receipt >= sentAt.
	e := ms(at.Sub(want))
	if e < 0 {
		t.Errorf("released %.1f ms before its schedule after kill -9 restart", -e)
	}
	return e
}

func ms(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }

// 300 subscribers against the real binary: relay-only RSS, release error.
func TestBinaryLoad300Subscribers(t *testing.T) { binaryLoad300(t) }

// TestPerfBinaryLoad300Subscribers is the machine-speed half: p95 release error under 100 ms. Local only.
func TestPerfBinaryLoad300Subscribers(t *testing.T) {
	perfOnly(t)
	if p95 := binaryLoad300(t); p95 > 100 {
		t.Errorf("p95 release error %.1f ms exceeds 100 ms", p95)
	}
}

// binaryLoad300 checks correctness (nothing missing, nothing early, no data race) and returns the p95 release error.
func binaryLoad300(t *testing.T) float64 {
	t.Helper()
	dir := t.TempDir()
	p := start(t, dir, "")
	rssIdle := p.rssMB()
	prov := newProvider()
	pub := dial(t, p.relayURL)
	pub.publish(t, prov.ruleSet(120, 1)) // everyone: 2 s

	const subs, nSig = 300, 60
	var cs []*conn
	var wg sync.WaitGroup
	var mu sync.Mutex
	for i := 0; i < subs; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c := dial(t, p.relayURL)
			c.subscribe(t)
			mu.Lock()
			cs = append(cs, c)
			mu.Unlock()
		}()
	}
	wg.Wait()
	rssConn := p.rssMB()
	type sent struct {
		id string
		at time.Time
	}
	var sents []sent
	for i := 0; i < nSig; i++ {
		ev := prov.signal(120, i)
		at := time.Now()
		pub.publish(t, ev)
		sents = append(sents, sent{ev.ID, at})
		time.Sleep(40 * time.Millisecond)
	}
	time.Sleep(4 * time.Second)
	rssAfter := p.rssMB()
	var errs []float64
	missing := 0
	for _, c := range cs {
		for _, s := range sents {
			at, ok := c.arrival(s.id)
			if !ok {
				missing++
				continue
			}
			e := ms(at.Sub(s.at) - 2*time.Second)
			errs = append(errs, e)
			if e < -2 {
				t.Errorf("delivery %.1f ms early", -e)
			}
		}
	}
	sort.Float64s(errs)
	at := func(q float64) float64 { return errs[int(q*float64(len(errs)-1))] }
	t.Logf("real binary, %d subscribers, %d signals, %d deliveries, %d missing: release error p50 %.2f ms p95 %.2f ms max %.2f ms; relay RSS %.0f MB idle, %.0f MB with %d connections, %.0f MB after (race build: %v)",
		subs, nSig, len(errs), missing, at(0.5), at(0.95), errs[len(errs)-1], rssIdle, rssConn, subs, rssAfter, raceOn)
	if missing != 0 {
		t.Errorf("%d deliveries missing", missing)
	}
	if strings.Contains(p.stderr.String(), "DATA RACE") {
		t.Errorf("relay reported a data race:\n%s", p.stderr.String())
	}
	return at(0.95)
}
