package relay_test

import (
	"bytes"
	"context"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nbd-wtf/go-nostr"
	"github.com/nbd-wtf/go-nostr/nip44"

	"github.com/superalgos/sarelay/internal/protocol"
	"github.com/superalgos/sarelay/internal/state"
)

const secretMarker = "SECRET-MARKER-7f3a91c2"

func privateSignal(t *testing.T, provider actor, recipientPub string, seq int) *nostr.Event {
	t.Helper()
	plain := `{"v":2,"strategy_id":"alpha","seq":` + itoa(seq) + `,"instrument":"BINANCE:BTCUSDT","side":"buy","action":"open","size_fraction":0.5,"order_type":"market","timeframe":60,"created_ms":` +
		itoa(int(time.Now().UnixMilli())) + `,"bar_open_ms":` + itoa(int(time.Now().UnixMilli())-60_000) + `,"note":"` + secretMarker + `"}`
	ck, err := nip44.GenerateConversationKey(recipientPub, provider.sk)
	if err != nil {
		t.Fatal(err)
	}
	ct, err := nip44.Encrypt(plain, ck)
	if err != nil {
		t.Fatal(err)
	}
	ev := &nostr.Event{PubKey: provider.pub, CreatedAt: nostr.Now(), Kind: protocol.KindSignal, Tags: nostr.Tags{provider.delegation(time.Now().Add(time.Hour).Unix()), {"p", recipientPub}}, Content: ct}
	_ = ev.Sign(provider.sk)
	return ev
}

func itoa(n int) string { return tokens(n) }

