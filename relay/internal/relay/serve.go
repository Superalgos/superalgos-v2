package relay

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// AdminToken resolves the bearer token that guards /healthz and /metrics (SEP-1.8: no endpoint
// answers 2xx without a valid session). Order: $SARELAY_ADMIN_TOKEN, the configured token file,
// then a random token generated once into <storage_path>/admin.token (mode 0600).
func AdminToken(tokenFile, storagePath string) (string, error) {
	if v := strings.TrimSpace(os.Getenv("SARELAY_ADMIN_TOKEN")); v != "" {
		if len(v) < 16 {
			return "", fmt.Errorf("SARELAY_ADMIN_TOKEN is %d characters; use at least 16", len(v))
		}
		return v, nil
	}
	path := tokenFile
	if path == "" {
		path = filepath.Join(storagePath, "admin.token")
	}
	if b, err := os.ReadFile(path); err == nil {
		if v := strings.TrimSpace(string(b)); len(v) >= 16 {
			return v, nil
		}
		return "", fmt.Errorf("admin token file %s holds fewer than 16 characters; delete it to generate a new one", path)
	}
	raw := make([]byte, 24)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	tok := hex.EncodeToString(raw)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", fmt.Errorf("cannot create %s for the admin token: %w", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(tok+"\n"), 0o600); err != nil {
		return "", fmt.Errorf("cannot write admin token to %s: %w", path, err)
	}
	return tok, nil
}

// AdminHandler serves GET /healthz and GET /metrics (Prometheus text), both behind a bearer token.
// There is no state-changing endpoint: all writes are signed Nostr events over the websocket.
func (e *Engine) AdminHandler(token string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if err := e.StoreHealthy(); err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "unhealthy", "error": err.Error()})
			return
		}
		p, a := e.Counts()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"status": "ok", "providers": p, "allocations": a,
			"pending_releases": e.Pending(), "connections": e.m.conns.Load(),
		})
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		e.m.reg.Write(w)
	})
	return bearer(token, readOnly(mux))
}

func readOnly(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			http.Error(w, "method not allowed: admin endpoints are read-only", http.StatusMethodNotAllowed)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func bearer(token string, next http.Handler) http.Handler {
	want := []byte("Bearer " + token)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got := []byte(r.Header.Get("Authorization"))
		if subtle.ConstantTimeCompare(got, want) != 1 {
			w.Header().Set("WWW-Authenticate", `Bearer realm="sarelay-admin"`)
			http.Error(w, "unauthorized: send Authorization: Bearer <admin token>", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// Serve runs the relay (and, if adminLn is non-nil, the admin endpoints) until ctx ends.
func (e *Engine) Serve(ctx context.Context, relayLn, adminLn net.Listener, adminToken string) error {
	relaySrv := &http.Server{Handler: e.Handler(), ReadHeaderTimeout: 10 * time.Second}
	errc := make(chan error, 2)
	go func() { errc <- relaySrv.Serve(relayLn) }()
	var adminSrv *http.Server
	if adminLn != nil {
		adminSrv = &http.Server{Handler: e.AdminHandler(adminToken), ReadHeaderTimeout: 10 * time.Second}
		go func() { errc <- adminSrv.Serve(adminLn) }()
	}
	select {
	case <-ctx.Done():
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			ctx = context.Background()
			defer func() { _ = relaySrv.Close() }()
			return fmt.Errorf("server stopped: %w", err)
		}
	}
	sctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = relaySrv.Shutdown(sctx)
	_ = relaySrv.Close() // websockets are hijacked; Shutdown does not wait for them
	if adminSrv != nil {
		_ = adminSrv.Shutdown(sctx)
	}
	return nil
}
