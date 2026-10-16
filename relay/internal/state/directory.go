package state

import (
	"fmt"
	"math/big"
	"sort"
	"sync"
	"time"

	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/evmsig"
	"github.com/superalgos/sarelay/internal/protocol"
)

// Persister is where the Directory writes accepted records. *Store implements it.
type Persister interface {
	SaveRuleSet(provider string, sv Saved) error
	DeleteRuleSet(provider string) error
	SaveAllocation(addr string, nonce uint64, eventID string, sv Saved) error
	DeleteAllocation(addr string, nonce uint64, eventID string) error
	SaveRevocation(addr, device string, sv Saved) error
	DeleteRevocation(addr, device string) error
	SaveActivity(provider string, at time.Time) error
	DeleteActivity(provider string) error
}

// activityGranularity is how often a provider's last-signal time is written to disk. Idle expiry is
// measured in days, so an hour of imprecision is harmless and a synced write per signal is avoided.
const activityGranularity = time.Hour

// Directory holds provider registrations, power allocations and delegation revocations in memory. A
// provider is "registered" once its rule set exists (SEP-3.39). Writers must call Check* then Commit*
// under their own lock so that checks and commits are atomic with the event store write.
type Directory struct {
	Lim              protocol.Limits
	AllocDelay       time.Duration // D: an allocation counts this long after the relay received it
	MaxProviders     int
	MinAllocInterval time.Duration
	Skew             time.Duration // a rule set's created_at may lead the relay clock by this much (+1 s)
	P                Persister

	mu         sync.RWMutex
	providers  map[string]*protocol.RuleSet
	ruleSaved  map[string]Saved // the stored rule-set event per provider (restored if a write fails)
	byAddr     map[string]string
	allocs     map[string][]allocRec // per SA address, ascending (nonce, event id)
	lastAlloc  map[string]time.Time
	revoked    map[string]int64     // address|device pubkey -> earliest effective_at (unix s)
	revSaved   map[string]Saved     // the stored revocation event behind each entry
	active     map[string]time.Time // provider -> last signal (or registration)
	activePers map[string]time.Time // provider -> last value written to disk
	nAllocs    int
}

type allocRec struct {
	a        *protocol.Allocation
	received time.Time
}

// NewDirectory builds an empty directory.
func NewDirectory(lim protocol.Limits, allocDelay time.Duration, maxProviders int, minAllocInterval time.Duration, p Persister) *Directory {
	return &Directory{
		Lim: lim, AllocDelay: allocDelay, MaxProviders: maxProviders, MinAllocInterval: minAllocInterval, P: p,
		Skew:      10 * time.Second,
		providers: map[string]*protocol.RuleSet{}, ruleSaved: map[string]Saved{}, byAddr: map[string]string{},
		allocs: map[string][]allocRec{}, lastAlloc: map[string]time.Time{},
		revoked: map[string]int64{}, revSaved: map[string]Saved{}, active: map[string]time.Time{}, activePers: map[string]time.Time{},
	}
}

// Load rebuilds the directory from saved records, re-verifying every signature. A record that no
// longer validates is quarantined (skipped and returned), never fatal: one bad record must not stop
// the relay or take every other provider and allocation down with it.
func (d *Directory) Load(rules, allocs, revs []Saved, activity map[string]time.Time) []Quarantined {
	var bad []Quarantined
	for _, sv := range rules {
		rs, err := protocol.ParseRuleSet(sv.Event, d.Lim)
		if err != nil {
			bad = append(bad, Quarantined{Key: "ruleset " + sv.Event.ID, Reason: err.Error()})
			continue
		}
		d.insertRuleSet(rs, sv)
		at := time.Unix(0, sv.Received)
		if a, ok := activity[rs.Provider]; ok && a.After(at) {
			at = a
		}
		d.mu.Lock()
		d.active[rs.Provider], d.activePers[rs.Provider] = at, at
		d.mu.Unlock()
	}
	for _, sv := range allocs {
		a, err := protocol.ParseAllocation(sv.Event, d.Lim)
		if err != nil {
			bad = append(bad, Quarantined{Key: "allocation " + sv.Event.ID, Reason: err.Error()})
			continue
		}
		d.insertAlloc(a, time.Unix(0, sv.Received))
	}
	for _, sv := range revs {
		r, err := protocol.ParseRevocation(sv.Event)
		if err != nil {
			bad = append(bad, Quarantined{Key: "revocation " + sv.Event.ID, Reason: err.Error()})
			continue
		}
		d.insertRevocation(r, sv)
	}
	return bad
}

