package relay_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/dgraph-io/badger/v4"
	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/config"
	"github.com/superalgos/sarelay/internal/evmsig/evmsigtest"
	"github.com/superalgos/sarelay/internal/protocol"
	"github.com/superalgos/sarelay/internal/relay"
	"github.com/superalgos/sarelay/internal/state"
)

// Regression tests for the independent security review of the Go relay. Each names its finding.

const kindRevocation = 9334

// ---- helpers ----

// stranger publishes through a fresh key: anyone can run one.
func strangerSign(ev *nostr.Event) *nostr.Event {
	sk := nostr.GeneratePrivateKey()
	ev.PubKey, _ = nostr.GetPublicKey(sk)
	ev.ID = ""
	_ = ev.Sign(sk)
	return ev
}

func (a actor) delegationExpiry() int64 { return time.Now().Add(time.Hour).Unix() }

// signed builds an event from this actor's device key with its delegation tag.
func (a actor) signed(kind int, content string, extra ...nostr.Tag) *nostr.Event {
	tags := append(nostr.Tags{a.delegation(a.delegationExpiry())}, extra...)
	ev := &nostr.Event{PubKey: a.pub, CreatedAt: nostr.Now(), Kind: kind, Tags: tags, Content: content}
	_ = ev.Sign(a.sk)
	return ev
}

// anchor builds a kind-1040 attestation for target, signed by this actor's device key.
func (a actor) anchor(target string) *nostr.Event {
	ev := &nostr.Event{PubKey: a.pub, CreatedAt: nostr.Now(), Kind: protocol.KindAnchor, Tags: nostr.Tags{{"e", target}, {"sa-anchor", "ots"}}, Content: "proof"}
	_ = ev.Sign(a.sk)
	return ev
}

func (a actor) govDelegation(nonce int, to string) *nostr.Event {
	msg := fmt.Sprintf("sa-govdelegation:%s:%d:%s=1000", a.addr, nonce, to)
	c, _ := json.Marshal(map[string]any{"v": 1, "address": a.addr, "nonce": nonce, "to": map[string]int{to: 1000}, "sig": evmsigtest.Sign(a.evm, msg)})
	ev := &nostr.Event{PubKey: a.pub, CreatedAt: nostr.Now(), Kind: protocol.KindGovDeleg, Content: string(c)}
	_ = ev.Sign(a.sk)
	return ev
}

// revocation is the kind-9334 event revoking devicePub under this SA address, carried by a stranger.
func (a actor) revocation(devicePub string, effectiveAt int64) *nostr.Event {
	sig := evmsigtest.Sign(a.evm, fmt.Sprintf("sa-revocation:%s:%d", devicePub, effectiveAt))
	c, _ := json.Marshal(map[string]any{"v": 1, "device_pubkey": devicePub, "effective_at": effectiveAt, "address": a.addr, "sig": sig})
	return strangerSign(&nostr.Event{CreatedAt: nostr.Now(), Kind: kindRevocation, Content: string(c)})
}

func metric(t *testing.T, r *testRelay, name string) float64 {
	t.Helper()
	code, body := adminDo(t, "GET", r.AdminURL+"/metrics", r.Token)
	if code != 200 {
		t.Fatalf("metrics status %d", code)
	}
	m := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(name) + ` ([0-9.e+-]+)$`).FindStringSubmatch(body)
	if m == nil {
		t.Fatalf("metric %s missing from\n%s", name, body)
	}
	v, _ := strconv.ParseFloat(m[1], 64)
	return v
}

func wantRefused(t *testing.T, what string, ok bool, why, prefix string) {
	t.Helper()
	if ok {
		t.Errorf("%s: accepted, want refusal starting %q", what, prefix)
		return
	}
	if !strings.HasPrefix(why, prefix) {
		t.Errorf("%s: reason %q should start with %q", what, why, prefix)
	}
}

// ---- H1: allocation equivocation by an unauthenticated wrapper ----

