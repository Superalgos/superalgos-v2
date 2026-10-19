package protocol

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/nbd-wtf/go-nostr"
	"github.com/nbd-wtf/go-nostr/nip44"

	"github.com/superalgos/sarelay/internal/evmsig"
)

// Events produced by the real Python sa_core (testdata/gen_pyvectors.py) must pass the Go
// parsers: this pins message texts, field names and tag shapes on both sides.

type vectors struct {
	GeneratedWith string `json:"generated_with"`
	Keys          map[string]string
	Events        map[string]*nostr.Event
}

func loadVectors(t *testing.T) vectors {
	t.Helper()
	raw, err := os.ReadFile("../../testdata/pyvectors.json")
	if err != nil {
		t.Fatalf("%v; regenerate with testdata/gen_pyvectors.py", err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestPythonEventsVerifyInGo(t *testing.T) {
	v := loadVectors(t)
	if len(v.Events) < 10 {
		t.Fatalf("only %d vectors", len(v.Events))
	}
	for name, ev := range v.Events {
		// event id (sha256 of the canonical array) and BIP-340 signature, computed by Python, checked by Go
		if !ev.CheckID() {
			t.Errorf("%s: event id differs between Python and Go", name)
		}
		if ok, err := ev.CheckSignature(); !ok || err != nil {
			t.Errorf("%s: BIP-340 signature: %v", name, err)
		}
	}
}

func TestPythonRuleSetsParse(t *testing.T) {
	v := loadVectors(t)
	pub, err := ParseRuleSet(v.Events["ruleset_public"], DefaultLimits())
	if err != nil {
		t.Fatalf("public rule set from Python: %v", err)
	}
	if pub.Provider != v.Keys["provider_public_pubkey"] || pub.SAAddress != v.Keys["sa_public_address"] || pub.TimeframeSec != 3600 {
		t.Fatalf("%+v", pub)
	}
	got := []string{}
	for _, tr := range pub.Tiers {
		got = append(got, tr.MinPower+"@"+tr.Fraction)
	}
	if want := "500000@0 100000@0.1 10000@0.5 0@2"; strings.Join(got, " ") != want {
		t.Errorf("tiers %v, want %s (strongest first)", got, want)
	}
	if err := CheckRuleSetDelegation(v.Events["ruleset_public"], pub, 1_700_000_000); err != nil {
		t.Errorf("delegation of the Python rule set: %v", err)
	}
	if err := CheckRuleSetDelegation(v.Events["ruleset_public"], pub, 4_200_000_000); err == nil {
		t.Error("delegation accepted after its expiry")
	}
	priv, err := ParseRuleSet(v.Events["ruleset_private"], DefaultLimits())
	if err != nil || !priv.Private || priv.PrivateMin != "1000" || priv.SAAddress != v.Keys["sa_private_address"] {
		t.Fatalf("private rule set from Python: %+v %v", priv, err)
	}
	// resolution agrees with sa_core.power.resolve_tier on the thresholds that matter
	for tokens, want := range map[string]int{"600000": 0, "500000": 0, "499999": 1, "100000": 1, "10000": 2, "9999": 3, "0": 3} {
		if got := pub.TierIndex(TokensToWei(tokens)); got != want {
			t.Errorf("%s SA -> tier %d, want %d", tokens, got, want)
		}
	}
}

func TestPythonAllocationParses(t *testing.T) {
	v := loadVectors(t)
	a, err := ParseAllocation(v.Events["allocation"], DefaultLimits())
	if err != nil {
		t.Fatalf("allocation from Python: %v", err)
	}
	if a.Address != v.Keys["follower_address"] || a.Nonce != 5 || a.Governance != 3000 ||
		a.Providers[v.Keys["provider_public_pubkey"]] != 4000 || a.Providers[v.Keys["provider_private_pubkey"]] != 3000 {
		t.Fatalf("%+v", a)
	}
	g, err := ParseAllocation(v.Events["allocation_governance_only"], DefaultLimits())
	if err != nil || g.Governance != 10000 || len(g.Providers) != 0 {
		t.Fatalf("governance-only allocation: %+v %v", g, err)
	}
}

func TestPythonSignalsCommitmentsAndGovernance(t *testing.T) {
	v := loadVectors(t)
	sig := v.Events["signal_public"]
	s, err := ParsePublicSignal(sig.Content)
	if err != nil || s.TimeframeSec != 3600 || s.CreatedAt.Unix() != 1_700_000_000 || s.BarOpenAt.Unix() != 1_700_000_000-3600 {
		t.Fatalf("public signal: %+v %v", s, err)
	}
	if addr, _, err := evmsig.Delegation(sig.Tags, sig.PubKey, 1_700_000_000); err != nil || addr != v.Keys["sa_public_address"] {
		t.Fatalf("signal delegation: %s %v", addr, err)
	}

	priv := v.Events["signal_private"]
	if !LooksLikeNIP44(priv.Content) {
		t.Fatal("Python NIP-44 ciphertext not recognised")
	}
	if r, err := Recipient(priv.Tags); err != nil || r != v.Keys["follower_pubkey"] {
		t.Fatalf("recipient %s %v", r, err)
	}
	if _, _, err := evmsig.Delegation(priv.Tags, priv.PubKey, 1_700_000_000); err != nil {
		t.Fatalf("private signal delegation: %v", err)
	}
	// NIP-44 interoperability: Python encrypts, Go decrypts with the recipient's secret
	secret, _ := hex.DecodeString(v.Keys["follower_secret_hex"])
	ck, err := nip44.GenerateConversationKey(priv.PubKey, hex.EncodeToString(secret))
	if err != nil {
		t.Fatal(err)
	}
	plain, err := nip44.Decrypt(priv.Content, ck)
	if err != nil || !strings.Contains(plain, `"strategy_id":"alpha"`) {
		t.Fatalf("Go (go-nostr nip44) cannot decrypt the Python sa-core NIP-44 ciphertext: plain=%q err=%v; fix sa_core.nostr, then regenerate testdata/pyvectors.json", plain, err)
	}

	if c, err := ParseCommitment(v.Events["commitment"].Content); err != nil || c.Seq != 1 || c.Prev != nil || len(c.Digest) != 64 {
		t.Fatalf("commitment: %+v %v", c, err)
	}
	if err := ValidateGovDelegation(v.Events["gov_delegation"].Content); err != nil {
		t.Fatalf("governance delegation: %v", err)
	}
	vote := v.Events["vote"]
	if addr, _, err := evmsig.Delegation(vote.Tags, vote.PubKey, 0); err != nil || addr != v.Keys["follower_address"] {
		t.Fatalf("vote delegation: %s %v", addr, err)
	}
	auth := v.Events["auth"]
	if addr, _, err := evmsig.Delegation(auth.Tags, auth.PubKey, 1_700_000_000); err != nil || addr != v.Keys["follower_address"] {
		t.Fatalf("AUTH delegation: %s %v", addr, err)
	}
}