func (d *Directory) insertRuleSet(rs *protocol.RuleSet, sv Saved) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.providers[rs.Provider] = rs
	d.ruleSaved[rs.Provider] = sv
	d.byAddr[rs.SAAddress] = rs.Provider
}

func (d *Directory) insertAlloc(a *protocol.Allocation, received time.Time) {
	d.mu.Lock()
	defer d.mu.Unlock()
	recs := append(d.allocs[a.Address], allocRec{a, received})
	sort.SliceStable(recs, func(i, j int) bool {
		if recs[i].a.Nonce != recs[j].a.Nonce {
			return recs[i].a.Nonce < recs[j].a.Nonce
		}
		return recs[i].a.EventID < recs[j].a.EventID
	})
	d.allocs[a.Address] = recs
	if received.After(d.lastAlloc[a.Address]) {
		d.lastAlloc[a.Address] = received
	}
	d.nAllocs++
}

func (d *Directory) insertRevocation(r *protocol.Revocation, sv Saved) {
	d.mu.Lock()
	defer d.mu.Unlock()
	k := revKey(r.Address, r.DevicePub)
	if cur, ok := d.revoked[k]; !ok || r.EffectiveAt < cur {
		d.revoked[k], d.revSaved[k] = r.EffectiveAt, sv
	}
}

func revKey(addr, device string) string { return addr + "|" + device }

// CheckRuleSet validates a rule-set event against the current directory without changing it. now is
// the relay clock the caller judged the whole event at.
func (d *Directory) CheckRuleSet(ev *nostr.Event, now time.Time) (*protocol.RuleSet, error) {
	rs, err := protocol.ParseRuleSet(ev, d.Lim)
	if err != nil {
		return nil, err
	}
	// The newest created_at wins, so a far-future one would pin the provider's rules for good.
	if limit := now.Add(d.Skew + time.Second).Unix(); rs.CreatedAt > limit {
		return nil, fmt.Errorf("rule set created_at %d is %ds ahead of the relay clock; the limit is %s. Fix the publisher's clock", rs.CreatedAt, rs.CreatedAt-now.Unix(), d.Skew+time.Second)
	}
	if max := d.Lim.MaxReleaseDelay; max > 0 {
		for _, t := range rs.Tiers {
			if w := protocol.DelayFor(t.FracRat, rs.TimeframeSec, 1); w > max {
				return nil, fmt.Errorf("tier min_power=%s waits %s (delay_fraction %s x timeframe %ds), above the relay's max_release_delay %s; lower the delay_fraction or the timeframe", t.MinPower, w, t.Fraction, rs.TimeframeSec, max)
			}
		}
	}
	d.mu.RLock()
	defer d.mu.RUnlock()
	if cur, ok := d.providers[rs.Provider]; ok {
		if rs.CreatedAt <= cur.CreatedAt {
			return nil, fmt.Errorf("rule set created_at %d is not newer than the registered rule set's %d; publish a new rule set with a later created_at", rs.CreatedAt, cur.CreatedAt)
		}
		if cur.SAAddress != rs.SAAddress {
			return nil, fmt.Errorf("provider %s is registered to SA address %s; a rule set from %s cannot replace it", short(rs.Provider), cur.SAAddress, rs.SAAddress)
		}
	} else if d.MaxProviders > 0 && len(d.providers) >= d.MaxProviders {
		return nil, fmt.Errorf("the relay already serves its maximum of %d providers; try again later (providers idle past policy.provider_idle_expiry are removed)", d.MaxProviders)
	}
	if other, ok := d.byAddr[rs.SAAddress]; ok && other != rs.Provider {
		return nil, fmt.Errorf("SA address %s is already bound to provider %s; one SA identity registers one provider key", rs.SAAddress, short(other))
	}
	return rs, nil
}

