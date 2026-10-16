// Package chains reads SA token balances from the chain registry (BSC, Ethereum, zkSync, or a
// JSON fixture file for tests) and sums them into an identity's token power.
package chains

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/superalgos/sarelay/internal/evmsig"
	"github.com/superalgos/sarelay/internal/protocol"
)

// Oracle answers "how many SA does this address hold on this chain?" in 18-decimal base units.
type Oracle interface {
	Balance(ctx context.Context, address string) (*big.Int, error)
}

// Chain is one registry entry.
type Chain struct {
	ID     string
	Oracle Oracle
}

// ---- fixture ----

// Fixture reads a JSON file {"0xaddr": "1500.5", ...} (whole tokens) on every call so tests can
// change balances while the relay runs. Unknown addresses hold 0.
type Fixture struct{ Path string }

// Balance implements Oracle.
func (f Fixture) Balance(_ context.Context, addr string) (*big.Int, error) {
	raw, err := os.ReadFile(f.Path)
	if err != nil {
		return nil, fmt.Errorf("fixture file %s unreadable: %w; create it as a JSON object {\"0xaddr\": \"tokens\"}", f.Path, err)
	}
	var m map[string]string
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("fixture file %s is not a JSON object of strings: %w", f.Path, err)
	}
	for k, v := range m {
		if strings.EqualFold(k, addr) {
			c, err := protocol.Canonical(v, 40, 18)
			if err != nil {
				return nil, fmt.Errorf("fixture balance for %s: %w", k, err)
			}
			return protocol.TokensToWei(c), nil
		}
	}
	return new(big.Int), nil
}

// ---- EVM JSON-RPC ----

// EVM calls balanceOf(address) on the SA contract through a JSON-RPC endpoint. Read-only eth_call.
type EVM struct {
	RPCURL   string
	Contract string
	Decimals int
	Client   *http.Client
}

// Balance implements Oracle. The result is normalised to 18 decimals.
func (e EVM) Balance(ctx context.Context, addr string) (*big.Int, error) {
	a, err := evmsig.NormalizeAddress(addr)
	if err != nil {
		return nil, err
	}
	data := "0x70a08231" + strings.Repeat("0", 24) + a[2:] // balanceOf(address)
	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": "eth_call",
		"params": []any{map[string]string{"to": e.Contract, "data": data}, "latest"},
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.RPCURL, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("bad RPC URL %q: %w", e.RPCURL, err)
	}
	req.Header.Set("Content-Type", "application/json")
	cl := e.Client
	if cl == nil {
		cl = &http.Client{Timeout: 5 * time.Second}
	}
	resp, err := cl.Do(req)
	if err != nil {
		return nil, fmt.Errorf("RPC %s unreachable: %w", e.RPCURL, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	var out struct {
		Result string `json:"result"`
		Error  *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("RPC %s returned HTTP %d, not JSON-RPC", e.RPCURL, resp.StatusCode)
	}
	if out.Error != nil {
		return nil, fmt.Errorf("RPC %s error: %s", e.RPCURL, out.Error.Message)
	}
	h := strings.TrimPrefix(out.Result, "0x")
	if h == "" {
		return nil, fmt.Errorf("empty eth_call result from %s (is %s a contract on this chain?)", e.RPCURL, e.Contract)
	}
	if _, err := hex.DecodeString(strings.Repeat("0", len(h)%2) + h); err != nil {
		return nil, fmt.Errorf("eth_call result %q is not hex", out.Result)
	}
	v, ok := new(big.Int).SetString(h, 16)
	if !ok {
		return nil, fmt.Errorf("eth_call result %q is not a number", out.Result)
	}
	return ToWei18(v, e.Decimals), nil
}

// ToWei18 rescales a raw token amount with `decimals` decimals to 18 decimals (rounding down).
func ToWei18(v *big.Int, decimals int) *big.Int {
	switch {
	case decimals == 18:
		return v
	case decimals < 18:
		return new(big.Int).Mul(v, new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(18-decimals)), nil))
	default:
		return new(big.Int).Quo(v, new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(decimals-18)), nil))
	}
}

// failureTTL is how long a lookup that had an error is cached.
const failureTTL = 2 * time.Second

// ---- token power ----

// Power sums an address's balance over the registry, with a TTL cache. A chain that fails keeps
// its last good value for MaxStale, then counts as zero: errors never raise power.
type Power struct {
	Chains   []Chain
	TTL      time.Duration
	MaxStale time.Duration
	Now      func() time.Time

	// MaxLookupsPerSec and LookupBurst are a relay-wide token bucket over chain reads (one token per
	// chain per address). Over the cap a chain is not read: the last good value serves for MaxStale,
	// then the chain counts as 0. 0 disables the cap (tests that build a Power directly).
	MaxLookupsPerSec float64
	LookupBurst      float64
	// BackoffBase and BackoffMax: a chain that fails is not read again for base x 2^(failures-1),
	// at most max, whichever address asks. 0 disables the back-off.
	BackoffBase time.Duration
	BackoffMax  time.Duration

	Lookups   atomic.Int64 // chain reads attempted
	Errors    atomic.Int64 // chain reads failed
	Throttled atomic.Int64 // chain reads skipped by the rate cap or a back-off

	mu     sync.Mutex
	cache  map[string]*powerEntry
	tokens float64
	tokAt  time.Time
	fails  map[string]*chainFail
}

