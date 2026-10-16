package metrics

import (
	"strings"
	"testing"
)

func TestWriteFormat(t *testing.T) {
	r := New()
	r.Counter("req_total", "Requests.", "kind", "a").Add(2)
	r.Counter("req_total", "Requests.", "kind", "b").Add(1)
	r.Counter("req_total", "Requests.", "kind", "a").Add(1)
	r.Gauge("up", "Up.", func() float64 { return 1 })
	h := r.Hist("lag_seconds", "Lag.", []float64{0.01, 0.1})
	h.Observe(0.005)
	h.Observe(0.05)
	h.Observe(5)
	var b strings.Builder
	r.Write(&b)
	out := b.String()
	for _, want := range []string{
		"# HELP req_total Requests.\n# TYPE req_total counter\n", `req_total{kind="a"} 3`, `req_total{kind="b"} 1`,
		"# TYPE up gauge\nup 1\n", "# TYPE lag_seconds histogram\n", `lag_seconds_bucket{le="0.01"} 1`, `lag_seconds_bucket{le="0.1"} 2`,
		`lag_seconds_bucket{le="+Inf"} 3`, "lag_seconds_count 3",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in\n%s", want, out)
		}
	}
	if strings.Count(out, "# TYPE req_total") != 1 {
		t.Error("TYPE line repeated")
	}
}