// CommitRuleSet records an accepted rule set. The directory record is written first, then save stores
// the event; if save fails the previous record is restored, so the two never disagree.
func (d *Directory) CommitRuleSet(ev *nostr.Event, rs *protocol.RuleSet, received time.Time, save func() error) error {
	sv := Saved{Event: ev, Received: received.UnixNano()}
	d.mu.RLock()
	prev, hadPrev := d.ruleSaved[rs.Provider]
	d.mu.RUnlock()
	if err := d.P.SaveRuleSet(rs.Provider, sv); err != nil {
		return fmt.Errorf("cannot persist rule set %s: %w", short(ev.ID), err)
	}
	if save != nil {
		if err := save(); err != nil {
			var rerr error
			if hadPrev {
				rerr = d.P.SaveRuleSet(rs.Provider, prev)
			} else {
				rerr = d.P.DeleteRuleSet(rs.Provider)
			}
			if rerr != nil {
				err = fmt.Errorf("%w; the directory record could not be rolled back either (%v): restart the relay", err, rerr)
			}
			return err
		}
	}
	d.insertRuleSet(rs, sv)
	d.mu.Lock()
	if _, ok := d.active[rs.Provider]; !ok {
		d.active[rs.Provider], d.activePers[rs.Provider] = received, received
	}
	d.mu.Unlock()
	return nil
}

// CheckAllocation validates an allocation event against history without changing it.
//
// The event must carry exactly one valid sa-delegation tag naming the allocating address, and be signed
// by the device key it delegates. Without that rule anyone could re-wrap a victim's public allocation in
// an event of their own: refreshing the victim's rate limit, or replacing which device key is the
// victim's, while the EIP-191 signature in the content stays valid.
func (d *Directory) CheckAllocation(ev *nostr.Event, now time.Time) (*protocol.Allocation, error) {
	a, err := protocol.ParseAllocation(ev, d.Lim)
	if err != nil {
		return nil, err
	}
	dAddr, _, err := evmsig.Delegation(ev.Tags, ev.PubKey, now.Unix())
	if err != nil {
		return nil, fmt.Errorf("an allocation must carry exactly one valid sa-delegation tag from the allocating SA address %s, signed by a device key it delegates: %v", a.Address, err)
	}
	if dAddr != a.Address {
		return nil, fmt.Errorf("delegation is from %s but the allocation is for %s; only a device key delegated by %s may publish it", dAddr, a.Address, a.Address)
	}
	d.mu.RLock()
	defer d.mu.RUnlock()
	if at, ok := d.revoked[revKey(a.Address, ev.PubKey)]; ok && at <= now.Unix() {
		return nil, fmt.Errorf("device key %s was revoked by %s effective %d; publish with a device key that is still delegated", short(ev.PubKey), a.Address, at)
	}
	if recs := d.allocs[a.Address]; len(recs) > 0 {
		top := recs[len(recs)-1].a.Nonce
		if a.Nonce < top {
			return nil, fmt.Errorf("allocation nonce %d is older than %d already accepted for %s; sign again with a higher nonce", a.Nonce, top, a.Address)
		}
		// An equal nonce with different content is equivocation by the SA identity: both events are kept
		// and the smallest event id wins (same rule as sa_core.power.allocation_in_force). The same event,
		// or the same signed content in another wrapper, is not.
		if a.Nonce == top {
			msg := a.Message()
			for _, r := range recs {
				if r.a.Nonce == top && (r.a.EventID == a.EventID || r.a.Message() == msg) {
					return nil, fmt.Errorf("duplicate: allocation %s repeats a signed allocation already accepted for %s (nonce %d)", short(a.EventID), a.Address, a.Nonce)
				}
			}
		}
	}
	if last, ok := d.lastAlloc[a.Address]; ok && now.Sub(last) < d.MinAllocInterval {
		return nil, fmt.Errorf("rate-limited: %s changed its allocation %s ago; wait %s between allocations", a.Address, now.Sub(last).Round(time.Millisecond), d.MinAllocInterval)
	}
	return a, nil
}

