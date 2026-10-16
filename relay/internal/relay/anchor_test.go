package relay_test

import (
	"strings"
	"testing"

	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/protocol"
)

// A kind-1040 attestation holding a real TSA proof (three tokens, about 27 KB) is stored; one beyond the
// anchor limit is refused with a message that names the limit.
func TestAnchorAttestationsMayCarryTSAProofs(t *testing.T) {
	r := startRelay(t, "")
	c := dial(t, r.URL)
	sk := nostr.GeneratePrivateKey()
	pub, _ := nostr.GetPublicKey(sk)
	target := newActor().ruleSet(1, 60, defaultRules) // any event the relay holds
	c.mustPublish(target)
	mk := func(n int) *nostr.Event {
		ev := &nostr.Event{
			PubKey: pub, CreatedAt: nostr.Now(), Kind: protocol.KindAnchor,
			Tags:    nostr.Tags{{"e", target.ID}, {"sa-anchor", "tsa"}},
			Content: strings.Repeat("x", n),
		}
		_ = ev.Sign(sk)
		return ev
	}
	if ok, msg := c.publish(mk(30 * 1024)); !ok {
		t.Fatalf("a 30 KB TSA attestation was refused: %s", msg)
	}
	ok, msg := c.publish(mk(70 * 1024))
	if ok || !strings.Contains(msg, "limit for kind 1040") {
		t.Fatalf("a 70 KB attestation should be refused with the limit in the message, got ok=%v msg=%q", ok, msg)
	}
}
