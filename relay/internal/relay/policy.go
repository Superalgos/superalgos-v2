package relay

import (
	"context"
	"fmt"
	"math/big"
	"strconv"
	"time"

	"github.com/fiatjaf/eventstore"
	"github.com/nbd-wtf/go-nostr"

	"github.com/superalgos/sarelay/internal/evmsig"
	"github.com/superalgos/sarelay/internal/protocol"
	"github.com/superalgos/sarelay/internal/state"
)

const (
	maxGatedContent = 70000 // NIP-44 payloads reach 65,603 bytes before base64 (about 87,500 with it)
	maxOtherContent = 16 * 1024
	// A TSA anchor attestation carries two or three RFC 3161 tokens (about 6-8 KB each, base64 on the wire),
	// so it outgrows the generic limit: 3 default endpoints give about 27 KB.
	maxAnchorContent = 64 * 1024
)

func kindName(k int) string {
	switch k {
	case protocol.KindSignal:
		return "signal"
	case protocol.KindCommitment:
		return "commitment"
	case protocol.KindAllocation:
		return "allocation"
	case protocol.KindRuleSet:
		return "ruleset"
	case protocol.KindProposal:
		return "proposal"
	case protocol.KindVote:
		return "vote"
	case protocol.KindGovDeleg:
		return "govdelegation"
	case protocol.KindRevocation:
		return "revocation"
	case protocol.KindAnchor:
		return "anchor"
	}
	return "other"
}

func (e *Engine) allowedKind(k int) bool {
	switch k {
	case protocol.KindSignal, protocol.KindCommitment, protocol.KindAllocation, protocol.KindRuleSet,
		protocol.KindProposal, protocol.KindVote, protocol.KindGovDeleg, protocol.KindRevocation, protocol.KindAnchor:
		return true
	}
	return e.extraKinds[k]
}

// rejectEvent is the publish policy. It never changes state.
func (e *Engine) rejectEvent(ctx context.Context, ev *nostr.Event) (bool, string) {
	reject, msg := e.checkEvent(ctx, ev)
	res := "accepted"
	if reject {
		res = "rejected"
		e.log.Info("event rejected", "kind", ev.Kind, "id", short(ev.ID), "pubkey", short(ev.PubKey), "reason", msg)
	}
	if reject {
		e.m.event(kindName(ev.Kind), res).Add(1)
	}
	return reject, msg
}

func (e *Engine) checkEvent(ctx context.Context, ev *nostr.Event) (bool, string) {
	now := time.Now()
	reject, msg := e.judge(ctx, ev, now)
	c := e.connFromCtx(ctx)
	if c == nil {
		return reject, msg
	}
	// Rejected events are charged against publish_per_sec; accepted ones get 10x the rate. A client
	// that keeps sending bad events is throttled; a provider publishing real signals never notices.
	perSec, burst := e.cfg.Rate.PublishPerSec, e.cfg.Rate.PublishBurst
	b := &c.pub
	if !reject {
		perSec, burst, b = perSec*10, burst*10, &c.pubOK
	}
	if ok, wait := b.allow(now, perSec, burst); !ok {
		e.m.rateLimited("publish").Add(1)
		return true, fmt.Sprintf("rate-limited: at most %.0f %s events/s per connection; retry in %dms", perSec, map[bool]string{true: "rejected", false: "accepted"}[reject], wait.Milliseconds()+1)
	}
	return reject, msg
}

