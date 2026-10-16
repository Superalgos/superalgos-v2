package relay

import (
	"testing"
	"time"
)

func TestBucket(t *testing.T) {
	var b bucket
	now := time.Unix(100, 0)
	for i := 0; i < 5; i++ {
		if ok, _ := b.allow(now, 2, 5); !ok {
			t.Fatalf("burst token %d refused", i)
		}
	}
	ok, wait := b.allow(now, 2, 5)
	if ok || wait != 500*time.Millisecond {
		t.Fatalf("sixth request: ok=%v wait=%s, want refused with 500ms", ok, wait)
	}
	if ok, _ := b.allow(now.Add(500*time.Millisecond), 2, 5); !ok {
		t.Error("refused after the advertised wait")
	}
	// refill never exceeds the burst
	later := now.Add(time.Hour)
	n := 0
	for ok, _ := b.allow(later, 2, 5); ok; ok, _ = b.allow(later, 2, 5) {
		n++
	}
	if n != 5 {
		t.Errorf("burst after a long idle: %d", n)
	}
}