func TestH1AllocationWithoutDelegationTagIsRefused(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	f := newActor()
	ev := f.allocation(1, 0, map[string]uint32{strings.Repeat("a", 64): 1000})
	ev.Tags = nil // the EIP-191 signature in the content is valid; only the wrapper lost its tag
	_ = ev.Sign(f.sk)
	ok, why := c.publish(ev)
	wantRefused(t, "allocation without sa-delegation", ok, why, "invalid:")
	if ok, why := c.publish(f.allocation(1, 0, map[string]uint32{strings.Repeat("a", 64): 1000})); !ok {
		t.Errorf("the same allocation with its tag was refused: %s", why)
	}
}

func TestH1AllocationDelegationNamingAnotherAddressIsRefused(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	victim, other := newActor(), newActor()
	ev := victim.allocation(1, 0, map[string]uint32{strings.Repeat("a", 64): 1000})
	// the wrapper key carries a perfectly valid delegation, but from the wrong SA address
	ev.Tags = nostr.Tags{evmsigtest.SignDelegation(other.evm, other.pub, time.Now().Add(time.Hour).Unix())}
	ev.PubKey = other.pub
	_ = ev.Sign(other.sk)
	ok, why := c.publish(ev)
	wantRefused(t, "delegation from another address", ok, why, "invalid:")
}

func TestH1StrangerCannotRewrapVictimAllocation(t *testing.T) {
	r := startRelay(t, "", withMinAllocInterval(0))
	c := dial(t, r.URL)
	victim := newActor()
	prov := strings.Repeat("a", 64)
	orig := victim.allocation(1, 0, map[string]uint32{prov: 1000})
	c.mustPublish(orig)
	// a stranger re-wraps the public content (same nonce, same sig) in an event of its own
	wrapped := &nostr.Event{CreatedAt: nostr.Now(), Kind: protocol.KindAllocation, Content: orig.Content}
	ok, why := c.publish(strangerSign(wrapped))
	wantRefused(t, "stranger re-wrap", ok, why, "invalid:")
	ids, _ := c.query(map[string]any{"kinds": []int{protocol.KindAllocation}})
	if len(ids) != 1 || ids[0] != orig.ID {
		t.Errorf("allocation history = %v, want only the victim's own event %s", ids, orig.ID[:12])
	}
}

func TestH1VictimCanReallocateAfterStrangerAttempts(t *testing.T) {
	const gap = 1500 * time.Millisecond
	r := startRelay(t, "", withMinAllocInterval(gap))
	c := dial(t, r.URL)
	victim := newActor()
	prov := strings.Repeat("a", 64)
	orig := victim.allocation(1, 0, map[string]uint32{prov: 1000})
	c.mustPublish(orig)
	time.Sleep(gap + 100*time.Millisecond)
	// the interval has passed; the stranger's attempts must not restart it
	for i := 0; i < 3; i++ {
		c.publish(strangerSign(&nostr.Event{CreatedAt: nostr.Now(), Kind: protocol.KindAllocation, Content: orig.Content}))
	}
	if ok, why := c.publish(victim.allocation(2, 0, map[string]uint32{prov: 2000})); !ok {
		t.Fatalf("the victim was rate-limited by a stranger's re-wrap: %s", why)
	}
}

func TestH1SameSignedContentFromADelegatedDeviceIsADuplicate(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	f := newActor()
	orig := f.allocation(1, 0, map[string]uint32{strings.Repeat("a", 64): 1000})
	c.mustPublish(orig)
	dev, devPub := f.device()
	again := &nostr.Event{PubKey: devPub, CreatedAt: nostr.Now(), Kind: protocol.KindAllocation, Content: orig.Content,
		Tags: nostr.Tags{evmsigDelegation(f, devPub, time.Now().Add(time.Hour).Unix())}}
	_ = again.Sign(dev)
	ok, why := c.publish(again)
	wantRefused(t, "same content, second device", ok, why, "duplicate:")
}

