// Package state persists everything the relay must remember across restarts: the events
// (badger, via khatru's eventstore), per-signal receipt metadata and pending releases (SEP-3.41),
// provider rule sets and power allocations. All of it lives in one badger database.
package state

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"time"

	"github.com/dgraph-io/badger/v4"
	badgerstore "github.com/fiatjaf/eventstore/badger"
	"github.com/nbd-wtf/go-nostr"
)

// Key prefixes. The eventstore uses 0..8 and 255; ours start at 0xF0 and cannot collide.
const (
	prefixMeta    byte = 0xF0 // 0xF0 | event id (32)            -> Meta JSON
	prefixRuleSet byte = 0xF1 // 0xF1 | provider pubkey (hex)   -> Saved JSON
	prefixAlloc   byte = 0xF2 // 0xF2 | address (hex) | nonce 8 | event id (hex) -> Saved JSON
	prefixRevoke  byte = 0xF3 // 0xF3 | address (hex) | device pubkey (hex)       -> Saved JSON (earliest effective_at wins)
	prefixPending byte = 0xF4 // 0xF4 | received ns (8) | id    -> (empty)
	prefixActive  byte = 0xF5 // 0xF5 | provider pubkey (hex)   -> last signal time, ns (8)
)

// MetaTier is one rung of the rule set in force when a signal arrived. Delay is final: already
// multiplied by the timeframe and the relay's time scale.
type MetaTier struct {
	MinWei string `json:"min_wei"`
	Delay  int64  `json:"delay_ns"`
}

// Meta is what the relay knows about a gated event (signal or commitment) beyond its content.
type Meta struct {
	ID           string     `json:"id"`
	Kind         int        `json:"kind"`
	Provider     string     `json:"provider"`
	ProviderAddr string     `json:"provider_addr"`
	Private      bool       `json:"private"`
	Recipient    string     `json:"recipient,omitempty"`
	Received     int64      `json:"received_ns"`
	Tiers        []MetaTier `json:"tiers,omitempty"`
	PrivateMin   string     `json:"private_min_wei,omitempty"`
	CommitDelay  int64      `json:"commit_delay_ns,omitempty"` // private provider: when everyone else sees the commitment
	MaxDelay     int64      `json:"max_delay_ns"`
	Seq          uint64     `json:"seq,omitempty"` // relay receipt order; releases of one provider at one delay are delivered in this order
}

// ReceivedAt is the relay's receipt time.
func (m *Meta) ReceivedAt() time.Time { return time.Unix(0, m.Received) }

// Saved is a persisted rule set or allocation: the original signed event plus the relay's receipt time.
type Saved struct {
	Event    *nostr.Event `json:"event"`
	Received int64        `json:"received_ns"`
}

// Store wraps the badger eventstore and adds our key spaces.
type Store struct {
	*badgerstore.BadgerBackend
}

// Open opens (creating if needed) the database under path. Writes are synced: a pending release
// must survive a crash, and signal rates are low.
func Open(path string) (*Store, error) {
	b := &badgerstore.BadgerBackend{
		Path: path,
		BadgerOptionsModifier: func(o badger.Options) badger.Options {
			return o.WithLogger(nil).WithSyncWrites(true).
				WithMemTableSize(16 << 20).WithNumMemtables(2).WithValueLogFileSize(64 << 20).
				WithBlockCacheSize(32 << 20).WithIndexCacheSize(16 << 20).WithNumLevelZeroTables(2).WithNumLevelZeroTablesStall(4)
		},
	}
	if err := b.Init(); err != nil {
		return nil, fmt.Errorf("cannot open storage at %q: %w; check that the directory is writable and no other sarelay uses it", path, err)
	}
	return &Store{b}, nil
}

// Close closes the database.
func (s *Store) Close() { s.BadgerBackend.Close() }

func metaKey(id string) []byte {
	k := make([]byte, 1, 33)
	k[0] = prefixMeta
	raw := hexBytes(id)
	return append(k, raw...)
}

func pendingKey(recvNS int64, id string) []byte {
	k := make([]byte, 9, 41)
	k[0] = prefixPending
	binary.BigEndian.PutUint64(k[1:], uint64(recvNS))
	return append(k, hexBytes(id)...)
}

