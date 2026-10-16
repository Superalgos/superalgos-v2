// Package metrics is a tiny Prometheus text-format registry (counters, gauges, one histogram).
package metrics

import (
	"fmt"
	"io"
	"math"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
)

// Registry holds named metrics.
type Registry struct {
	mu       sync.Mutex
	counters map[string]*atomic.Int64 // key: name{labels}
	help     map[string]string
	typ      map[string]string
	gauges   map[string]func() float64
	hists    map[string]*Histogram
}

// New returns an empty registry.
func New() *Registry {
	return &Registry{counters: map[string]*atomic.Int64{}, help: map[string]string{}, typ: map[string]string{},
		gauges: map[string]func() float64{}, hists: map[string]*Histogram{}}
}

func key(name string, labels []string) string {
	if len(labels) == 0 {
		return name
	}
	parts := make([]string, 0, len(labels)/2)
	for i := 0; i+1 < len(labels); i += 2 {
		parts = append(parts, fmt.Sprintf(`%s=%q`, labels[i], labels[i+1]))
	}
	return name + "{" + strings.Join(parts, ",") + "}"
}

// Counter returns (creating) the counter for name with label pairs k1,v1,k2,v2.
func (r *Registry) Counter(name, help string, labels ...string) *atomic.Int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	k := key(name, labels)
	c, ok := r.counters[k]
	if !ok {
		c = new(atomic.Int64)
		r.counters[k] = c
		r.help[name], r.typ[name] = help, "counter"
	}
	return c
}

// Gauge registers a gauge read at scrape time.
func (r *Registry) Gauge(name, help string, f func() float64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.gauges[name] = f
	r.help[name], r.typ[name] = help, "gauge"
}

// Histogram is a fixed-bucket histogram in seconds.
type Histogram struct {
	bounds []float64
	counts []atomic.Int64
	sum    atomic.Int64 // microseconds
	n      atomic.Int64
}

// Hist registers (or returns) a histogram with the given upper bounds.
func (r *Registry) Hist(name, help string, bounds []float64) *Histogram {
	r.mu.Lock()
	defer r.mu.Unlock()
	if h, ok := r.hists[name]; ok {
		return h
	}
	h := &Histogram{bounds: bounds, counts: make([]atomic.Int64, len(bounds)+1)}
	r.hists[name] = h
	r.help[name], r.typ[name] = help, "histogram"
	return h
}

// Observe records one value in seconds.
func (h *Histogram) Observe(sec float64) {
	i := sort.SearchFloat64s(h.bounds, sec)
	h.counts[i].Add(1)
	h.sum.Add(int64(math.Round(sec * 1e6)))
	h.n.Add(1)
}

// Write renders the Prometheus text exposition format.
func (r *Registry) Write(w io.Writer) {
	r.mu.Lock()
	defer r.mu.Unlock()
	names := map[string]bool{}
	for k := range r.counters {
		names[strings.SplitN(k, "{", 2)[0]] = true
	}
	for k := range r.gauges {
		names[k] = true
	}
	for k := range r.hists {
		names[k] = true
	}
	sorted := make([]string, 0, len(names))
	for n := range names {
		sorted = append(sorted, n)
	}
	sort.Strings(sorted)
	for _, n := range sorted {
		fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s %s\n", n, r.help[n], n, r.typ[n])
		switch r.typ[n] {
		case "counter":
			var ks []string
			for k := range r.counters {
				if k == n || strings.HasPrefix(k, n+"{") {
					ks = append(ks, k)
				}
			}
			sort.Strings(ks)
			for _, k := range ks {
				fmt.Fprintf(w, "%s %d\n", k, r.counters[k].Load())
			}
		case "gauge":
			fmt.Fprintf(w, "%s %g\n", n, r.gauges[n]())
		case "histogram":
			h := r.hists[n]
			var cum int64
			for i, b := range h.bounds {
				cum += h.counts[i].Load()
				fmt.Fprintf(w, "%s_bucket{le=\"%g\"} %d\n", n, b, cum)
			}
			cum += h.counts[len(h.bounds)].Load()
			fmt.Fprintf(w, "%s_bucket{le=\"+Inf\"} %d\n%s_sum %g\n%s_count %d\n", n, cum, n, float64(h.sum.Load())/1e6, n, h.n.Load())
		}
	}
}