// ---- H2: quotas ----

func TestH2ReleaseDelayIsCapped(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	p, q := newActor(), newActor()
	// 8 x one day = 8 days, above the default 7-day max_release_delay (and below max_delay_fraction 100)
	ok, why := c.publish(p.ruleSet(1, 86400, []tierSpec{{"0", "8"}}))
	wantRefused(t, "8 day release delay", ok, why, "invalid:")
	if !strings.Contains(why, "max_release_delay") {
		t.Errorf("reason %q should name max_release_delay", why)
	}
	if ok, why := c.publish(q.ruleSet(1, 86400, []tierSpec{{"0", "7"}})); !ok {
		t.Errorf("exactly 7 days must be accepted: %s", why)
	}
}

func TestH2PendingCapPerProvider(t *testing.T) {
	r := startRelay(t, "", withPendingCaps(3, 100))
	c := dial(t, r.URL)
	a := registerProvider(t, r, 60, defaultRules)
	b := registerProvider(t, r, 60, defaultRules)
	for i := 1; i <= 3; i++ {
		c.mustPublish(a.signal(60, i))
	}
	ok, why := c.publish(a.signal(60, 4))
	wantRefused(t, "4th pending signal", ok, why, "blocked:")
	if !strings.Contains(why, "max_pending_per_provider") {
		t.Errorf("reason %q should name the config key", why)
	}
	if ok, why := c.publish(b.signal(60, 1)); !ok {
		t.Errorf("another provider must not be affected: %s", why)
	}
	// releases finish (2 s delay + 2 s grace at the test scale), then room returns
	deadline := time.Now().Add(8 * time.Second)
	for {
		if ok, _ := c.publish(a.signal(60, 5)); ok {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the pending slots were never freed")
		}
		time.Sleep(200 * time.Millisecond)
	}
}

func TestH2PendingCapTotal(t *testing.T) {
	r := startRelay(t, "", withPendingCaps(100, 3))
	c := dial(t, r.URL)
	a := registerProvider(t, r, 60, defaultRules)
	b := registerProvider(t, r, 60, defaultRules)
	c.mustPublish(a.signal(60, 1))
	c.mustPublish(a.signal(60, 2))
	c.mustPublish(b.signal(60, 1))
	ok, why := c.publish(b.signal(60, 2))
	wantRefused(t, "global pending cap", ok, why, "blocked:")
	if !strings.Contains(why, "max_pending_total") {
		t.Errorf("reason %q should name the config key", why)
	}
}

func TestH2StorageBudgetPerKind(t *testing.T) {
	r := startRelay(t, "", withBudgets(2, 2, 2, 2, 2))
	c := dial(t, r.URL)
	prov := registerProvider(t, r, 60, defaultRules)
	sig := prov.signal(60, 1)
	c.mustPublish(sig)
	a := newActor()
	mk := map[int]func(i int) *nostr.Event{
		protocol.KindAnchor: func(i int) *nostr.Event {
			e := prov.anchor(sig.ID)
			e.Content = fmt.Sprint("proof", i)
			_ = e.Sign(prov.sk)
			return e
		},
		protocol.KindProposal: func(i int) *nostr.Event { return a.signed(protocol.KindProposal, fmt.Sprintf(`{"n":%d}`, i)) },
		protocol.KindVote:     func(i int) *nostr.Event { return a.signed(protocol.KindVote, fmt.Sprintf(`{"n":%d}`, i)) },
		protocol.KindGovDeleg: func(i int) *nostr.Event { return a.govDelegation(i, newActor().addr) },
		kindRevocation:        func(i int) *nostr.Event { return newActor().revocation(newActor().pub, time.Now().Unix()+int64(i)) },
	}
	for kind, build := range mk {
		for i := 1; i <= 2; i++ {
			if ok, why := c.publish(build(i)); !ok {
				t.Fatalf("kind %d event %d refused within budget: %s", kind, i, why)
			}
		}
		ok, why := c.publish(build(3))
		wantRefused(t, fmt.Sprintf("kind %d over budget", kind), ok, why, "blocked:")
		if !strings.Contains(why, "budget") {
			t.Errorf("kind %d: reason %q should mention the budget", kind, why)
		}
	}
}