func hexBytes(h string) []byte {
	out := make([]byte, 0, len(h)/2)
	for i := 0; i+1 < len(h); i += 2 {
		out = append(out, unhex(h[i])<<4|unhex(h[i+1]))
	}
	return out
}

func unhex(c byte) byte {
	switch {
	case c >= '0' && c <= '9':
		return c - '0'
	case c >= 'a' && c <= 'f':
		return c - 'a' + 10
	case c >= 'A' && c <= 'F':
		return c - 'A' + 10
	}
	return 0
}

// PutMeta durably records a gated event's receipt and schedule. Call it before the event is saved.
func (s *Store) PutMeta(m *Meta) error {
	raw, err := json.Marshal(m)
	if err != nil {
		return err
	}
	return s.Update(func(tx *badger.Txn) error {
		if err := tx.Set(metaKey(m.ID), raw); err != nil {
			return err
		}
		return tx.Set(pendingKey(m.Received, m.ID), nil)
	})
}

// Meta returns the metadata of an event id, or nil when the relay never recorded one.
func (s *Store) Meta(id string) (*Meta, error) {
	var m *Meta
	err := s.View(func(tx *badger.Txn) error {
		it, err := tx.Get(metaKey(id))
		if err == badger.ErrKeyNotFound {
			return nil
		}
		if err != nil {
			return err
		}
		return it.Value(func(v []byte) error {
			m = new(Meta)
			return json.Unmarshal(v, m)
		})
	})
	return m, err
}

// DropMeta removes the metadata of an event that was never saved.
func (s *Store) DropMeta(m *Meta) error {
	return s.Update(func(tx *badger.Txn) error {
		_ = tx.Delete(pendingKey(m.Received, m.ID))
		return tx.Delete(metaKey(m.ID))
	})
}

// ClearPending forgets that m still has releases to deliver. Its Meta stays for visibility checks.
func (s *Store) ClearPending(m *Meta) error {
	return s.Update(func(tx *badger.Txn) error { return tx.Delete(pendingKey(m.Received, m.ID)) })
}

// Pending lists the metadata of every event whose releases were not yet cleared and that was
// received at or after since.
func (s *Store) Pending(since time.Time) ([]*Meta, error) {
	var out []*Meta
	err := s.View(func(tx *badger.Txn) error {
		it := tx.NewIterator(badger.IteratorOptions{Prefix: []byte{prefixPending}})
		defer it.Close()
		start := make([]byte, 9)
		start[0] = prefixPending
		binary.BigEndian.PutUint64(start[1:], uint64(since.UnixNano()))
		var ids [][]byte
		for it.Seek(start); it.Valid(); it.Next() {
			k := it.Item().KeyCopy(nil)
			ids = append(ids, k[9:])
		}
		for _, raw := range ids {
			item, err := tx.Get(append([]byte{prefixMeta}, raw...))
			if err == badger.ErrKeyNotFound {
				continue
			}
			if err != nil {
				return err
			}
			var m Meta
			if err := item.Value(func(v []byte) error { return json.Unmarshal(v, &m) }); err != nil {
				return err
			}
			out = append(out, &m)
		}
		return nil
	})
	return out, err
}

func ruleSetKey(provider string) []byte { return append([]byte{prefixRuleSet}, provider...) }

func allocKey(addr string, nonce uint64, eventID string) []byte {
	k := append([]byte{prefixAlloc}, addr...)
	var n [8]byte
	binary.BigEndian.PutUint64(n[:], nonce)
	return append(append(k, n[:]...), eventID...)
}

func (s *Store) putSaved(key []byte, sv Saved) error {
	raw, err := json.Marshal(sv)
	if err != nil {
		return err
	}
	return s.Update(func(tx *badger.Txn) error { return tx.Set(key, raw) })
}

// SaveRuleSet records the newest accepted rule set of a provider.
func (s *Store) SaveRuleSet(provider string, sv Saved) error {
	return s.putSaved(ruleSetKey(provider), sv)
}

// SaveAllocation records an accepted allocation (history is kept).
func (s *Store) SaveAllocation(addr string, nonce uint64, eventID string, sv Saved) error {
	return s.putSaved(allocKey(addr, nonce, eventID), sv)
}

