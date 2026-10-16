package relay

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/superalgos/sarelay/internal/config"
)

func testEngine(t *testing.T) *Engine {
	t.Helper()
	dir := t.TempDir()
	bal := filepath.Join(dir, "b.json")
	if err := os.WriteFile(bal, []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := config.Default()
	cfg.StoragePath = filepath.Join(dir, "data")
	cfg.Chains = []config.Chain{{ID: "f", Kind: "fixture", File: bal}}
	e, err := New(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(e.Close)
	return e
}

// M5: a request that examines many stored events is billed for the work, not just for arriving.
func TestScanWorkIsChargedToTheConnection(t *testing.T) {
	e := testEngine(t)
	perSec, burst := e.cfg.Rate.ZeroPowerPerSec, e.cfg.Rate.ZeroPowerBurst
	now := time.Now()

	cheap := &conn{}
	e.chargeScan(cheap, scanCostEvents-1) // under one block: free
	if ok, _ := cheap.read.allow(now, perSec, burst); !ok {
		t.Fatal("a tiny scan was charged")
	}

	heavy := &conn{}
	e.chargeScan(heavy, e.cfg.Limits.MaxScanEvents) // a full-cap scan costs far more than the zero-power burst
	ok, wait := heavy.read.allow(time.Now(), perSec, burst)
	if ok {
		t.Fatalf("a %d-event scan left the zero-power connection able to send another request at once", e.cfg.Limits.MaxScanEvents)
	}
	if wait < time.Second {
		t.Errorf("must wait %s after a full-cap scan, want at least 1s", wait)
	}
}

func TestBucketSpendNeverGoesBelowMinusBurst(t *testing.T) {
	var b bucket
	now := time.Unix(100, 0)
	b.spend(now, 1, 5, 1e9)
	if ok, wait := b.allow(now, 1, 5); ok || wait > 7*time.Second {
		t.Errorf("after a huge spend: ok=%v wait=%s, want refused and a bounded wait", ok, wait)
	}
}