// judge applies the publish policy without touching state or rate limits. Cheap checks come first; the
// ones that may cost a chain read (balances) come last and go through the relay-wide lookup cap.
func (e *Engine) judge(ctx context.Context, ev *nostr.Event, now time.Time) (bool, string) {
	if !e.allowedKind(ev.Kind) {
		return true, fmt.Sprintf("blocked: this relay does not accept kind %d; accepted kinds are %v", ev.Kind, e.acceptedKinds())
	}
	limit := maxOtherContent
	if gated(ev.Kind) {
		limit = maxGatedContent
	}
	if ev.Kind == protocol.KindAnchor {
		limit = maxAnchorContent
	}
	if len(ev.Content) > limit {
		return true, fmt.Sprintf("invalid: content is %d bytes, the limit for kind %d is %d", len(ev.Content), ev.Kind, limit)
	}
	if b := e.budgets[ev.Kind]; b != nil {
		if err := e.budgetRoom(ev.Kind); err != nil {
			return true, err.Error()
		}
		// created_at drives retention: a far-future one would be kept for ever.
		if lead := ev.CreatedAt.Time().Sub(now); lead > time.Duration(e.cfg.Limits.Skew)+time.Second {
			return true, fmt.Sprintf("invalid: created_at is %s ahead of the relay clock, limit is %s; fix the publisher's clock", lead.Round(time.Second), time.Duration(e.cfg.Limits.Skew)+time.Second)
		}
	}
	switch ev.Kind {
	case protocol.KindSignal, protocol.KindCommitment:
		if _, err := e.prepareGated(ev, now); err != nil {
			return true, err.Error()
		}
	case protocol.KindRuleSet:
		rs, err := e.dir.CheckRuleSet(ev, now)
		if err != nil {
			return true, "invalid: " + err.Error()
		}
		if err := protocol.CheckRuleSetDelegation(ev, rs, now.Unix()); err != nil {
			return true, "invalid: " + err.Error()
		}
		if e.dir.Revoked(rs.SAAddress, ev.PubKey, now) {
			return true, fmt.Sprintf("invalid: device key %s was revoked by %s", short(ev.PubKey), rs.SAAddress)
		}
		if err := e.requirePower(ctx, rs.SAAddress, e.cfg.Policy.MinProviderPower, "registering a provider"); err != nil {
			return true, err.Error()
		}
	case protocol.KindAllocation:
		if _, err := e.dir.CheckAllocation(ev, now); err != nil {
			return true, prefixed(err.Error(), "invalid")
		}
	case protocol.KindRevocation:
		if _, err := e.dir.CheckRevocation(ev); err != nil {
			return true, prefixed(err.Error(), "invalid")
		}
	case protocol.KindVote:
		// The voter is the delegating SA address. Expiry is judged by the tally against the vote's
		// anchored time, so a late republication of a valid vote is not refused here (at = 0).
		if _, _, err := evmsig.Delegation(ev.Tags, ev.PubKey, 0); err != nil {
			return true, "invalid: a vote needs a valid sa-delegation tag: " + err.Error()
		}
	case protocol.KindProposal:
		addr, _, err := e.delegation(ev.Tags, ev.PubKey, now)
		if err != nil {
			return true, "invalid: a proposal needs a valid sa-delegation tag from the proposing SA address: " + err.Error()
		}
		if err := e.requirePower(ctx, addr, e.cfg.Policy.MinProposerPower, "making a proposal"); err != nil {
			return true, err.Error()
		}
	case protocol.KindGovDeleg:
		if err := protocol.ValidateGovDelegation(ev.Content); err != nil {
			return true, "invalid: " + err.Error()
		}
	case protocol.KindAnchor:
		if err := e.checkAnchor(ctx, ev); err != nil {
			return true, err.Error()
		}
	}
	return false, ""
}

func (e *Engine) acceptedKinds() []int {
	ks := []int{protocol.KindAnchor, protocol.KindSignal, protocol.KindCommitment, protocol.KindProposal, protocol.KindVote, protocol.KindAllocation, protocol.KindGovDeleg, protocol.KindRevocation, protocol.KindRuleSet}
	for k := range e.extraKinds {
		ks = append(ks, k)
	}
	return ks
}

// anchorTarget returns the id named by the single e tag of a kind-1040 attestation.
func anchorTarget(ev *nostr.Event) (string, bool) {
	if ev.Kind != protocol.KindAnchor {
		return "", false
	}
	id, n := "", 0
	for _, t := range ev.Tags {
		if len(t) >= 2 && t[0] == "e" {
			id = t[1]
			n++
		}
	}
	return id, n == 1 && protocol.ValidPubkey(id)
}

// checkAnchor: an attestation must name, through its single e tag, an event this relay holds. Without
// that, 1040 is free storage for anyone, and its existence could not be tied to the release of the
// event it attests (see visibleEvent).
func (e *Engine) checkAnchor(ctx context.Context, ev *nostr.Event) error {
	id, ok := anchorTarget(ev)
	if !ok {
		return fmt.Errorf("invalid: a kind %d attestation needs exactly one [\"e\", <64-hex id of the event it attests>] tag", protocol.KindAnchor)
	}
	ch, err := e.store.QueryEvents(ctx, nostr.Filter{IDs: []string{id}})
	if err != nil {
		return fmt.Errorf("error: cannot look up the attested event %s: %v", short(id), err)
	}
	found := false
	for range ch {
		found = true
	}
	if !found {
		return fmt.Errorf("invalid: the e tag names event %s, which this relay does not hold; publish that event first, then its attestation", short(id))
	}
	return nil
}

