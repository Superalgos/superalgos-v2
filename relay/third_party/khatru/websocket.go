package khatru

import (
	"context"
	"net/http"
	"sync"

	"github.com/fasthttp/websocket"
	"github.com/nbd-wtf/go-nostr"
	"github.com/puzpuzpuz/xsync/v3"
)

type WebSocket struct {
	conn  *websocket.Conn
	mutex sync.Mutex

	// original request
	Request *http.Request

	// this Context will be canceled whenever the connection is closed from the client side or server-side.
	Context context.Context
	cancel  context.CancelFunc

	// nip42
	Challenge       string
	AuthedPublicKey string
	// authEvent is the validated kind-22242 event, read through AuthEventSnapshot.
	// (Local patch: stock khatru discards the event after checking it.)
	authEvent *nostr.Event
	Authed    chan struct{}

	// nip77
	negentropySessions *xsync.MapOf[string, *NegentropySession]

	authLock sync.Mutex
}

func (ws *WebSocket) WriteJSON(any any) error {
	ws.mutex.Lock()
	err := ws.conn.WriteJSON(any)
	ws.mutex.Unlock()
	return err
}

func (ws *WebSocket) WriteMessage(t int, b []byte) error {
	ws.mutex.Lock()
	err := ws.conn.WriteMessage(t, b)
	ws.mutex.Unlock()
	return err
}

// AuthEventSnapshot returns the last validated NIP-42 AUTH event of this connection, or nil.
// (Local patch.) Policies read extra AUTH tags from it, e.g. the sa-delegation tag.
func (ws *WebSocket) AuthEventSnapshot() *nostr.Event {
	ws.authLock.Lock()
	defer ws.authLock.Unlock()
	return ws.authEvent
}
