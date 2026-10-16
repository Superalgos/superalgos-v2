package relay

import (
	"sync"
	"time"
)

// bucket is a token bucket. allow takes one token or reports how long until one is available.
type bucket struct {
	mu     sync.Mutex
	tokens float64
	last   time.Time
}

func (b *bucket) allow(now time.Time, perSec, burst float64) (bool, time.Duration) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.last.IsZero() {
		b.tokens, b.last = burst, now
	}
	b.tokens += now.Sub(b.last).Seconds() * perSec
	if b.tokens > burst {
		b.tokens = burst
	}
	b.last = now
	if b.tokens >= 1 {
		b.tokens--
		return true, 0
	}
	return false, time.Duration((1 - b.tokens) / perSec * float64(time.Second))
}

// spend takes n tokens at once for work already done. The balance may go negative (down to -burst), so
// the next request waits until it has recovered.
func (b *bucket) spend(now time.Time, perSec, burst, n float64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.last.IsZero() {
		b.tokens, b.last = burst, now
	}
	b.tokens += now.Sub(b.last).Seconds() * perSec
	if b.tokens > burst {
		b.tokens = burst
	}
	b.last = now
	b.tokens = max(b.tokens-n, -burst)
}