// requirePower refuses unless addr holds at least minTokens whole SA tokens. A chain read that fails
// never counts as enough. minTokens <= 0 switches the check off.
func (e *Engine) requirePower(ctx context.Context, addr string, minTokens int64, what string) error {
	if minTokens <= 0 {
		return nil
	}
	need := protocol.TokensToWei(strconv.FormatInt(minTokens, 10))
	total, err := e.power.Total(ctx, addr)
	if total != nil && total.Cmp(need) >= 0 {
		return nil
	}
	if err != nil {
		return fmt.Errorf("blocked: cannot confirm that %s holds the %d SA that %s needs (%v); retry shortly", addr, minTokens, what, err)
	}
	held := new(big.Int).Quo(total, protocol.TokensToWei("1"))
	return fmt.Errorf("blocked: %s needs the SA address to hold at least %d SA; %s holds %s", what, minTokens, addr, held)
}

func prefixed(msg, prefix string) string {
	for i := 0; i < len(msg); i++ {
		if msg[i] == ':' && i > 0 {
			return msg // already "prefix: ..."
		}
		if msg[i] == ' ' {
			break
		}
	}
	return prefix + ": " + msg
}

// resolveProvider maps a publisher to its registered provider (SEP-3.39). The event must carry a valid
// sa-delegation tag (followers verify it, so the relay refuses to store what they would reject), and
// the delegating SA address must have registered a provider by publishing its rule set.
func (e *Engine) resolveProvider(ev *nostr.Event, now time.Time) (*protocol.RuleSet, error) {
	addr, _, err := e.delegation(ev.Tags, ev.PubKey, now)
	if err != nil {
		return nil, fmt.Errorf("blocked: pubkey %s is not a registered provider device key: %v", short(ev.PubKey), err)
	}
	pk, ok := e.dir.ProviderForAddress(addr)
	if !ok {
		return nil, fmt.Errorf("blocked: SA address %s has not registered as a provider; publish a kind %d rule set first", addr, protocol.KindRuleSet)
	}
	rs, ok := e.dir.Provider(pk)
	if !ok {
		return nil, fmt.Errorf("blocked: provider %s has no rule set", short(pk))
	}
	return rs, nil
}

// prepareGated validates a signal/commitment and snapshots the provider's rules into its Meta.
func (e *Engine) prepareGated(ev *nostr.Event, now time.Time) (*state.Meta, error) {
	rs, err := e.resolveProvider(ev, now)
	if err != nil {
		return nil, err
	}
	if err := e.pendingRoom(rs.Provider); err != nil {
		return nil, err
	}
	tol := time.Duration(e.cfg.Limits.Skew) + time.Second
	if d := now.Sub(ev.CreatedAt.Time()); d > tol || -d > tol {
		return nil, fmt.Errorf("invalid: created_at is %s off the relay clock, limit is %s", d.Round(time.Millisecond), tol)
	}
	m := &state.Meta{
		ID: ev.ID, Kind: ev.Kind, Provider: rs.Provider, ProviderAddr: rs.SAAddress,
		Private: rs.Private, Received: now.UnixNano(),
	}
	if ev.Kind == protocol.KindCommitment {
		if _, err := protocol.ParseCommitment(ev.Content); err != nil {
			return nil, fmt.Errorf("invalid: %v", err)
		}
	}
	scale := e.cfg.TimeScale
	if rs.Private {
		m.PrivateMin = rs.PrivateWei.String()
		if ev.Kind == protocol.KindSignal {
			if !protocol.LooksLikeNIP44(ev.Content) {
				return nil, fmt.Errorf("invalid: signals of a private provider must be NIP-44 ciphertext (base64, version 2); this content looks like plaintext and was not stored")
			}
			r, err := protocol.Recipient(ev.Tags)
			if err != nil {
				return nil, fmt.Errorf("invalid: %v", err)
			}
			m.Recipient = r
		}
		frac := protocol.Rat(strconv.FormatFloat(e.cfg.Policy.PrivateCommitmentDelayFraction, 'f', -1, 64))
		m.CommitDelay = int64(protocol.DelayFor(frac, rs.TimeframeSec, scale))
		if ev.Kind == protocol.KindCommitment {
			m.MaxDelay = m.CommitDelay
		}
		return m, nil
	}
	switch ev.Kind {
	case protocol.KindSignal:
		s, err := protocol.ParsePublicSignal(ev.Content)
		if err != nil {
			return nil, fmt.Errorf("invalid: %v", err)
		}
		if s.TimeframeSec != rs.TimeframeSec {
			return nil, fmt.Errorf("invalid: signal timeframe %ds does not match the %ds bound in the provider's rule set; delays are computed from the bound value", s.TimeframeSec, rs.TimeframeSec)
		}
		if d := now.Sub(s.CreatedAt); d > time.Duration(e.cfg.Limits.Skew) || -d > time.Duration(e.cfg.Limits.Skew) {
			return nil, fmt.Errorf("invalid: created_ms is %s off the relay clock, limit is %s", d.Round(time.Millisecond), time.Duration(e.cfg.Limits.Skew))
		}
	}
	for i := range rs.Tiers {
		d := rs.Delay(i, scale)
		m.Tiers = append(m.Tiers, state.MetaTier{MinWei: rs.Tiers[i].MinWei.String(), Delay: int64(d)})
		m.MaxDelay = max(m.MaxDelay, int64(d))
	}
	return m, nil
}

