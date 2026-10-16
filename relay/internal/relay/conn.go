package relay

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/big"
	"sync"
	"sync/atomic"
	"time"

	"github.com/fiatjaf/khatru"
	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/chains"
	"github.com/superalgos/sarelay/internal/evmsig"
)

// conn is the relay's view of one websocket: who authenticated, what power they hold.
type conn struct {
	evalMu  sync.Mutex // held while an AUTH event is evaluated, so concurrent requests wait for the result
	mu      sync.Mutex
	authID  string // id of the AUTH event last evaluated
	pubkey  string // device key proven by NIP-42
	addr    string // SA address from a valid sa-delegation tag, "" if none
	expires int64  // delegation expiry, unix seconds
	total   atomic.Pointer[big.Int]

	read  bucket
	pub   bucket // rejected publishes
	pubOK bucket // accepted publishes
}

type identity struct {
	pubkey string
	addr   string
	total  *big.Int
}

// identity returns the connection's current identity. An expired delegation drops back to anonymous.
func (c *conn) identity(now time.Time) identity {
	c.mu.Lock()
	pk, addr, exp := c.pubkey, c.addr, c.expires
	c.mu.Unlock()
	if addr == "" || now.Unix() >= exp {
		return identity{pubkey: pk, total: new(big.Int)}
	}
	t := c.total.Load()
	if t == nil {
		t = new(big.Int)
	}
	return identity{pubkey: pk, addr: addr, total: t}
}

func (e *Engine) connOf(ws *khatru.WebSocket) *conn {
	if v, ok := e.conns.Load(ws); ok {
		return v.(*conn)
	}
	v, _ := e.conns.LoadOrStore(ws, &conn{})
	return v.(*conn)
}

// connFromCtx returns the connection for a request context, or nil for internal calls.
func (e *Engine) connFromCtx(ctx context.Context) *conn {
	ws := khatru.GetConnection(ctx)
	if ws == nil {
		return nil
	}
	return e.connOf(ws)
}

// sync evaluates the connection's latest AUTH event (once per AUTH event): the sa-delegation tag
// binds the device key to an SA address, whose summed balance becomes the connection's total power.
// Anything that goes wrong leaves the connection anonymous: slower, never blocked, never earlier.
func (e *Engine) sync(ctx context.Context, ws *khatru.WebSocket, c *conn) {
	ev := ws.AuthEventSnapshot()
	if ev == nil {
		return
	}
	c.evalMu.Lock()
	defer c.evalMu.Unlock()
	c.mu.Lock()
	if c.authID == ev.ID {
		c.mu.Unlock()
		return
	}
	c.authID = ev.ID
	c.pubkey, c.addr, c.expires = ev.PubKey, "", 0
	c.mu.Unlock()
	c.total.Store(new(big.Int))

	addr, exp, err := e.delegation(ev.Tags, ev.PubKey, time.Now())
	if err != nil {
		e.log.Info("auth without usable delegation, serving at the zero-power tier", "device", short(ev.PubKey), "reason", err.Error())
		e.m.authResult("no_delegation").Add(1)
		return
	}
	c.mu.Lock()
	c.addr, c.expires = addr, exp
	c.mu.Unlock()
	e.m.authResult("delegated").Add(1)
	e.refresh(ctx, c)
}

// delegation verifies the sa-delegation tag of an event or AUTH signed by pubkey at now and refuses
// a device key its SA address has revoked (kind 9334) as of now.
func (e *Engine) delegation(tags nostr.Tags, pubkey string, now time.Time) (string, int64, error) {
	addr, exp, err := evmsig.Delegation(tags, pubkey, now.Unix())
	if err != nil {
		return "", 0, err
	}
	if e.dir.Revoked(addr, pubkey, now) {
		return "", 0, fmt.Errorf("device key %s was revoked by %s; ask the SA identity for a delegation to a new device key", short(pubkey), addr)
	}
	return addr, exp, nil
}

// ident is the connection's identity at now: c.identity, minus a delegation revoked since AUTH.
func (e *Engine) ident(c *conn, now time.Time) identity {
	id := c.identity(now)
	if id.addr != "" && e.dir.Revoked(id.addr, id.pubkey, now) {
		return identity{pubkey: id.pubkey, total: new(big.Int)}
	}
	return id
}

// refresh re-reads the summed balance for a connection's address. Only addresses that hold an
// allocation on this relay (or are a registered provider's) are looked up: anyone can mint an address
// and a self-signed delegation, and each lookup is a call to a public RPC endpoint.
func (e *Engine) refresh(ctx context.Context, c *conn) {
	c.mu.Lock()
	addr := c.addr
	c.mu.Unlock()
	if addr == "" {
		return
	}
	if !e.dir.Known(addr) {
		c.total.Store(new(big.Int))
		return
	}
	total, err := e.power.Total(ctx, addr)
	if err != nil {
		lvl := slog.LevelWarn
		if errors.Is(err, chains.ErrThrottled) {
			lvl = slog.LevelDebug // the cap or a back-off is already visible in metrics; do not log it every tick
		}
		e.log.Log(ctx, lvl, "balance lookup failed; using the conservative sum", "address", addr, "error", err.Error())
	}
	c.total.Store(total)
}
