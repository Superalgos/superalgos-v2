package relay_test

import (
	"context"
	"time"

	"github.com/superalgos/sarelay/internal/config"
)

// Options and hooks for the hardening tests. Everything that touches a config key or an Engine method
// introduced by the security review lives in this one file.

func withMinProviderPower(n int64) opt {
	return func(c *config.Config) { c.Policy.MinProviderPower = n }
}
func withMinProposerPower(n int64) opt {
	return func(c *config.Config) { c.Policy.MinProposerPower = n }
}
func withMaxProviders(n int) opt { return func(c *config.Config) { c.Limits.MaxProviders = n } }
func withMinAllocInterval(d time.Duration) opt {
	return func(c *config.Config) { c.Limits.MinAllocInterval = config.Duration(d) }
}
func withPendingCaps(perProvider, total int) opt {
	return func(c *config.Config) { c.Limits.MaxPendingPerProvider, c.Limits.MaxPendingTotal = perProvider, total }
}
func withMaxScan(n int) opt { return func(c *config.Config) { c.Limits.MaxScanEvents = n } }
func withMaxConnsPerIP(n int) opt {
	return func(c *config.Config) { c.Limits.MaxConnsPerIP = n }
}
func withBudgets(anchor, proposal, vote, govdeleg, revocation int) opt {
	return func(c *config.Config) {
		c.Budget.MaxAnchor, c.Budget.MaxProposal, c.Budget.MaxVote = anchor, proposal, vote
		c.Budget.MaxGovDelegation, c.Budget.MaxRevocation = govdeleg, revocation
	}
}
func withRetention(d time.Duration) opt {
	return func(c *config.Config) { c.Budget.Retention = config.Duration(d) }
}
func withIdleExpiry(d time.Duration) opt {
	return func(c *config.Config) { c.Policy.ProviderIdleExpiry = config.Duration(d) }
}

// sweepRetention runs the retention sweep as of now and returns how many events it deleted.
func (r *testRelay) sweepRetention(now time.Time) int { return r.eng.SweepRetention(now) }

// sweepProviders runs the provider upkeep sweep (idle expiry, power re-check) as of now.
func (r *testRelay) sweepProviders(now time.Time) int {
	return r.eng.SweepProviders(context.Background(), now)
}