// CommitAllocation records an accepted allocation: the directory record first, then save stores the
// event; if save fails the record is removed again.
func (d *Directory) CommitAllocation(ev *nostr.Event, a *protocol.Allocation, received time.Time, save func() error) error {
	if err := d.P.SaveAllocation(a.Address, a.Nonce, ev.ID, Saved{Event: ev, Received: received.UnixNano()}); err != nil {
		return fmt.Errorf("cannot persist allocation %s: %w", short(ev.ID), err)
	}
	if save != nil {
		if err := save(); err != nil {
			if rerr := d.P.DeleteAllocation(a.Address, a.Nonce, ev.ID); rerr != nil {
				err = fmt.Errorf("%w; the directory record could not be rolled back either (%v): restart the relay", err, rerr)
			}
			return err
		}
	}
	d.insertAlloc(a, received)
	return nil
}

// CheckRevocation validates a kind-9334 event. Anyone may carry it: the EIP-191 signature by the
// address is the authority. A revocation that does not bring the effective time forward is a duplicate.
func (d *Directory) CheckRevocation(ev *nostr.Event) (*protocol.Revocation, error) {
	r, err := protocol.ParseRevocation(ev)
	if err != nil {
		return nil, err
	}
	d.mu.RLock()
	defer d.mu.RUnlock()
	if cur, ok := d.revoked[revKey(r.Address, r.DevicePub)]; ok && cur <= r.EffectiveAt {
		return nil, fmt.Errorf("duplicate: device %s is already revoked under %s effective %d", short(r.DevicePub), r.Address, cur)
	}
	return r, nil
}

// CommitRevocation records an accepted revocation (record first, then save, with rollback).
func (d *Directory) CommitRevocation(ev *nostr.Event, r *protocol.Revocation, received time.Time, save func() error) error {
	k := revKey(r.Address, r.DevicePub)
	d.mu.RLock()
	prev, had := d.revSaved[k]
	d.mu.RUnlock()
	sv := Saved{Event: ev, Received: received.UnixNano()}
	if err := d.P.SaveRevocation(r.Address, r.DevicePub, sv); err != nil {
		return fmt.Errorf("cannot persist revocation %s: %w", short(ev.ID), err)
	}
	if save != nil {
		if err := save(); err != nil {
			var rerr error
			if had {
				rerr = d.P.SaveRevocation(r.Address, r.DevicePub, prev)
			} else {
				rerr = d.P.DeleteRevocation(r.Address, r.DevicePub)
			}
			if rerr != nil {
				err = fmt.Errorf("%w; the directory record could not be rolled back either (%v): restart the relay", err, rerr)
			}
			return err
		}
	}
	d.insertRevocation(r, sv)
	return nil
}

// Revoked reports whether device's delegation under addr has been revoked as of now.
func (d *Directory) Revoked(addr, device string, now time.Time) bool {
	d.mu.RLock()
	defer d.mu.RUnlock()
	at, ok := d.revoked[revKey(addr, device)]
	return ok && at <= now.Unix()
}

// Known reports whether addr holds an allocation on this relay or is a registered provider's SA
// address. Only those addresses are worth a balance lookup.
func (d *Directory) Known(addr string) bool {
	d.mu.RLock()
	defer d.mu.RUnlock()
	if len(d.allocs[addr]) > 0 {
		return true
	}
	_, ok := d.byAddr[addr]
	return ok
}