// Quarantined describes a stored record that could not be decoded or no longer validates. The relay
// logs it, counts it and carries on without it; the record stays in the database for inspection.
type Quarantined struct {
	Key    string
	Reason string
}

// loadPrefix reads every record under a key prefix. A record whose JSON is corrupt is returned in the
// quarantine list instead of failing the whole load.
func (s *Store) loadPrefix(prefix byte) ([]Saved, []Quarantined, error) {
	var out []Saved
	var bad []Quarantined
	err := s.View(func(tx *badger.Txn) error {
		it := tx.NewIterator(badger.IteratorOptions{Prefix: []byte{prefix}, PrefetchValues: true, PrefetchSize: 64})
		defer it.Close()
		for it.Rewind(); it.Valid(); it.Next() {
			var sv Saved
			key := fmt.Sprintf("%x", it.Item().Key())
			if err := it.Item().Value(func(v []byte) error { return json.Unmarshal(v, &sv) }); err != nil {
				bad = append(bad, Quarantined{Key: key, Reason: "undecodable record: " + err.Error()})
				continue
			}
			if sv.Event == nil {
				bad = append(bad, Quarantined{Key: key, Reason: "record holds no event"})
				continue
			}
			out = append(out, sv)
		}
		return nil
	})
	return out, bad, err
}

// LoadRuleSets returns every saved rule set, and the undecodable ones separately.
func (s *Store) LoadRuleSets() ([]Saved, []Quarantined, error) { return s.loadPrefix(prefixRuleSet) }

// LoadAllocations returns every saved allocation in (address, nonce, event id) order.
func (s *Store) LoadAllocations() ([]Saved, []Quarantined, error) { return s.loadPrefix(prefixAlloc) }

// LoadRevocations returns every saved delegation revocation.
func (s *Store) LoadRevocations() ([]Saved, []Quarantined, error) { return s.loadPrefix(prefixRevoke) }

func revokeKey(addr, device string) []byte {
	return append(append([]byte{prefixRevoke}, addr...), device...)
}

// SaveRevocation records an accepted revocation of device under addr.
func (s *Store) SaveRevocation(addr, device string, sv Saved) error {
	return s.putSaved(revokeKey(addr, device), sv)
}

// DeleteRevocation removes a revocation record (rollback).
func (s *Store) DeleteRevocation(addr, device string) error {
	return s.Update(func(tx *badger.Txn) error { return tx.Delete(revokeKey(addr, device)) })
}

// DeleteRuleSet removes a provider's rule-set record (expiry or rollback).
func (s *Store) DeleteRuleSet(provider string) error {
	return s.Update(func(tx *badger.Txn) error { return tx.Delete(ruleSetKey(provider)) })
}

// DeleteAllocation removes one allocation record (rollback of a failed event write).
func (s *Store) DeleteAllocation(addr string, nonce uint64, eventID string) error {
	return s.Update(func(tx *badger.Txn) error { return tx.Delete(allocKey(addr, nonce, eventID)) })
}

func activeKey(provider string) []byte { return append([]byte{prefixActive}, provider...) }

// SaveActivity records the last time a provider published a signal.
func (s *Store) SaveActivity(provider string, at time.Time) error {
	var v [8]byte
	binary.BigEndian.PutUint64(v[:], uint64(at.UnixNano()))
	return s.Update(func(tx *badger.Txn) error { return tx.Set(activeKey(provider), v[:]) })
}

// DeleteActivity forgets a provider's activity record.
func (s *Store) DeleteActivity(provider string) error {
	return s.Update(func(tx *badger.Txn) error { return tx.Delete(activeKey(provider)) })
}

// LoadActivity returns provider -> last signal time.
func (s *Store) LoadActivity() (map[string]time.Time, error) {
	out := map[string]time.Time{}
	err := s.View(func(tx *badger.Txn) error {
		it := tx.NewIterator(badger.IteratorOptions{Prefix: []byte{prefixActive}, PrefetchValues: true})
		defer it.Close()
		for it.Rewind(); it.Valid(); it.Next() {
			k := string(it.Item().Key()[1:])
			_ = it.Item().Value(func(v []byte) error {
				if len(v) == 8 {
					out[k] = time.Unix(0, int64(binary.BigEndian.Uint64(v)))
				}
				return nil
			})
		}
		return nil
	})
	return out, err
}
