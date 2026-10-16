// Package evmsigtest holds the EVM signing helpers that only tests use. Production code verifies
// signatures (package evmsig) and never signs: no private key reaches the relay (SEP-1.13). Nothing
// outside _test.go files may import this package; TestBinaryDoesNotLinkSigningHelpers enforces it.
package evmsigtest

import (
	"encoding/hex"
	"strconv"

	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/decred/dcrd/dcrec/secp256k1/v4/ecdsa"
	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/evmsig"
)

// AddressOf returns the lowercase 0x address of a secp256k1 key.
func AddressOf(k *secp256k1.PrivateKey) string {
	h := evmsig.Keccak256(k.PubKey().SerializeUncompressed()[1:])
	return "0x" + hex.EncodeToString(h[len(h)-20:])
}

// Sign produces a 65-byte r|s|v (v = 27/28) personal_sign signature as 0x hex.
func Sign(k *secp256k1.PrivateKey, msg string) string {
	c := ecdsa.SignCompact(k, evmsig.PersonalDigest(msg), true) // [27+4+recid | r | s]
	sig := append(append([]byte{}, c[1:]...), c[0]-4)
	return "0x" + hex.EncodeToString(sig)
}

// SignDelegation builds the tag a device key attaches to prove its SA address allowed it.
func SignDelegation(k *secp256k1.PrivateKey, nostrPubkey string, expiresAt int64) nostr.Tag {
	return nostr.Tag{evmsig.DelegationTag, AddressOf(k), strconv.FormatInt(expiresAt, 10), Sign(k, evmsig.DelegationMessage(nostrPubkey, expiresAt))}
}
