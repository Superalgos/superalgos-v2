package relay_test

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/nbd-wtf/go-nostr"
)

// withExtra returns a copy of ev whose JSON content also carries field, re-signed by sk. Everything
// else (tags, kind, the SA signature inside the content) is unchanged, so the only thing wrong with
// the event is the extra field.
func withExtra(t *testing.T, ev *nostr.Event, sk, field string) *nostr.Event {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal([]byte(ev.Content), &m); err != nil {
		t.Fatalf("event content is not a JSON object: %v", err)
	}
	m[field] = "hunter2"
	b, _ := json.Marshal(m)
	cp := *ev
	cp.Content, cp.Sig, cp.ID = string(b), "", ""
	if err := cp.Sign(sk); err != nil {
		t.Fatal(err)
	}
	return &cp
}

// SEP-1.13 probe: the relay rejects a signal, commitment, allocation or rule set whose content carries a
// field outside the schema (a secret pasted by mistake, say), and the rejection names the field. The
// same events without the extra field are accepted, so the field is the only difference.
func TestRelayRejectsSecretFieldsInContent(t *testing.T) {
	r := startRelay(t, "")
	pub := dial(t, r.URL)
	prov := registerProvider(t, r, 60, defaultRules)
	sa := newActor()

	type kindCase struct {
		name string
		sk   string
		make func(n int) *nostr.Event
	}
	cases := []kindCase{
		{"signal 9317", prov.sk, func(n int) *nostr.Event { return prov.signal(60, n) }},
		{"commitment 9318", prov.sk, func(n int) *nostr.Event { return prov.commitment(1, nil) }},
		{"allocation 9332", sa.sk, func(n int) *nostr.Event {
			return sa.allocation(uint64(n), 0, map[string]uint32{prov.pub: 5000})
		}},
		{"rule set 37318", prov.sk, func(n int) *nostr.Event { return prov.ruleSet(int64(2+n%6), 60, defaultRules) }},
	}
	for i, c := range cases {
		if ok, why := pub.publish(c.make(i + 1)); !ok {
			t.Fatalf("%s: the clean event was refused (%s); the probe would prove nothing", c.name, why)
		}
		for _, field := range []string{"api_secret", "private_key", "mnemonic"} {
			ok, why := pub.publish(withExtra(t, c.make(i+10), c.sk, field))
			if ok {
				t.Errorf("%s with %q in its content was accepted", c.name, field)
				continue
			}
			if !strings.HasPrefix(why, "invalid:") || !strings.Contains(why, field) {
				t.Errorf("%s with %q: reason %q should start with invalid: and name the field", c.name, field, why)
			}
		}
	}
}
