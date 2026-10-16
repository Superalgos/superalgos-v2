// Package evmsig implements the EVM-side signatures the relay verifies: EIP-191 personal_sign
// recovery and the sa-delegation tag that binds a Nostr device key to an SA (EVM) address.
package evmsig

import (
	"encoding/hex"
	"fmt"
	"strconv"
	"strings"

	"github.com/decred/dcrd/dcrec/secp256k1/v4/ecdsa"
	"github.com/nbd-wtf/go-nostr"
	"golang.org/x/crypto/sha3"
)

// DelegationTag is the tag name carrying the SA delegation:
// ["sa-delegation", evm_address, expires_at, signature]. The name avoids "delegation",
// which NIP-26 relays parse (and nostr-rs-relay panics on).
const DelegationTag = "sa-delegation"

// Keccak256 is the legacy Keccak-256 used by Ethereum.
func Keccak256(b []byte) []byte {
	h := sha3.NewLegacyKeccak256()
	h.Write(b)
	return h.Sum(nil)
}

// PersonalDigest is the EIP-191 version 0x45 digest of msg.
func PersonalDigest(msg string) []byte {
	return Keccak256([]byte(fmt.Sprintf("\x19Ethereum Signed Message:\n%d%s", len(msg), msg)))
}

// NormalizeAddress lowercases a 0x-prefixed 20-byte hex address and rejects anything else.
func NormalizeAddress(a string) (string, error) {
	s := strings.ToLower(a)
	if len(s) != 42 || !strings.HasPrefix(s, "0x") {
		return "", fmt.Errorf("address %q must be 0x followed by 40 hex characters", a)
	}
	if _, err := hex.DecodeString(s[2:]); err != nil {
		return "", fmt.Errorf("address %q is not hex: %v", a, err)
	}
	return s, nil
}

// Recover returns the lowercase 0x address that signed msg with personal_sign.
func Recover(msg, sigHex string) (string, error) {
	sig, err := hex.DecodeString(strings.TrimPrefix(sigHex, "0x"))
	if err != nil || len(sig) != 65 {
		return "", fmt.Errorf("signature must be 65 hex bytes (r|s|v), got %d characters", len(sigHex))
	}
	v := sig[64]
	if v >= 27 {
		v -= 27
	}
	if v > 1 {
		return "", fmt.Errorf("signature recovery id %d must be 0, 1, 27 or 28", sig[64])
	}
	compact := append([]byte{27 + v + 4}, sig[:64]...)
	pub, _, err := ecdsa.RecoverCompact(compact, PersonalDigest(msg))
	if err != nil {
		return "", fmt.Errorf("cannot recover signer from signature: %w", err)
	}
	h := Keccak256(pub.SerializeUncompressed()[1:])
	return "0x" + hex.EncodeToString(h[len(h)-20:]), nil
}

// VerifyEIP191 reports whether sigHex over msg was made by address.
func VerifyEIP191(msg, sigHex, address string) error {
	want, err := NormalizeAddress(address)
	if err != nil {
		return err
	}
	got, err := Recover(msg, sigHex)
	if err != nil {
		return err
	}
	if got != want {
		return fmt.Errorf("signature recovers %s, but the message claims %s", got, want)
	}
	return nil
}

// DelegationMessage is what the SA address signs: sa-delegation:{nostr_pubkey_hex}:{expires_at}.
func DelegationMessage(nostrPubkey string, expiresAt int64) string {
	return fmt.Sprintf("sa-delegation:%s:%d", nostrPubkey, expiresAt)
}

// Delegation reads the single sa-delegation tag of an event signed by pubkey and verifies it:
// well-formed, unexpired at now (unix seconds), and the signature recovers the claimed address.
// It returns the lowercase SA address and the expiry.
func Delegation(tags nostr.Tags, pubkey string, now int64) (string, int64, error) {
	var tag nostr.Tag
	n := 0
	for _, t := range tags {
		if len(t) > 0 && t[0] == DelegationTag {
			tag = t
			n++
		}
	}
	if n == 0 {
		return "", 0, fmt.Errorf("no %q tag; add [%q, evm_address, expires_at, signature]", DelegationTag, DelegationTag)
	}
	if n != 1 || len(tag) != 4 {
		return "", 0, fmt.Errorf("need exactly one [%q, evm_address, expires_at, signature] tag, found %d (last has %d fields)", DelegationTag, n, len(tag))
	}
	expires, err := strconv.ParseInt(tag[2], 10, 64)
	if err != nil {
		return "", 0, fmt.Errorf("delegation expires_at %q is not an integer unix time", tag[2])
	}
	if expires <= now {
		return "", 0, fmt.Errorf("delegation expired at %d (now %d); ask the SA identity for a new one", expires, now)
	}
	addr, err := NormalizeAddress(tag[1])
	if err != nil {
		return "", 0, err
	}
	if err := VerifyEIP191(DelegationMessage(pubkey, expires), tag[3], addr); err != nil {
		return "", 0, fmt.Errorf("delegation to %s is invalid: %w", pubkey, err)
	}
	return addr, expires, nil
}