func TestH2AnchorMustNameAStoredEvent(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	prov := registerProvider(t, r, 60, defaultRules)
	sig := prov.signal(60, 1)
	c.mustPublish(sig)
	ok, why := c.publish(prov.anchor(strings.Repeat("ab", 32)))
	wantRefused(t, "1040 naming an unknown event", ok, why, "invalid:")
	noE := prov.anchor(sig.ID)
	noE.Tags = nostr.Tags{{"sa-anchor", "ots"}}
	_ = noE.Sign(prov.sk)
	ok, why = c.publish(noE)
	wantRefused(t, "1040 without an e tag", ok, why, "invalid:")
	if ok, why := c.publish(prov.anchor(sig.ID)); !ok {
		t.Errorf("1040 naming a stored event refused: %s", why)
	}
}

func TestH2RetentionFreesBudget(t *testing.T) {
	r := startRelay(t, "", withBudgets(2, 2, 2, 2, 2), withRetention(time.Hour))
	c := dial(t, r.URL)
	prov := registerProvider(t, r, 60, defaultRules)
	sig := prov.signal(60, 1)
	c.mustPublish(sig)
	for i := 1; i <= 2; i++ {
		e := prov.anchor(sig.ID)
		e.Content = fmt.Sprint("p", i)
		_ = e.Sign(prov.sk)
		c.mustPublish(e)
	}
	if ok, _ := c.publish(prov.anchor(sig.ID)); ok {
		t.Fatal("budget not enforced")
	}
	if n := r.sweepRetention(time.Now()); n != 0 {
		t.Errorf("fresh events swept: %d", n)
	}
	if n := r.sweepRetention(time.Now().Add(2 * time.Hour)); n != 2 {
		t.Fatalf("swept %d old attestations, want 2", n)
	}
	if ids, _ := c.query(map[string]any{"kinds": []int{protocol.KindAnchor}}); len(ids) != 0 {
		t.Errorf("%d attestations survived retention", len(ids))
	}
	if ok, why := c.publish(prov.anchor(sig.ID)); !ok {
		t.Errorf("budget not freed by the sweep: %s", why)
	}
}

func TestH2ProviderRegistrationNeedsPower(t *testing.T) {
	r := startRelay(t, "", withMinProviderPower(1000))
	c := dial(t, r.URL)
	p := newActor()
	ok, why := c.publish(p.ruleSet(1, 60, defaultRules))
	wantRefused(t, "rule set from an address holding nothing", ok, why, "blocked:")
	r.SetBalances(map[string]string{p.addr: "999"})
	time.Sleep(2200 * time.Millisecond) // oracle cache_ttl in tests
	ok, why = c.publish(p.ruleSet(1, 60, defaultRules))
	wantRefused(t, "999 SA", ok, why, "blocked:")
	r.SetBalances(map[string]string{p.addr: "1000"})
	time.Sleep(2200 * time.Millisecond)
	if ok, why := c.publish(p.ruleSet(1, 60, defaultRules)); !ok {
		t.Errorf("1000 SA refused: %s", why)
	}
}