type chainFail struct {
	n     int
	until time.Time
}

// ErrThrottled marks a chain read that was skipped, not attempted.
var ErrThrottled = errors.New("chain read skipped: relay-wide lookup cap or back-off in force")

// maxCache bounds the per-address cache even when every entry is fresh.
const maxCache = 16384

// take reserves n chain reads from the global bucket; false means none may be made now.
func (p *Power) take(now time.Time, n float64) bool {
	if p.MaxLookupsPerSec <= 0 {
		return true
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.tokAt.IsZero() {
		p.tokens, p.tokAt = p.LookupBurst, now
	}
	p.tokens = min(p.LookupBurst, p.tokens+now.Sub(p.tokAt).Seconds()*p.MaxLookupsPerSec)
	p.tokAt = now
	if p.tokens < n {
		return false
	}
	p.tokens -= n
	return true
}

// backedOff reports whether chain id is inside a back-off window.
func (p *Power) backedOff(id string, now time.Time) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	f := p.fails[id]
	return f != nil && now.Before(f.until)
}

// note records the outcome of a real chain read.
func (p *Power) note(id string, ok bool, now time.Time) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.fails == nil {
		p.fails = map[string]*chainFail{}
	}
	if ok {
		delete(p.fails, id)
		return
	}
	if p.BackoffBase <= 0 {
		return
	}
	f := p.fails[id]
	if f == nil {
		f = &chainFail{}
		p.fails[id] = f
	}
	f.n++
	d := p.BackoffBase
	for i := 1; i < f.n && d < p.BackoffMax; i++ {
		d *= 2
	}
	if p.BackoffMax > 0 && d > p.BackoffMax {
		d = p.BackoffMax
	}
	f.until = now.Add(d)
}

type powerEntry struct {
	total *big.Int
	at    time.Time
	good  map[string]chainGood // chain id -> last good
	err   error
}

type chainGood struct {
	bal *big.Int
	at  time.Time
}

func (p *Power) now() time.Time {
	if p.Now != nil {
		return p.Now()
	}
	return time.Now()
}

// Total returns the address's summed power in 18-decimal base units. On partial failure it returns
// the conservative sum together with a non-nil error; callers log it and use the value.
func (p *Power) Total(ctx context.Context, addr string) (*big.Int, error) {
	key := strings.ToLower(addr)
	now := p.now()
	p.mu.Lock()
	if p.cache == nil {
		p.cache = map[string]*powerEntry{}
	}
	e := p.cache[key]
	if e != nil && now.Sub(e.at) < p.TTL {
		defer p.mu.Unlock()
		return e.total, e.err
	}
	var prevGood map[string]chainGood
	if e != nil {
		prevGood = e.good
	}
	p.mu.Unlock()

	type res struct {
		id  string
		bal *big.Int
		err error
	}
	out := make([]res, len(p.Chains))
	var wg sync.WaitGroup
	for i, c := range p.Chains {
		if p.backedOff(c.ID, now) || !p.take(now, 1) {
			p.Throttled.Add(1)
			out[i] = res{c.ID, nil, fmt.Errorf("chain %s: %w", c.ID, ErrThrottled)}
			continue
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			p.Lookups.Add(1)
			b, err := c.Oracle.Balance(ctx, key)
			p.note(c.ID, err == nil, now)
			out[i] = res{c.ID, b, err}
		}()
	}
	wg.Wait()

	total := new(big.Int)
	good := map[string]chainGood{}
	var errs []error
	for _, r := range out {
		if r.err == nil {
			good[r.id] = chainGood{r.bal, now}
			total.Add(total, r.bal)
			continue
		}
		if !errors.Is(r.err, ErrThrottled) {
			p.Errors.Add(1)
		}
		errs = append(errs, fmt.Errorf("chain %s: %w", r.id, r.err))
		if g, ok := prevGood[r.id]; ok && now.Sub(g.at) <= p.MaxStale {
			good[r.id] = g
			total.Add(total, g.bal)
		}
	}
	err := errors.Join(errs...)
	at := now
	if err != nil && p.TTL > failureTTL { // retry failures soon instead of serving them for a full TTL
		at = now.Add(failureTTL - p.TTL)
	}
	p.mu.Lock()
	p.cache[key] = &powerEntry{total: total, at: at, good: good, err: err}
	if len(p.cache) > 4096 { // drop entries too old to serve even as a stale fallback
		for k, e := range p.cache {
			if now.Sub(e.at) > p.MaxStale+p.TTL {
				delete(p.cache, k)
			}
		}
	}
	for k := range p.cache { // hard bound: never hold more than maxCache addresses
		if len(p.cache) <= maxCache {
			break
		}
		delete(p.cache, k)
	}
	p.mu.Unlock()
	return total, err
}
