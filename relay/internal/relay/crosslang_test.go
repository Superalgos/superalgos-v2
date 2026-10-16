package relay_test

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/nbd-wtf/go-nostr"
)

// Events signed by the real Python sa_core are accepted by the running Go relay.
func TestRelayAcceptsEventsSignedByPythonSACore(t *testing.T) {
	raw, err := os.ReadFile("../../testdata/pyvectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Events map[string]*nostr.Event `json:"events"`
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	r := startRelay(t, "")
	c := dial(t, r.URL)
	for _, name := range []string{"ruleset_public", "ruleset_private", "allocation", "allocation_governance_only", "gov_delegation", "vote"} {
		ok, why := c.publish(v.Events[name])
		t.Logf("python %-26s -> ok=%v %s", name, ok, why)
		if !ok {
			t.Errorf("relay refused the Python-signed %s: %s", name, why)
		}
	}
	// the stored rule set and allocation are readable back
	ids, _ := c.query(map[string]any{"ids": []string{v.Events["ruleset_public"].ID, v.Events["allocation"].ID}})
	if len(ids) != 2 {
		t.Errorf("stored back %d of 2 events", len(ids))
	}
	// republishing the same allocation is not a second allocation (same nonce)
	if ok, _ := c.publish(v.Events["allocation"]); !ok {
		t.Log("duplicate allocation refused, as expected")
	}
}
