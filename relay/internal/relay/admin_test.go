package relay_test

import (
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/superalgos/sarelay/internal/protocol"
)

func adminDo(t *testing.T, method, url, token string) (int, string) {
	t.Helper()
	req, _ := http.NewRequest(method, url, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := (&http.Client{Timeout: 3 * time.Second}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

// SEP-1.8: every admin endpoint answers 401 without a valid token; nothing state-changing exists.
func TestAdminEndpointsRequireAuth(t *testing.T) {
	r := startRelay(t, "")
	for _, path := range []string{"/healthz", "/metrics", "/", "/nope"} {
		for _, tok := range []string{"", "wrong-token-0123456789"} {
			if code, _ := adminDo(t, "GET", r.AdminURL+path, tok); code != 401 {
				t.Errorf("GET %s with token %q: status %d, want 401", path, tok, code)
			}
		}
		for _, m := range []string{"POST", "PUT", "DELETE"} {
			if code, _ := adminDo(t, m, r.AdminURL+path, ""); code != 401 {
				t.Errorf("%s %s without token: status %d, want 401", m, path, code)
			}
			if code, _ := adminDo(t, m, r.AdminURL+path, r.Token); code != 405 {
				t.Errorf("%s %s with token: status %d, want 405 (read-only)", m, path, code)
			}
		}
	}
	if code, body := adminDo(t, "GET", r.AdminURL+"/healthz", r.Token); code != 200 || !strings.Contains(body, `"status":"ok"`) {
		t.Errorf("healthz with token: %d %s", code, body)
	}
}

func TestMetricsExposePrometheusText(t *testing.T) {
	r := startRelay(t, "")
	prov := registerProvider(t, r, 60, defaultRules)
	c := dial(t, r.URL)
	c.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	ev := prov.signal(60, 1)
	c.mustPublish(ev)
	c.waitFor(ev.ID, 4*time.Second)
	c.publish(signalBy(newActor().sk, nil, 60, 2, time.Now().UnixMilli())) // rejected

	code, body := adminDo(t, "GET", r.AdminURL+"/metrics", r.Token)
	if code != 200 {
		t.Fatalf("status %d", code)
	}
	for _, want := range []string{
		"# TYPE sarelay_connections gauge", "sarelay_providers 1",
		`sarelay_events_total{kind="signal",result="accepted"} 1`, `sarelay_events_total{kind="signal",result="rejected"} 1`,
		"sarelay_deliveries_total ", "sarelay_release_lag_seconds_bucket{le=\"+Inf\"} ", "sarelay_pending_releases ",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("metrics lack %q\n%s", want, body)
		}
	}
}