func TestH2ProviderPowerIsRecheckedAndOutagesRemoveNobody(t *testing.T) {
	r := startRelay(t, "", withMinProviderPower(1000))
	c := dial(t, r.URL)
	p := newActor()
	r.SetBalances(map[string]string{p.addr: "5000"})
	c.mustPublish(p.ruleSet(1, 60, defaultRules))
	// the chain read fails (unreadable fixture): nobody is removed on an error
	time.Sleep(2200 * time.Millisecond)
	if err := os.WriteFile(r.bal, []byte("not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if n := r.sweepProviders(time.Now()); n != 0 {
		t.Fatalf("a failing balance read removed %d providers", n)
	}
	// a clean read below the minimum removes the provider
	r.SetBalances(map[string]string{p.addr: "10"})
	time.Sleep(2200 * time.Millisecond)
	if n := r.sweepProviders(time.Now()); n != 1 {
		t.Fatalf("swept %d providers, want 1", n)
	}
	ok, why := c.publish(p.signal(60, 1))
	wantRefused(t, "signal from a removed provider", ok, why, "blocked:")
}

func TestH2IdleProvidersExpireAndFreeTheirSlot(t *testing.T) {
	r := startRelay(t, "", withMaxProviders(1), withIdleExpiry(time.Hour))
	c := dial(t, r.URL)
	a, b := newActor(), newActor()
	c.mustPublish(a.ruleSet(1, 60, defaultRules))
	c.mustPublish(a.signal(60, 1))
	ok, why := c.publish(b.ruleSet(1, 60, defaultRules))
	wantRefused(t, "second provider over max_providers", ok, why, "invalid:")
	if n := r.sweepProviders(time.Now().Add(30 * time.Minute)); n != 0 {
		t.Fatalf("a provider active 30 minutes ago was removed (%d)", n)
	}
	if n := r.sweepProviders(time.Now().Add(61 * time.Minute)); n != 1 {
		t.Fatalf("swept %d idle providers, want 1", n)
	}
	if ok, why := c.publish(b.ruleSet(1, 60, defaultRules)); !ok {
		t.Errorf("the freed slot was not usable: %s", why)
	}
}

func TestH2PerIPConnectionCap(t *testing.T) {
	r := startRelay(t, "", withMaxConnsPerIP(3))
	var conns []*websocket.Conn
	defer func() {
		for _, c := range conns {
			_ = c.CloseNow()
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for i := 0; i < 3; i++ {
		c, _, err := websocket.Dial(ctx, r.URL, nil)
		if err != nil {
			t.Fatalf("connection %d within the cap refused: %v", i+1, err)
		}
		conns = append(conns, c)
	}
	_, resp, err := websocket.Dial(ctx, r.URL, nil)
	if err == nil {
		t.Fatal("a 4th connection from one address was accepted")
	}
	if resp == nil || resp.StatusCode != http.StatusTooManyRequests {
		t.Errorf("4th connection: %v (response %v), want HTTP 429", err, resp)
	}
	_ = conns[0].CloseNow()
	deadline := time.Now().Add(5 * time.Second)
	for {
		c, _, err := websocket.Dial(ctx, r.URL, nil)
		if err == nil {
			conns = append(conns, c)
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("slot not freed after a close: %v", err)
		}
		time.Sleep(50 * time.Millisecond)
	}
	// NIP-11 over plain HTTP is not a websocket and is never counted
	for i := 0; i < 6; i++ {
		req, _ := http.NewRequest("GET", "http"+strings.TrimPrefix(r.URL, "ws"), nil)
		req.Header.Set("Accept", "application/nostr+json")
		resp, err := http.DefaultClient.Do(req)
		if err != nil || resp.StatusCode != 200 {
			t.Fatalf("NIP-11 request %d: %v %v", i, err, resp)
		}
		_ = resp.Body.Close()
	}
}

// ---- M1: oracle starvation ----

func TestM1SelfMadeIdentitiesCauseNoChainLookups(t *testing.T) {
	r := startRelay(t, "")
	for i := 0; i < 12; i++ {
		a := newActor() // a fresh address with a self-signed delegation, never allocated anything
		c := dial(t, r.URL)
		c.login(r.URL, a)
	}
	time.Sleep(1600 * time.Millisecond) // several recheck ticks
	if got := metric(t, r, "sarelay_oracle_lookups_total"); got != 0 {
		t.Fatalf("12 self-made identities caused %v chain lookups, want 0", got)
	}
	// control: an address that holds an allocation is looked up
	prov := registerProvider(t, r, 60, defaultRules)
	f := newActor()
	fundFollower(t, r, map[string]string{}, f, 1000, 1, map[string]uint32{prov.pub: 10000})
	c := dial(t, r.URL)
	c.login(r.URL, f)
	time.Sleep(800 * time.Millisecond)
	if got := metric(t, r, "sarelay_oracle_lookups_total"); got == 0 {
		t.Error("an allocated address was never looked up; the restriction is too tight")
	}
}

// ---- M2: proposals ----

func TestM2ProposalNeedsDelegationAndPower(t *testing.T) {
	r := startRelay(t, "", withMinProposerPower(1000))
	c := dial(t, r.URL)
	p := newActor()
	bare := &nostr.Event{PubKey: p.pub, CreatedAt: nostr.Now(), Kind: protocol.KindProposal, Content: `{"title":"x"}`}
	_ = bare.Sign(p.sk)
	ok, why := c.publish(bare)
	wantRefused(t, "proposal without delegation", ok, why, "invalid:")
	ok, why = c.publish(p.signed(protocol.KindProposal, `{"title":"x"}`))
	wantRefused(t, "proposal from an address with no SA", ok, why, "blocked:")
	r.SetBalances(map[string]string{p.addr: "1000"})
	time.Sleep(2200 * time.Millisecond)
	if ok, why := c.publish(p.signed(protocol.KindProposal, `{"title":"y"}`)); !ok {
		t.Errorf("proposal from 1000 SA refused: %s", why)
	}
}

// ---- M3: revocation ----

func TestM3RevocationRefusesPublishAndAuthUnderThatAddress(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	prov := registerProvider(t, r, 60, defaultRules)
	f := newActor()
	fundFollower(t, r, map[string]string{}, f, 1_000_000, 1, map[string]uint32{prov.pub: 10000})

	// baseline: the follower's device key earns the top tier (no delay) and the provider can publish
	first := dial(t, r.URL)
	first.login(r.URL, f)
	first.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	s1 := prov.signal(60, 1)
	t0 := time.Now()
	c.mustPublish(s1)
	if !first.waitFor(s1.ID, 4*time.Second) {
		t.Fatal("no delivery before revocation")
	}
	if at, _ := first.firstArrival(s1.ID); at.Sub(t0) > 700*time.Millisecond {
		t.Fatalf("baseline delivery took %s, the follower should be in the top tier", at.Sub(t0))
	}

	// the SA address revokes both device keys; a stranger carries the events
	if ok, why := c.publish(prov.revocation(prov.pub, time.Now().Unix()-1)); !ok {
		t.Fatalf("revocation of the provider's device refused: %s", why)
	}
	if ok, why := c.publish(f.revocation(f.pub, time.Now().Unix()-1)); !ok {
		t.Fatalf("revocation of the follower's device refused: %s", why)
	}
	ok, why := c.publish(prov.signal(60, 2))
	wantRefused(t, "signal from a revoked device key", ok, why, "blocked:")

	// the connection that authenticated before the revocation loses its power at once, and a new AUTH
	// with the revoked device earns nothing: the signal reaches it only at the zero-power delay
	second := dial(t, r.URL)
	second.login(r.URL, f)
	second.live(map[string]any{"kinds": []int{protocol.KindSignal}})
	// a fresh provider device (delegated, not revoked) publishes
	dev, devPub := prov.device()
	s3 := signalBy(dev, nostr.Tags{evmsigDelegation(prov, devPub, time.Now().Add(time.Hour).Unix())}, 60, 3, time.Now().UnixMilli())
	t1 := time.Now()
	c.mustPublish(s3)
	if !second.waitFor(s3.ID, 6*time.Second) || !first.waitFor(s3.ID, 6*time.Second) {
		t.Fatal("signal from a fresh provider device never delivered")
	}
	for name, cl := range map[string]*client{"already connected": first, "re-authenticated": second} {
		at, _ := cl.firstArrival(s3.ID)
		if d := at.Sub(t1); d < 1500*time.Millisecond {
			t.Errorf("%s follower with a revoked device got the signal after %s; revocation must drop it to the 2 s zero-power tier", name, d)
		}
	}
}

func TestM3RevocationValidationAndPersistence(t *testing.T) {
	dir := t.TempDir()
	r := startRelay(t, dir)
	c := dial(t, r.URL)
	a, other := newActor(), newActor()
	dev := newActor().pub
	good := a.revocation(dev, 5000)
	if ok, why := c.publish(good); !ok {
		t.Fatalf("valid revocation refused: %s", why)
	}
	// signed by a different EVM key than the address it names
	forged := a.revocation(dev, 4000)
	var content map[string]any
	_ = json.Unmarshal([]byte(forged.Content), &content)
	content["sig"] = evmsigtest.Sign(other.evm, fmt.Sprintf("sa-revocation:%s:%d", dev, 4000))
	b, _ := json.Marshal(content)
	forged.Content = string(b)
	ok, why := c.publish(strangerSign(forged))
	wantRefused(t, "forged revocation", ok, why, "invalid:")
	// a later effective_at adds nothing
	ok, why = c.publish(a.revocation(dev, 6000))
	wantRefused(t, "later duplicate", ok, why, "duplicate:")
	// an earlier one tightens
	if ok, why := c.publish(a.revocation(dev, 3000)); !ok {
		t.Errorf("earlier effective_at refused: %s", why)
	}
	ids, _ := c.query(map[string]any{"kinds": []int{kindRevocation}})
	if len(ids) != 2 {
		t.Errorf("stored %d revocations, want 2", len(ids))
	}
	// persisted: after a restart a revoked provider device is still refused
	prov := newActor()
	c.mustPublish(prov.ruleSet(1, 60, defaultRules))
	c.mustPublish(prov.revocation(prov.pub, time.Now().Unix()-1))
	r.Stop()
	r2 := startRelay(t, dir)
	c2 := dial(t, r2.URL)
	ok, why = c2.publish(prov.signal(60, 1))
	wantRefused(t, "revoked device after restart", ok, why, "blocked:")
}

// ---- M5: full-store scans ----

func TestM5ScanIsCapped(t *testing.T) {
	r := startRelay(t, "", withMaxScan(25), noThrottle())
	c := dial(t, r.URL)
	prov := registerProvider(t, r, 60, []tierSpec{{"0", "0"}})
	for i := 1; i <= 60; i++ {
		c.mustPublish(prov.signal(60, i))
	}
	reader := dial(t, r.URL)
	reader.login(r.URL, prov) // the provider's own identity reads everything with no delay
	ids, closed := reader.query(map[string]any{"kinds": []int{protocol.KindSignal}})
	if closed != "" {
		t.Fatalf("closed: %s", closed)
	}
	if len(ids) == 0 || len(ids) > 25 {
		t.Errorf("a REQ examined %d events with max_scan_events = 25", len(ids))
	}
	if n, _ := reader.count(map[string]any{"kinds": []int{protocol.KindSignal}}); n > 25 {
		t.Errorf("COUNT examined %d events with max_scan_events = 25", n)
	}
}

// ---- M6: kind 1040 timing leak ----

func TestM6AnchorOfAGatedEventFollowsItsRelease(t *testing.T) {
	r := startRelay(t, "", noThrottle())
	pub := dial(t, r.URL)
	prov := registerProvider(t, r, 60, defaultRules) // a zero-power follower waits 2 s at the test scale
	zero := dial(t, r.URL)
	zeroLive := zero.live(map[string]any{"kinds": []int{protocol.KindAnchor}})
	_ = zeroLive
	powered := newActor()
	fundFollower(t, r, map[string]string{}, powered, 1_000_000, 1, map[string]uint32{prov.pub: 10000})
	top := dial(t, r.URL)
	top.login(r.URL, powered)
	top.live(map[string]any{"kinds": []int{protocol.KindAnchor}})

	sig := prov.signal(60, 1)
	pub.mustPublish(sig)
	anchor := prov.anchor(sig.ID)
	t0 := time.Now()
	pub.mustPublish(anchor)

	for name, f := range map[string]map[string]any{
		"kinds":   {"kinds": []int{protocol.KindAnchor}},
		"ids":     {"ids": []string{anchor.ID}},
		"#e":      {"#e": []string{sig.ID}},
		"authors": {"authors": []string{prov.pub}, "kinds": []int{protocol.KindAnchor}},
	} {
		ids, _ := zero.query(f)
		if len(ids) != 0 {
			t.Errorf("zero-power REQ by %s saw the attestation %s after its signal was received but before its release", name, time.Since(t0).Round(time.Millisecond))
		}
	}
	if n, _ := zero.count(map[string]any{"kinds": []int{protocol.KindAnchor}}); n != 0 {
		t.Errorf("zero-power COUNT = %d before the release", n)
	}
	if !top.waitFor(anchor.ID, 2*time.Second) {
		t.Error("the top tier never got the attestation")
	}
	// the zero-power tier gets it live together with the signal, not before
	if !zero.waitFor(anchor.ID, 6*time.Second) {
		t.Fatal("the zero-power tier never got the attestation live")
	}
	if d := mustArrival(t, zero, anchor.ID).Sub(t0); d < 1500*time.Millisecond {
		t.Errorf("zero-power live attestation arrived after %s, before the 2 s release", d)
	}
	if ids, _ := zero.query(map[string]any{"ids": []string{anchor.ID}}); len(ids) != 1 {
		t.Errorf("after the release the attestation is still hidden")
	}
}

func mustArrival(t *testing.T, c *client, id string) time.Time {
	t.Helper()
	at, ok := c.firstArrival(id)
	if !ok {
		t.Fatalf("event %s never arrived", id[:12])
	}
	return at
}

// ---- L4: rule-set created_at skew ----

func TestL4FarFutureRuleSetIsRefused(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	p := newActor()
	ok, why := c.publish(p.ruleSet(24*3600, 60, defaultRules)) // a day ahead would pin the provider's rules
	wantRefused(t, "rule set a day in the future", ok, why, "invalid:")
	if !strings.Contains(why, "ahead of the relay clock") {
		t.Errorf("reason %q should say the clock is ahead", why)
	}
	if ok, why := c.publish(p.ruleSet(2, 60, defaultRules)); !ok {
		t.Errorf("rule set 2 s ahead (within skew) refused: %s", why)
	}
}

// ---- L5: corrupt records are quarantined ----

func TestL5CorruptDirectoryRecordIsQuarantinedNotFatal(t *testing.T) {
	dir := t.TempDir()
	r := startRelay(t, dir)
	c := dial(t, r.URL)
	good, bad := newActor(), newActor()
	c.mustPublish(good.ruleSet(1, 60, defaultRules))
	c.mustPublish(bad.ruleSet(1, 60, defaultRules))
	r.Stop()

	s, err := state.Open(filepath.Join(dir, "data"))
	if err != nil {
		t.Fatal(err)
	}
	key := append([]byte{0xF1}, bad.pub...)
	if err := s.Update(func(tx *badger.Txn) error { return tx.Set(key, []byte("{not json")) }); err != nil {
		t.Fatal(err)
	}
	s.Close()

	cfg := config.Default()
	cfg.StoragePath = filepath.Join(dir, "data")
	cfg.Chains = []config.Chain{{ID: "fixture", Kind: "fixture", File: filepath.Join(dir, "balances.json")}}
	eng, err := relay.New(cfg, quietLogger())
	if err != nil {
		t.Fatalf("one corrupt record stopped the relay from starting: %v", err)
	}
	defer eng.Close()
	if p, _ := eng.Counts(); p != 1 {
		t.Errorf("%d providers restored, want 1 (the intact one)", p)
	}
}