// storeEvent persists accepted events. For signals the receipt time and rule snapshot are written
// BEFORE the event becomes queryable; an event without metadata is invisible. Allocations and
// revocations write their directory record first and the event second, and undo the record if the
// event write fails, so the two never disagree.
func (e *Engine) storeEvent(ctx context.Context, ev *nostr.Event) error {
	now := time.Now()
	switch {
	case gated(ev.Kind):
		e.writeMu.Lock()
		defer e.writeMu.Unlock()
		if e.metaFor(ev.ID) != nil {
			return eventstore.ErrDupEvent // never reset the receipt time of a known event
		}
		m, err := e.prepareGated(ev, now)
		if err != nil {
			return err
		}
		m.Seq = e.recvSeq.Add(1)
		if err := e.store.PutMeta(m); err != nil {
			return fmt.Errorf("error: cannot persist the release schedule: %v", err)
		}
		e.trackPending(m)
		if err := e.store.SaveEvent(ctx, ev); err != nil {
			e.untrackPending(m.ID)
			_ = e.store.DropMeta(m)
			return err
		}
		e.dir.Touch(m.Provider, now)
		return nil
	case ev.Kind == protocol.KindAllocation:
		e.writeMu.Lock()
		defer e.writeMu.Unlock()
		a, err := e.dir.CheckAllocation(ev, now)
		if err != nil {
			return fmt.Errorf("%s", prefixed(err.Error(), "invalid"))
		}
		return e.dir.CommitAllocation(ev, a, now, func() error { return e.saveBudgeted(ctx, ev) })
	case ev.Kind == protocol.KindRevocation:
		e.writeMu.Lock()
		defer e.writeMu.Unlock()
		r, err := e.dir.CheckRevocation(ev)
		if err != nil {
			return fmt.Errorf("%s", prefixed(err.Error(), "invalid"))
		}
		if err := e.budgetRoom(ev.Kind); err != nil {
			return err
		}
		return e.dir.CommitRevocation(ev, r, now, func() error { return e.saveBudgeted(ctx, ev) })
	case e.budgets[ev.Kind] != nil:
		e.writeMu.Lock()
		defer e.writeMu.Unlock()
		if err := e.budgetRoom(ev.Kind); err != nil {
			return err
		}
		return e.saveBudgeted(ctx, ev)
	}
	return e.store.SaveEvent(ctx, ev)
}

// saveBudgeted stores an event and counts it against its kind's budget. Call with writeMu held.
func (e *Engine) saveBudgeted(ctx context.Context, ev *nostr.Event) error {
	if err := e.store.SaveEvent(ctx, ev); err != nil {
		return err
	}
	if b := e.budgets[ev.Kind]; b != nil {
		b.used.Add(1)
	}
	return nil
}

// replaceEvent handles replaceable kinds; today that is the provider rule set.
func (e *Engine) replaceEvent(ctx context.Context, ev *nostr.Event) error {
	if ev.Kind != protocol.KindRuleSet {
		return e.store.ReplaceEvent(ctx, ev)
	}
	e.writeMu.Lock()
	defer e.writeMu.Unlock()
	now := time.Now()
	rs, err := e.dir.CheckRuleSet(ev, now)
	if err != nil {
		return fmt.Errorf("invalid: %v", err)
	}
	return e.dir.CommitRuleSet(ev, rs, now, func() error { return e.store.ReplaceEvent(ctx, ev) })
}

// onSaved arms the releases of a newly stored signal or commitment.
func (e *Engine) onSaved(_ context.Context, ev *nostr.Event) {
	e.m.event(kindName(ev.Kind), "accepted").Add(1)
	if !gated(ev.Kind) {
		e.log.Info("event stored", "kind", ev.Kind, "id", short(ev.ID), "pubkey", short(ev.PubKey))
		e.armAnchor(ev)
		return
	}
	v, ok := e.pending.Load(ev.ID)
	if !ok {
		return
	}
	m := v.(*state.Meta)
	e.log.Info("signal stored", "kind", ev.Kind, "id", short(ev.ID), "provider", short(m.Provider), "private", m.Private, "max_delay", time.Duration(m.MaxDelay).String())
	e.arm(ev, m)
}
