package relay

import (
	"sync"
	"testing"
	"time"
)

// TestLaneFiresInReceiptOrder: items pushed out of order, with the same release moment, run in receipt (seq)
// order, and one at a time even when several drains are triggered at once.
func TestLaneFiresInReceiptOrder(t *testing.T) {
	e := &Engine{timers: map[uint64]*time.Timer{}, lanes: map[laneKey]*lane{}}
	l := e.laneFor(laneKey{"p", 0})
	at := time.Now()
	var mu sync.Mutex
	var order []uint64
	running := 0
	for _, seq := range []uint64{3, 1, 4, 2, 5} {
		l.push(laneItem{seq: seq, at: at, run: func() {
			mu.Lock()
			running++
			if running != 1 {
				t.Errorf("two releases of one lane ran at the same time")
			}
			mu.Unlock()
			time.Sleep(time.Millisecond)
			mu.Lock()
			order = append(order, seq)
			running--
			mu.Unlock()
		}})
	}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); e.drain(l) }()
	}
	wg.Wait()
	deadline := time.Now().Add(laneWait)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(order)
		mu.Unlock()
		if n == 5 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	for i, seq := range order {
		if seq != uint64(i+1) {
			t.Fatalf("lane ran %v, want 1..5 in receipt order", order)
		}
	}
	if len(order) != 5 {
		t.Fatalf("lane ran %d/5 items: %v", len(order), order)
	}
}

// TestLaneHoldsFutureItems: an item whose release is in the future is not run early, and is run by the timer.
func TestLaneHoldsFutureItems(t *testing.T) {
	e := &Engine{timers: map[uint64]*time.Timer{}, lanes: map[laneKey]*lane{}}
	l := e.laneFor(laneKey{"p", time.Second})
	fired := make(chan time.Time, 1)
	at := time.Now().Add(80 * time.Millisecond)
	l.push(laneItem{seq: 1, at: at, run: func() { fired <- time.Now() }})
	e.drain(l)
	select {
	case <-fired:
		t.Fatal("released before its time")
	default:
	}
	select {
	case got := <-fired:
		if got.Before(at) {
			t.Fatalf("fired %s early", at.Sub(got))
		}
	case <-time.After(laneWait):
		t.Fatal("the timer armed by drain never fired the item")
	}
}

// laneWait bounds how long these tests wait for a timer or goroutine; it is not a product requirement, so
// it is generous enough for a loaded CI runner and fires only on a real hang.
const laneWait = 30 * time.Second
