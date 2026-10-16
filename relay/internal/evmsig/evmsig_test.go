package evmsig_test

import (
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/evmsig"
	"github.com/superalgos/sarelay/internal/evmsig/evmsigtest"
)

// Vectors produced independently by Python eth_account 0.14 (the library the Python side uses),
// key 0x11 repeated 32 times.
const (
	vecAddr   = "0x19e7e376e7c213b7e7e7e46cc70a5dd086daff2a"
	vecPub    = "abababababababababababababababababababababababababababababababab"
	vecDelMsg = "sa-delegation:" + vecPub + ":4102444800"
	vecDelSig = "0x8100754914f93d690fb47bd7356642ff95e405b5a9fc9b5e87d0c2ea60326ee45f2d7d5bc64643b82ade8c4c1381d7121441407dacf63eb480af92d103fbd8c41b"
)

func key11() *secp256k1.PrivateKey {
	b := make([]byte, 32)
	for i := range b {
		b[i] = 0x11
	}
	return secp256k1.PrivKeyFromBytes(b)
}

func TestMatchesPythonEthAccount(t *testing.T) {
	if got := evmsig.DelegationMessage(vecPub, 4102444800); got != vecDelMsg {
		t.Fatalf("delegation message %q, want %q", got, vecDelMsg)
	}
	if got, err := evmsig.Recover(vecDelMsg, vecDelSig); err != nil || got != vecAddr {
		t.Fatalf("Recover = %q, %v; want %s", got, err, vecAddr)
	}
	if got := evmsigtest.AddressOf(key11()); got != vecAddr {
		t.Fatalf("AddressOf = %s", got)
	}
	// our signer is deterministic (RFC 6979) and must agree with eth_account byte for byte
	if got := evmsigtest.Sign(key11(), vecDelMsg); got != vecDelSig {
		t.Fatalf("Sign = %s\nwant   %s", got, vecDelSig)
	}
}

func TestDelegation(t *testing.T) {
	k := key11()
	now := int64(1_000)
	good := evmsigtest.SignDelegation(k, vecPub, now+60)
	for _, c := range []struct {
		name string
		tags nostr.Tags
		pub  string
		ok   bool
		sub  string
	}{
		{"valid", nostr.Tags{good}, vecPub, true, ""},
		{"expired", nostr.Tags{evmsigtest.SignDelegation(k, vecPub, now-1)}, vecPub, false, "expired"},
		{"bound to another pubkey", nostr.Tags{good}, strings.Repeat("cd", 32), false, "invalid"},
		{"missing", nostr.Tags{{"p", "x"}}, vecPub, false, "no \"sa-delegation\" tag"},
		{"duplicated", nostr.Tags{good, good}, vecPub, false, "exactly one"},
		{"short tag", nostr.Tags{{evmsig.DelegationTag, vecAddr, "5"}}, vecPub, false, "exactly one"},
		{"expiry altered", nostr.Tags{{evmsig.DelegationTag, good[1], "9999", good[3]}}, vecPub, false, "invalid"},
		{"address altered", nostr.Tags{{evmsig.DelegationTag, "0x" + strings.Repeat("1", 40), good[2], good[3]}}, vecPub, false, "invalid"},
		{"bad address", nostr.Tags{{evmsig.DelegationTag, "0x12", good[2], good[3]}}, vecPub, false, "address"},
		{"junk signature", nostr.Tags{{evmsig.DelegationTag, good[1], good[2], "0xzz"}}, vecPub, false, "65 hex bytes"},
	} {
		addr, exp, err := evmsig.Delegation(c.tags, c.pub, now)
		if c.ok != (err == nil) {
			t.Errorf("%s: err=%v, want ok=%v", c.name, err, c.ok)
			continue
		}
		if c.ok && (addr != vecAddr || exp != now+60) {
			t.Errorf("%s: got %s %d", c.name, addr, exp)
		}
		if !c.ok && !strings.Contains(err.Error(), c.sub) {
			t.Errorf("%s: error %q should mention %q", c.name, err, c.sub)
		}
	}
	_ = time.Now
}

func TestRecoverAcceptsBothVConventions(t *testing.T) {
	sig := []byte(vecDelSig)
	_ = sig
	raw := vecDelSig[:len(vecDelSig)-2]
	for _, v := range []string{"1b", "00"} { // 27 and 0
		if got, err := evmsig.Recover(vecDelMsg, raw+v); err != nil || got != vecAddr {
			t.Errorf("v=%s: %s %v", v, got, err)
		}
	}
	if _, err := evmsig.Recover(vecDelMsg, raw+"05"); err == nil {
		t.Error("recovery id 5 accepted")
	}
}

// SEP-1.13: the relay verifies signatures and never signs. The signing helpers live in evmsigtest, which
// only tests may import, so the shipped binary must not link it (and so holds no code path that takes
// a private key).
func TestBinaryDoesNotLinkSigningHelpers(t *testing.T) {
	out, err := exec.Command("go", "list", "-deps", "github.com/superalgos/sarelay/cmd/sarelay").CombinedOutput()
	if err != nil {
		t.Skipf("go list unavailable (%v): %s", err, out)
	}
	for _, pkg := range strings.Fields(string(out)) {
		if strings.HasSuffix(pkg, "/evmsigtest") {
			t.Fatalf("cmd/sarelay links %s; signing helpers must stay test-only (move the caller into a _test.go file)", pkg)
		}
	}
	if !strings.Contains(string(out), "github.com/superalgos/sarelay/internal/evmsig\n") {
		t.Fatalf("go list -deps did not list internal/evmsig; the probe is not looking at the binary:\n%s", out)
	}
}
