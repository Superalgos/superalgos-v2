package chains

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const holder = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

func wei(tokens int64) *big.Int {
	return new(big.Int).Mul(big.NewInt(tokens), new(big.Int).Exp(big.NewInt(10), big.NewInt(18), nil))
}

func rpcServer(t *testing.T, balance *big.Int, mutate func(w http.ResponseWriter, r *http.Request) bool) (*httptest.Server, *[]string) {
	t.Helper()
	var mu sync.Mutex
	var calls []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Method string `json:"method"`
			Params []any  `json:"params"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		call := req.Params[0].(map[string]any)
		mu.Lock()
		calls = append(calls, fmt.Sprintf("%s %s %s", req.Method, call["to"], call["data"]))
		mu.Unlock()
		if mutate != nil && mutate(w, r) {
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": 1, "result": fmt.Sprintf("0x%064x", balance)})
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

func TestEVMBalanceOfCall(t *testing.T) {
	srv, calls := rpcServer(t, wei(1234), nil)
	e := EVM{RPCURL: srv.URL, Contract: "0xfb981ed9a92377ca4d75d924b9ca06df163924fd", Decimals: 18}
	got, err := e.Balance(context.Background(), holder)
	if err != nil || got.Cmp(wei(1234)) != 0 {
		t.Fatalf("%v %v", got, err)
	}
	want := "eth_call 0xfb981ed9a92377ca4d75d924b9ca06df163924fd 0x70a08231000000000000000000000000" + strings.ToLower(holder[2:])
	if len(*calls) != 1 || (*calls)[0] != want {
		t.Fatalf("call %v\nwant %s", *calls, want)
	}
}

func TestEVMNormalisesDecimals(t *testing.T) {
	for dec, raw := range map[int]*big.Int{6: big.NewInt(2_500_000), 18: wei(2), 20: new(big.Int).Mul(big.NewInt(2), new(big.Int).Exp(big.NewInt(10), big.NewInt(20), nil))} {
		srv, _ := rpcServer(t, raw, nil)
		got, err := EVM{RPCURL: srv.URL, Contract: "0xc17272c3e15074c55b810bceba02ba0c4481cd79", Decimals: dec}.Balance(context.Background(), holder)
		want := wei(2)
		if dec == 6 {
			want = new(big.Int).Div(wei(5), big.NewInt(2)) // 2.5 tokens
		}
		if err != nil || got.Cmp(want) != 0 {
			t.Errorf("decimals %d: got %v want %v (%v)", dec, got, want, err)
		}
	}
}

func TestEVMErrors(t *testing.T) {
	for name, tc := range map[string]struct {
		h   func(w http.ResponseWriter, r *http.Request) bool
		sub string
	}{
		"rpc error": {func(w http.ResponseWriter, _ *http.Request) bool {
			fmt.Fprint(w, `{"error":{"message":"rate limited"}}`)
			return true
		}, "rate limited"},
		"not json": {func(w http.ResponseWriter, _ *http.Request) bool { fmt.Fprint(w, `<html>`); return true }, "not JSON-RPC"},
		"empty":    {func(w http.ResponseWriter, _ *http.Request) bool { fmt.Fprint(w, `{"result":"0x"}`); return true }, "is "},
		"not hex":  {func(w http.ResponseWriter, _ *http.Request) bool { fmt.Fprint(w, `{"result":"0xzz"}`); return true }, "not hex"},
	} {
		srv, _ := rpcServer(t, big.NewInt(0), tc.h)
		_, err := EVM{RPCURL: srv.URL, Contract: "0x00", Decimals: 18}.Balance(context.Background(), holder)
		if err == nil || !strings.Contains(err.Error(), tc.sub) {
			t.Errorf("%s: err=%v, want %q", name, err, tc.sub)
		}
	}
	if _, err := (EVM{RPCURL: "http://127.0.0.1:1", Decimals: 18}).Balance(context.Background(), holder); err == nil || !strings.Contains(err.Error(), "unreachable") {
		t.Errorf("unreachable: %v", err)
	}
	if _, err := (EVM{RPCURL: "http://x", Decimals: 18}).Balance(context.Background(), "0x12"); err == nil {
		t.Error("short address accepted")
	}
}

func fixtureFile(t *testing.T, m map[string]string) Fixture {
	t.Helper()
	p := filepath.Join(t.TempDir(), "f.json")
	b, _ := json.Marshal(m)
	if err := os.WriteFile(p, b, 0o600); err != nil {
		t.Fatal(err)
	}
	return Fixture{Path: p}
}

func TestFixture(t *testing.T) {
	f := fixtureFile(t, map[string]string{holder: "1500.5"})
	got, err := f.Balance(context.Background(), strings.ToLower(holder))
	want := new(big.Int).Mul(big.NewInt(15005), new(big.Int).Exp(big.NewInt(10), big.NewInt(17), nil))
	if err != nil || got.Cmp(want) != 0 {
		t.Fatalf("%v %v", got, err)
	}
	if z, _ := f.Balance(context.Background(), "0x"+strings.Repeat("0", 40)); z.Sign() != 0 {
		t.Error("unknown address should hold 0")
	}
	if _, err := (Fixture{Path: "/nonexistent/f.json"}).Balance(context.Background(), holder); err == nil || !strings.Contains(err.Error(), "create it") {
		t.Errorf("missing file: %v", err)
	}
}

type flaky struct {
	mu  sync.Mutex
	bal *big.Int
	err error
	n   int
}

func (f *flaky) Balance(context.Context, string) (*big.Int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.n++
	return f.bal, f.err
}

func TestPowerSumsChainsAndCaches(t *testing.T) {
	a, b := &flaky{bal: wei(100)}, &flaky{bal: wei(23)}
	now := time.Unix(1000, 0)
	p := &Power{Chains: []Chain{{"bsc", a}, {"eth", b}}, TTL: 5 * time.Second, MaxStale: time.Minute, Now: func() time.Time { return now }}
	for i := 0; i < 3; i++ {
		got, err := p.Total(context.Background(), holder)
		if err != nil || got.Cmp(wei(123)) != 0 {
			t.Fatalf("%v %v", got, err)
		}
	}
	if a.n != 1 || b.n != 1 {
		t.Fatalf("cache missed: %d %d lookups", a.n, b.n)
	}
	now = now.Add(6 * time.Second)
	_, _ = p.Total(context.Background(), holder)
	if a.n != 2 {
		t.Fatalf("expired entry not refreshed: %d", a.n)
	}
}

// Errors never raise power: a failing chain keeps its last good value for MaxStale, then counts as zero.
func TestPowerFailingChainDegradesToZero(t *testing.T) {
	a, b := &flaky{bal: wei(100)}, &flaky{bal: wei(50)}
	now := time.Unix(1000, 0)
	p := &Power{Chains: []Chain{{"bsc", a}, {"eth", b}}, TTL: time.Second, MaxStale: 30 * time.Second, Now: func() time.Time { return now }}
	if got, _ := p.Total(context.Background(), holder); got.Cmp(wei(150)) != 0 {
		t.Fatalf("baseline %v", got)
	}
	b.mu.Lock()
	b.err, b.bal = errors.New("rpc down"), nil
	b.mu.Unlock()
	now = now.Add(2 * time.Second)
	got, err := p.Total(context.Background(), holder)
	if got.Cmp(wei(150)) != 0 || err == nil || !strings.Contains(err.Error(), "chain eth") {
		t.Fatalf("within MaxStale want last good 150 + error, got %v %v", got, err)
	}
	now = now.Add(40 * time.Second)
	got, err = p.Total(context.Background(), holder)
	if got.Cmp(wei(100)) != 0 || err == nil {
		t.Fatalf("beyond MaxStale the failing chain must count as zero: %v %v", got, err)
	}
	if p.Errors.Load() != 2 {
		t.Errorf("error counter %d", p.Errors.Load())
	}
	// a chain that never answered contributes nothing, and that never raises power
	q := &Power{Chains: []Chain{{"x", &flaky{err: errors.New("down")}}}, TTL: time.Second, MaxStale: time.Minute}
	if got, err := q.Total(context.Background(), holder); got.Sign() != 0 || err == nil {
		t.Fatalf("%v %v", got, err)
	}
}

// M1: chain reads are capped relay-wide. Over the cap a chain is not read at all; the last good value
// serves for MaxStale, an address never read before counts as zero, and the tokens refill with time.
func TestPowerGlobalLookupCap(t *testing.T) {
	c := &flaky{bal: wei(10)}
	now := time.Unix(1000, 0)
	p := &Power{Chains: []Chain{{"bsc", c}}, TTL: time.Second, MaxStale: time.Minute, Now: func() time.Time { return now },
		MaxLookupsPerSec: 2, LookupBurst: 3}
	addr := func(i int) string { return fmt.Sprintf("0x%040x", i+1) }
	var zero int
	for i := 0; i < 10; i++ {
		got, err := p.Total(context.Background(), addr(i))
		if got.Sign() == 0 {
			zero++
			if !errors.Is(err, ErrThrottled) {
				t.Fatalf("address %d: want ErrThrottled, got %v", i, err)
			}
		}
	}
	if c.n != 3 || zero != 7 {
		t.Fatalf("burst 3: %d reads and %d throttled addresses, want 3 and 7", c.n, zero)
	}
	if p.Throttled.Load() != 7 || p.Errors.Load() != 0 {
		t.Errorf("throttled %d errors %d, want 7 and 0 (a skipped read is not an RPC error)", p.Throttled.Load(), p.Errors.Load())
	}
	// an address already read keeps its last good value while the cap bites
	now = now.Add(2 * time.Second)
	got, _ := p.Total(context.Background(), addr(0)) // refresh uses a token
	if got.Cmp(wei(10)) != 0 {
		t.Fatalf("refill: %v", got)
	}
	for i := 20; i < 30; i++ { // drain the bucket
		_, _ = p.Total(context.Background(), addr(i))
	}
	now = now.Add(2 * time.Second)
	if got, err := p.Total(context.Background(), addr(0)); got.Cmp(wei(10)) != 0 || err != nil {
		t.Fatalf("after refill the address must be read again: %v %v", got, err)
	}
	for i := 40; i < 50; i++ {
		_, _ = p.Total(context.Background(), addr(i))
	}
	now = now.Add(1500 * time.Millisecond) // TTL over, but the bucket holds under one token... then spent above
	got, _ = p.Total(context.Background(), addr(0))
	if got.Cmp(wei(10)) != 0 {
		t.Errorf("a throttled refresh must serve the last good value within MaxStale, got %v", got)
	}
}

// M1: a chain that keeps failing is not read again until an exponentially growing delay has passed,
// whichever address asks.
func TestPowerBackoffGrowsExponentially(t *testing.T) {
	c := &flaky{err: errors.New("rpc down")}
	now := time.Unix(1000, 0)
	p := &Power{Chains: []Chain{{"bsc", c}}, TTL: time.Second, MaxStale: time.Minute, Now: func() time.Time { return now },
		BackoffBase: 2 * time.Second, BackoffMax: 10 * time.Second}
	read := func(i int) { _, _ = p.Total(context.Background(), fmt.Sprintf("0x%040x", i+1)) }
	read(0) // fails: next read allowed after 2 s
	read(1)
	read(2)
	if c.n != 1 {
		t.Fatalf("%d reads during the first back-off window, want 1", c.n)
	}
	now = now.Add(2100 * time.Millisecond)
	read(3) // fails again: 4 s
	now = now.Add(3 * time.Second)
	read(4)
	if c.n != 2 {
		t.Fatalf("%d reads inside the doubled window, want 2", c.n)
	}
	now = now.Add(1500 * time.Millisecond)
	read(5) // allowed (4.5 s since the failure)
	if c.n != 3 {
		t.Fatalf("%d reads after the doubled window, want 3", c.n)
	}
	for i := 0; i < 6; i++ { // 8 s, then capped at 10 s
		now = now.Add(11 * time.Second)
		read(10 + i)
	}
	if c.n != 9 {
		t.Fatalf("%d reads with the window capped at 10 s, want 9", c.n)
	}
	// recovery resets the window
	c.mu.Lock()
	c.err, c.bal = nil, wei(7)
	c.mu.Unlock()
	now = now.Add(11 * time.Second)
	if got, err := p.Total(context.Background(), fmt.Sprintf("0x%040x", 99)); err != nil || got.Cmp(wei(7)) != 0 {
		t.Fatalf("recovery: %v %v", got, err)
	}
	if p.backedOff("bsc", now) {
		t.Error("a successful read must end the back-off")
	}
}

func TestPowerKeepsLastGoodThroughBackoffForMaxStale(t *testing.T) {
	c := &flaky{bal: wei(40)}
	now := time.Unix(1000, 0)
	p := &Power{Chains: []Chain{{"bsc", c}}, TTL: time.Second, MaxStale: 30 * time.Second, Now: func() time.Time { return now },
		BackoffBase: time.Minute, BackoffMax: time.Hour}
	if got, _ := p.Total(context.Background(), holder); got.Cmp(wei(40)) != 0 {
		t.Fatal("baseline")
	}
	c.mu.Lock()
	c.err = errors.New("down")
	c.mu.Unlock()
	now = now.Add(2 * time.Second)
	_, _ = p.Total(context.Background(), holder) // fails once, back-off for a minute
	reads := c.n
	now = now.Add(10 * time.Second)
	if got, _ := p.Total(context.Background(), holder); got.Cmp(wei(40)) != 0 || c.n != reads {
		t.Fatalf("within MaxStale during the back-off: %v with %d new reads, want 40 and 0", got, c.n-reads)
	}
	now = now.Add(40 * time.Second)
	if got, _ := p.Total(context.Background(), holder); got.Sign() != 0 {
		t.Fatalf("past MaxStale the chain counts as zero, got %v", got)
	}
}