// SEP-3.26 and SEP-3.27: private signals reach only entitled followers, as NIP-44 ciphertext they decrypt
// and verify; everyone else, and the relay's own storage, sees nothing readable.
func TestPrivateProviderDeliversCiphertextToEntitledOnly(t *testing.T) {
	dir := t.TempDir()
	r := startRelay(t, dir, noThrottle())
	prov := newActor()
	pubc := dial(t, r.URL)
	pubc.mustPublish(prov.privateRuleSet(1, 60, "1000"))

	bal := map[string]string{}
	split := map[string]uint32{prov.pub: 10000}
	fA, fB, fC, fD := newActor(), newActor(), newActor(), newActor()
	fundFollower(t, r, bal, fA, 1000, 1, split)                             // entitled
	fundFollower(t, r, bal, fB, 5000, 1, split)                             // entitled
	fundFollower(t, r, bal, fC, 1000, 1, map[string]uint32{prov.pub: 5000}) // 500 allocated: not entitled
	fundFollower(t, r, bal, fD, 0, 1, split)                                // no SA at all
	cA, cB, cC, cD, cE := dial(t, r.URL), dial(t, r.URL), dial(t, r.URL), dial(t, r.URL), dial(t, r.URL)
	cA.login(r.URL, fA)
	cB.login(r.URL, fB)
	cC.login(r.URL, fC)
	cD.login(r.URL, fD)
	cOwner := dial(t, r.URL)
	cOwner.login(r.URL, prov) // the provider's own SA identity
	all := map[string]*client{"A": cA, "B": cB, "C (500 of 1000 allocated)": cC, "D (zero SA)": cD, "E (anonymous)": cE, "owner": cOwner}
	for _, c := range all {
		c.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	}

	// the provider refuses to leak: plaintext, no recipient, two recipients
	plain := prov.signal(60, 1)
	if ok, why := pubc.publish(plain); ok || !strings.HasPrefix(why, "invalid:") {
		t.Errorf("plaintext signal from a private provider: ok=%v reason=%q", ok, why)
	}
	noP := privateSignal(t, prov, fA.pub, 2)
	noP.Tags = nostr.Tags{prov.delegation(time.Now().Add(time.Hour).Unix())}
	_ = noP.Sign(prov.sk)
	if ok, why := pubc.publish(noP); ok || !strings.Contains(why, "recipient") {
		t.Errorf("private signal without p tag: ok=%v reason=%q", ok, why)
	}
	twoP := privateSignal(t, prov, fA.pub, 3)
	twoP.Tags = nostr.Tags{prov.delegation(time.Now().Add(time.Hour).Unix()), {"p", fA.pub}, {"p", fB.pub}}
	_ = twoP.Sign(prov.sk)
	if ok, _ := pubc.publish(twoP); ok {
		t.Error("private signal addressed to two recipients accepted (NIP-44 is pairwise)")
	}

	toA, toB := privateSignal(t, prov, fA.pub, 10), privateSignal(t, prov, fB.pub, 11)
	pubc.mustPublish(toA)
	pubc.mustPublish(toB)
	time.Sleep(scaledDelay("2", 60) + 600*time.Millisecond) // well past any tier delay

	// entitled followers: exactly their own event, decryptable and verifiable
	for name, tc := range map[string]struct {
		c        *client
		a        actor
		mine, no *nostr.Event
	}{"A": {cA, fA, toA, toB}, "B": {cB, fB, toB, toA}} {
		if !tc.c.waitFor(tc.mine.ID, harnessWait) {
			t.Fatalf("%s never received its private signal", name)
		}
		if _, got := tc.c.firstArrival(tc.no.ID); got {
			t.Errorf("%s received the signal addressed to someone else", name)
		}
		got := tc.c.event(tc.mine.ID)
		if got == nil {
			t.Fatalf("%s: event body missing", name)
		}
		if ok, err := got.CheckSignature(); !ok || err != nil {
			t.Errorf("%s: received event does not verify: %v", name, err)
		}
		if strings.Contains(got.Content, secretMarker) {
			t.Errorf("%s: content on the wire is plaintext", name)
		}
		ck, _ := nip44.GenerateConversationKey(prov.pub, tc.a.sk)
		plain, err := nip44.Decrypt(got.Content, ck)
		if err != nil || !strings.Contains(plain, secretMarker) {
			t.Errorf("%s cannot decrypt its signal: %v", name, err)
		}
		if ids, _ := tc.c.query(map[string]any{"ids": []string{tc.no.ID}}); len(ids) != 0 {
			t.Errorf("%s fetched another follower's ciphertext by id", name)
		}
	}
	// the provider's own identity can read what it published (for archiving)
	if !cOwner.waitFor(toA.ID, harnessWait) || !cOwner.waitFor(toB.ID, harnessWait) {
		t.Error("provider's own identity does not see its events")
	}

	// non-entitled clients: nothing by any route
	probes := []map[string]any{
		{"kinds": []int{protocol.KindSignal}},
		{"ids": []string{toA.ID, toB.ID}},
		{"#p": []string{fA.pub}},
		{"authors": []string{prov.pub}, "kinds": []int{protocol.KindSignal}},
		{"since": int64(toA.CreatedAt) - 10},
	}
	leaks := 0
	for _, name := range []string{"C (500 of 1000 allocated)", "D (zero SA)", "E (anonymous)"} {
		c := all[name]
		for _, id := range []string{toA.ID, toB.ID} {
			if _, got := c.firstArrival(id); got {
				leaks++
				t.Errorf("%s received a private event live", name)
			}
		}
		for _, f := range probes {
			ids, _ := c.query(f)
			for _, id := range ids {
				if id == toA.ID || id == toB.ID {
					leaks++
					t.Errorf("%s read a private event via %v", name, f)
				}
			}
			if n, _ := c.count(f); n > 0 && f["kinds"] != nil {
				leaks++
				t.Errorf("%s COUNT %v = %d", name, f, n)
			}
		}
	}
	// the entitled follower B cannot count A's event either (COUNT with ids)
	if n, _ := cB.count(map[string]any{"ids": []string{toA.ID}}); n != 0 {
		leaks++
		t.Errorf("B counted A's private event")
	}
	t.Logf("private provider: 2 ciphertext events, 6 clients; leaks to non-entitled/other recipients: %d", leaks)

	// relay storage: only ciphertext, through the store API and in the raw files
	r.Stop()
	st, err := state.Open(filepath.Join(dir, "data"))
	if err != nil {
		t.Fatal(err)
	}
	ch, err := st.QueryEvents(context.Background(), nostr.Filter{Kinds: []int{protocol.KindSignal}})
	if err != nil {
		t.Fatal(err)
	}
	stored := 0
	for ev := range ch {
		stored++
		if strings.Contains(ev.Content, secretMarker) || strings.Contains(ev.Content, "BTCUSDT") {
			t.Errorf("stored content of %s is readable", ev.ID[:8])
		}
		if !protocol.LooksLikeNIP44(ev.Content) {
			t.Errorf("stored content of %s is not a NIP-44 payload", ev.ID[:8])
		}
	}
	st.Close()
	if stored != 2 {
		t.Errorf("store holds %d signals, want 2", stored)
	}
	found := 0
	_ = filepath.WalkDir(filepath.Join(dir, "data"), func(p string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			if b, e := os.ReadFile(p); e == nil && (bytes.Contains(b, []byte(secretMarker)) || bytes.Contains(b, []byte("BTCUSDT"))) {
				found++
				t.Errorf("plaintext marker found in storage file %s", p)
			}
		}
		return nil
	})
	t.Logf("storage: %d signal events read back, all NIP-44 ciphertext; plaintext found in %d raw storage files", stored, found)
}

// Private providers' commitments: entitled followers see them at once, others only after the
// documented delay; the commitment reveals no content.
func TestPrivateCommitmentVisibility(t *testing.T) {
	r := startRelay(t, "", noThrottle())
	prov := newActor()
	pubc := dial(t, r.URL)
	pubc.mustPublish(prov.privateRuleSet(1, 60, "1000"))
	bal := map[string]string{}
	f, other := newActor(), newActor()
	fundFollower(t, r, bal, f, 1000, 1, map[string]uint32{prov.pub: 10000})
	cf, co := dial(t, r.URL), dial(t, r.URL)
	cf.login(r.URL, f)
	co.login(r.URL, other)
	for _, c := range []*client{cf, co} {
		c.live(map[string]any{"kinds": []int{protocol.KindCommitment}})
	}
	ev := prov.commitment(1, nil)
	t0 := time.Now()
	pubc.mustPublish(ev)
	if !cf.waitFor(ev.ID, harnessWait) {
		t.Fatal("entitled follower missed the commitment")
	}
	if !co.waitFor(ev.ID, harnessWait) {
		t.Fatal("public never sees the commitment")
	}
	at, _ := co.firstArrival(ev.ID)
	if d := at.Sub(t0); d < scaledDelay("1", 60)-2*time.Millisecond {
		t.Errorf("non-entitled client saw the commitment after %s, before the configured delay %s", d, scaledDelay("1", 60))
	}
}