// Touch records that provider published a signal at now.
func (d *Directory) Touch(provider string, now time.Time) {
	d.mu.Lock()
	if _, ok := d.providers[provider]; !ok {
		d.mu.Unlock()
		return
	}
	d.active[provider] = now
	persist := now.Sub(d.activePers[provider]) >= activityGranularity
	if persist {
		d.activePers[provider] = now
	}
	d.mu.Unlock()
	if persist {
		_ = d.P.SaveActivity(provider, now) // best effort: losing it only shortens a provider's grace period
	}
}

// Providers lists the registered rule sets.
func (d *Directory) Providers() []*protocol.RuleSet {
	d.mu.RLock()
	defer d.mu.RUnlock()
	out := make([]*protocol.RuleSet, 0, len(d.providers))
	for _, rs := range d.providers {
		out = append(out, rs)
	}
	return out
}

// Idle lists providers with no signal (and no registration) since cutoff.
func (d *Directory) Idle(cutoff time.Time) []string {
	d.mu.RLock()
	defer d.mu.RUnlock()
	var out []string
	for pk := range d.providers {
		if d.active[pk].Before(cutoff) {
			out = append(out, pk)
		}
	}
	return out
}

// Remove deregisters a provider (idle expiry, or its SA address no longer holds the required power).
// Its stored events stay; signals already scheduled are still delivered. The provider can register again.
func (d *Directory) Remove(provider string) error {
	d.mu.Lock()
	rs, ok := d.providers[provider]
	d.mu.Unlock()
	if !ok {
		return nil
	}
	if err := d.P.DeleteRuleSet(provider); err != nil {
		return fmt.Errorf("cannot remove provider %s: %w", short(provider), err)
	}
	_ = d.P.DeleteActivity(provider)
	d.mu.Lock()
	delete(d.providers, provider)
	delete(d.ruleSaved, provider)
	delete(d.active, provider)
	delete(d.activePers, provider)
	if d.byAddr[rs.SAAddress] == provider {
		delete(d.byAddr, rs.SAAddress)
	}
	d.mu.Unlock()
	return nil
}

// Provider returns the registered rule set of a provider pubkey.
func (d *Directory) Provider(pubkey string) (*protocol.RuleSet, bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	rs, ok := d.providers[pubkey]
	return rs, ok
}

// ProviderForAddress returns the provider pubkey registered by an SA address.
func (d *Directory) ProviderForAddress(addr string) (string, bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	p, ok := d.byAddr[addr]
	return p, ok
}

// BPS is the share (basis points) of address's token power allocated to provider at time at: the
// highest-nonce allocation whose delay D has elapsed; among equal nonces the smallest event id
// (matching sa-core). Without one, 0 (SEP-3.22, SEP-3.35).
func (d *Directory) BPS(addr, provider string, at time.Time) uint32 {
	d.mu.RLock()
	defer d.mu.RUnlock()
	// recs is ascending by (nonce, event id): scan down for the first effective record, then walk
	// back up over its nonce group to the smallest event id that is also effective.
	recs := d.allocs[addr]
	for i := len(recs) - 1; i >= 0; i-- {
		if recs[i].received.Add(d.AllocDelay).After(at) {
			continue
		}
		best := i
		for j := i - 1; j >= 0 && recs[j].a.Nonce == recs[i].a.Nonce; j-- {
			if !recs[j].received.Add(d.AllocDelay).After(at) {
				best = j
			}
		}
		return recs[best].a.Providers[provider]
	}
	return 0
}

// Power is total x bps(address, provider, at) / 10000, rounded down (SEP-3.22, SEP-3.33).
func (d *Directory) Power(total *big.Int, addr, provider string, at time.Time) *big.Int {
	if total == nil || total.Sign() == 0 {
		return new(big.Int)
	}
	b := d.BPS(addr, provider, at)
	p := new(big.Int).Mul(total, big.NewInt(int64(b)))
	return p.Quo(p, big.NewInt(protocol.MaxBPS))
}

// Counts reports registered providers and accepted allocations.
func (d *Directory) Counts() (providers, allocations int) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	return len(d.providers), d.nAllocs
}

func short(h string) string {
	if len(h) > 12 {
		return h[:12] + ".."
	}
	return h
}
