package relay

import (
	"sync/atomic"

	"github.com/superalgos/sarelay/internal/metrics"
)

type engineMetrics struct {
	reg        *metrics.Registry
	deliveries *atomic.Int64
	releaseLag *metrics.Histogram
	conns      atomic.Int64
	armed      atomic.Int64
}

func newEngineMetrics() *engineMetrics {
	r := metrics.New()
	m := &engineMetrics{reg: r}
	m.deliveries = r.Counter("sarelay_deliveries_total", "Live signal deliveries written to subscribers.")
	m.releaseLag = r.Hist("sarelay_release_lag_seconds", "How late a scheduled release fired versus its release time.",
		[]float64{0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1})
	r.Gauge("sarelay_connections", "Open websocket connections.", func() float64 { return float64(m.conns.Load()) })
	r.Gauge("sarelay_pending_releases", "Scheduled releases not yet fired.", func() float64 { return float64(m.armed.Load()) })
	return m
}

func (m *engineMetrics) event(kind string, result string) *atomic.Int64 {
	return m.reg.Counter("sarelay_events_total", "Published events by kind and result.", "kind", kind, "result", result)
}

func (m *engineMetrics) rateLimited(op string) *atomic.Int64 {
	return m.reg.Counter("sarelay_rate_limited_total", "Requests answered with a rate-limited reply.", "op", op)
}

func (m *engineMetrics) authResult(result string) *atomic.Int64 {
	return m.reg.Counter("sarelay_auth_total", "NIP-42 AUTH evaluations by outcome.", "result", result)
}

func (m *engineMetrics) capped(what string) *atomic.Int64 {
	return m.reg.Counter("sarelay_capped_total", "Requests or events refused by a resource cap.", "cap", what)
}

func (m *engineMetrics) providersRemoved(why string) *atomic.Int64 {
	return m.reg.Counter("sarelay_providers_removed_total", "Providers deregistered by the upkeep sweep.", "reason", why)
}
