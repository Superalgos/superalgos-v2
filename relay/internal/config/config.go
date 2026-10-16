// Package config loads and validates the sarelay TOML configuration.
package config

import (
	"bytes"
	"errors"
	"fmt"
	"net"
	"os"
	"strings"
	"time"

	"github.com/pelletier/go-toml/v2"
)

// Duration is a time.Duration written as a string ("168h", "5s") in TOML.
type Duration time.Duration

// UnmarshalText parses a Go duration string.
func (d *Duration) UnmarshalText(b []byte) error {
	v, err := time.ParseDuration(string(b))
	if err != nil {
		return fmt.Errorf("%q is not a duration like 5s, 10m or 168h", string(b))
	}
	*d = Duration(v)
	return nil
}

// MarshalText renders the duration.
func (d Duration) MarshalText() ([]byte, error) { return []byte(time.Duration(d).String()), nil }

// D converts to time.Duration.
func (d Duration) D() time.Duration { return time.Duration(d) }

// Chain is one entry of the chain registry.
type Chain struct {
	ID       string `toml:"id"`
	Kind     string `toml:"kind"` // "evm" or "fixture"
	RPC      string `toml:"rpc"`
	Contract string `toml:"contract"`
	Decimals int    `toml:"decimals"`
	File     string `toml:"file"` // fixture: JSON {address: tokens}
	Disabled bool   `toml:"disabled"`
}

// Config is the whole relay configuration.
type Config struct {
	Listen          string   `toml:"listen"`
	PublicURL       string   `toml:"public_url"`
	StoragePath     string   `toml:"storage_path"`
	AllocationDelay Duration `toml:"allocation_delay"` // D as the relay applies it, counted from relay receipt
	// CoreDelay is the D that sa-core applies, counted from the allocation's anchored time. The relay
	// cannot see anchor proofs, so it starts its clock at receipt; MaxAnchorLatency is the longest an
	// anchor proof can lag the receipt. AllocationDelay must cover both (SEP-3.35), so the relay is never
	// earlier than sa-core.
	CoreDelay        Duration `toml:"core_delay"`
	MaxAnchorLatency Duration `toml:"max_anchor_latency"`
	TimeScale        float64  `toml:"time_scale"`

	Log struct {
		Level  string `toml:"level"`
		Format string `toml:"format"`
	} `toml:"log"`

	Admin struct {
		Listen    string `toml:"listen"`
		TokenFile string `toml:"token_file"`
	} `toml:"admin"`

	Limits struct {
		Skew             Duration `toml:"skew"`
		MinTimeframe     int64    `toml:"min_timeframe_seconds"`
		MaxTimeframe     int64    `toml:"max_timeframe_seconds"`
		MaxDelayFraction float64  `toml:"max_delay_fraction"`
		MaxProviders     int      `toml:"max_providers"`
		MinAllocInterval Duration `toml:"min_allocation_interval"`
		MaxMessageBytes  int64    `toml:"max_message_bytes"`
		// MaxReleaseDelay caps delay_fraction x timeframe of every tier of a rule set (and the private
		// commitment delay): a release timer never outlives it.
		MaxReleaseDelay Duration `toml:"max_release_delay"`
		// MaxPendingPerProvider and MaxPendingTotal cap events whose releases are still scheduled.
		MaxPendingPerProvider int `toml:"max_pending_per_provider"`
		MaxPendingTotal       int `toml:"max_pending_total"`
		// MaxScanEvents caps how many stored events one REQ or COUNT may examine.
		MaxScanEvents int `toml:"max_scan_events"`
		// MaxConnsPerIP caps simultaneous websockets per client address (in-process; 0 disables).
		MaxConnsPerIP int `toml:"max_conns_per_ip"`
		// ForwardedHeader names the header a trusted reverse proxy sets (e.g. "X-Forwarded-For"); the
		// right-most value is the client address. Empty: use the TCP peer address.
		ForwardedHeader string `toml:"forwarded_header"`
	} `toml:"limits"`

	// Budget bounds what the kinds that anyone may publish can occupy in storage.
	Budget struct {
		MaxAnchor        int      `toml:"max_anchor_events"`        // kind 1040
		MaxProposal      int      `toml:"max_proposal_events"`      // kind 9330
		MaxVote          int      `toml:"max_vote_events"`          // kind 9331
		MaxGovDelegation int      `toml:"max_govdelegation_events"` // kind 9333
		MaxRevocation    int      `toml:"max_revocation_events"`    // kind 9334
		Retention        Duration `toml:"retention"`                // 1040, 9330, 9331 older than this are deleted; 0s keeps them
	} `toml:"budget"`

	Rate struct {
		ZeroPowerPerSec float64 `toml:"zero_power_per_sec"`
		ZeroPowerBurst  float64 `toml:"zero_power_burst"`
		PoweredPerSec   float64 `toml:"powered_per_sec"`
		PoweredBurst    float64 `toml:"powered_burst"`
		PublishPerSec   float64 `toml:"publish_per_sec"`
		PublishBurst    float64 `toml:"publish_burst"`
	} `toml:"rate"`

	Oracle struct {
		CacheTTL Duration `toml:"cache_ttl"`
		MaxStale Duration `toml:"max_stale"`
		// MaxLookupsPerSec and LookupBurst cap chain reads for the whole relay (one read per chain per address).
		MaxLookupsPerSec float64 `toml:"max_lookups_per_sec"`
		LookupBurst      float64 `toml:"lookup_burst"`
		// BackoffBase and BackoffMax shape the exponential back-off on a chain that keeps failing.
		BackoffBase Duration `toml:"backoff_base"`
		BackoffMax  Duration `toml:"backoff_max"`
	} `toml:"oracle"`

	Policy struct {
		PrivateCommitmentDelayFraction float64 `toml:"private_commitment_delay_fraction"`
		ExtraKinds                     []int   `toml:"extra_kinds"`
		// MinProviderPower (whole SA tokens) is what a provider's SA address must hold to register or
		// keep a rule set; MinProposerPower is the same for kind 9330 proposals. 0 disables the check.
		MinProviderPower int64 `toml:"min_provider_power"`
		MinProposerPower int64 `toml:"min_proposer_power"`
		// ProviderIdleExpiry removes providers that published no signal for this long; 0s never.
		ProviderIdleExpiry Duration `toml:"provider_idle_expiry"`
		// ProviderRecheck is how often provider balances and idleness are re-examined.
		ProviderRecheck Duration `toml:"provider_recheck_interval"`
	} `toml:"policy"`

	Chains []Chain `toml:"chains"`
}

// Default returns the production defaults.
func Default() Config {
	var c Config
	c.Listen = "127.0.0.1:7781"
	c.StoragePath = "./sarelay-data"
	c.CoreDelay = Duration(7 * 24 * time.Hour)
	c.MaxAnchorLatency = Duration(24 * time.Hour) // OpenTimestamps takes hours to reach Bitcoin
	c.AllocationDelay = c.CoreDelay + c.MaxAnchorLatency
	c.TimeScale = 1
	c.Log.Level, c.Log.Format = "info", "json"
	c.Admin.Listen = "127.0.0.1:7782"
	c.Limits.Skew = Duration(10 * time.Second)
	c.Limits.MinTimeframe, c.Limits.MaxTimeframe = 60, 7*24*3600
	c.Limits.MaxDelayFraction = 100
	c.Limits.MaxProviders = 1000
	c.Limits.MinAllocInterval = Duration(10 * time.Second)
	c.Limits.MaxMessageBytes = 128 * 1024
	c.Limits.MaxReleaseDelay = Duration(7 * 24 * time.Hour)
	c.Limits.MaxPendingPerProvider, c.Limits.MaxPendingTotal = 5000, 100000
	c.Limits.MaxScanEvents = 20000
	c.Limits.MaxConnsPerIP = 64
	c.Budget.MaxAnchor, c.Budget.MaxProposal, c.Budget.MaxVote = 50000, 5000, 50000
	c.Budget.MaxGovDelegation, c.Budget.MaxRevocation = 20000, 20000
	c.Budget.Retention = Duration(90 * 24 * time.Hour)
	c.Oracle.MaxLookupsPerSec, c.Oracle.LookupBurst = 20, 40
	c.Oracle.BackoffBase, c.Oracle.BackoffMax = Duration(2*time.Second), Duration(5*time.Minute)
	c.Policy.MinProviderPower, c.Policy.MinProposerPower = 10000, 10000
	c.Policy.ProviderIdleExpiry = Duration(30 * 24 * time.Hour)
	c.Policy.ProviderRecheck = Duration(10 * time.Minute)
	c.Rate.ZeroPowerPerSec, c.Rate.ZeroPowerBurst = 5, 20
	c.Rate.PoweredPerSec, c.Rate.PoweredBurst = 50, 200
	c.Rate.PublishPerSec, c.Rate.PublishBurst = 20, 40
	c.Oracle.CacheTTL = Duration(30 * time.Second)
	c.Oracle.MaxStale = Duration(10 * time.Minute)
	c.Policy.PrivateCommitmentDelayFraction = 1
	return c
}

// Load reads a TOML file over the defaults. Unknown keys are errors.
func Load(path string) (Config, error) {
	c := Default()
	raw, err := os.ReadFile(path)
	if err != nil {
		return c, fmt.Errorf("cannot read config %q: %w; pass -config <file> (see sarelay.example.toml)", path, err)
	}
	return Parse(raw)
}

// Parse decodes TOML bytes over the defaults and validates.
func Parse(raw []byte) (Config, error) {
	c := Default()
	dec := toml.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		var se *toml.StrictMissingError
		if errors.As(err, &se) {
			return c, fmt.Errorf("config has unknown keys:\n%s", se.String())
		}
		return c, fmt.Errorf("config is not valid TOML: %w", err)
	}
	return c, c.Validate()
}

// Validate checks the configuration for values the relay cannot run with.
func (c *Config) Validate() error {
	var errs []error
	add := func(f string, a ...any) { errs = append(errs, fmt.Errorf(f, a...)) }
	if _, _, err := net.SplitHostPort(c.Listen); err != nil {
		add("listen %q must be host:port: %v", c.Listen, err)
	}
	if c.Admin.Listen != "" {
		if _, _, err := net.SplitHostPort(c.Admin.Listen); err != nil {
			add("admin.listen %q must be host:port or empty to disable: %v", c.Admin.Listen, err)
		}
	}
	if c.StoragePath == "" {
		add("storage_path is empty; set a directory for the persistent store")
	}
	if c.AllocationDelay < 0 {
		add("allocation_delay %s must be >= 0 (0 is allowed for demos)", c.AllocationDelay.D())
	}
	if c.CoreDelay < 0 || c.MaxAnchorLatency < 0 {
		add("core_delay %s and max_anchor_latency %s must be >= 0", c.CoreDelay.D(), c.MaxAnchorLatency.D())
	}
	if need := c.CoreDelay + c.MaxAnchorLatency; c.AllocationDelay < need {
		add("allocation_delay %s is shorter than core_delay %s + max_anchor_latency %s = %s: the relay would count an allocation before sa-core does; raise allocation_delay to at least %s (for a demo set core_delay and max_anchor_latency to \"0s\" as well)",
			c.AllocationDelay.D(), c.CoreDelay.D(), c.MaxAnchorLatency.D(), need.D(), need.D())
	}
	if c.TimeScale <= 0 {
		add("time_scale %v must be > 0 (1.0 in production; tests use 1/60)", c.TimeScale)
	}
	switch strings.ToLower(c.Log.Level) {
	case "debug", "info", "warn", "error":
	default:
		add("log.level %q must be debug, info, warn or error", c.Log.Level)
	}
	if c.Log.Format != "json" && c.Log.Format != "text" {
		add("log.format %q must be json or text", c.Log.Format)
	}
	if c.Limits.MinTimeframe <= 0 || c.Limits.MaxTimeframe < c.Limits.MinTimeframe {
		add("limits timeframes %d..%d seconds are not a valid range", c.Limits.MinTimeframe, c.Limits.MaxTimeframe)
	}
	if c.Limits.Skew < 0 {
		add("limits.skew must be >= 0")
	}
	if c.Rate.ZeroPowerPerSec <= 0 || c.Rate.PoweredPerSec <= 0 || c.Rate.PublishPerSec <= 0 ||
		c.Rate.ZeroPowerBurst < 1 || c.Rate.PoweredBurst < 1 || c.Rate.PublishBurst < 1 {
		add("rate limits need per_sec > 0 and burst >= 1")
	}
	if c.Policy.PrivateCommitmentDelayFraction < 0 {
		add("policy.private_commitment_delay_fraction must be >= 0")
	}
	if c.Limits.MaxReleaseDelay <= 0 {
		add("limits.max_release_delay %s must be > 0 (default 168h)", c.Limits.MaxReleaseDelay.D())
	}
	if c.Limits.MaxPendingPerProvider < 1 || c.Limits.MaxPendingTotal < 1 || c.Limits.MaxScanEvents < 1 {
		add("limits.max_pending_per_provider (%d), max_pending_total (%d) and max_scan_events (%d) must be >= 1; raise them instead of disabling", c.Limits.MaxPendingPerProvider, c.Limits.MaxPendingTotal, c.Limits.MaxScanEvents)
	}
	if c.Limits.MaxConnsPerIP < 0 {
		add("limits.max_conns_per_ip %d must be >= 0 (0 disables)", c.Limits.MaxConnsPerIP)
	}
	if c.Budget.MaxAnchor < 1 || c.Budget.MaxProposal < 1 || c.Budget.MaxVote < 1 || c.Budget.MaxGovDelegation < 1 || c.Budget.MaxRevocation < 1 {
		add("every [budget] max_*_events must be >= 1; raise the budget instead of disabling it")
	}
	if c.Budget.Retention < 0 || c.Policy.ProviderIdleExpiry < 0 {
		add("budget.retention and policy.provider_idle_expiry must be >= 0 (0s keeps forever)")
	}
	if c.Policy.ProviderRecheck <= 0 {
		add("policy.provider_recheck_interval %s must be > 0", c.Policy.ProviderRecheck.D())
	}
	if c.Policy.MinProviderPower < 0 || c.Policy.MinProposerPower < 0 {
		add("policy.min_provider_power and min_proposer_power must be >= 0 (whole SA tokens; 0 disables the check)")
	}
	if c.Oracle.MaxLookupsPerSec <= 0 || c.Oracle.LookupBurst < 1 || c.Oracle.BackoffBase <= 0 || c.Oracle.BackoffMax < c.Oracle.BackoffBase {
		add("oracle.max_lookups_per_sec must be > 0, lookup_burst >= 1 and 0 < backoff_base <= backoff_max")
	}
	if frac := c.Policy.PrivateCommitmentDelayFraction; frac*float64(c.Limits.MaxTimeframe) > c.Limits.MaxReleaseDelay.D().Seconds() {
		add("policy.private_commitment_delay_fraction %v x max_timeframe_seconds %d exceeds limits.max_release_delay %s; lower one of them", frac, c.Limits.MaxTimeframe, c.Limits.MaxReleaseDelay.D())
	}
	if len(c.Chains) == 0 {
		add("no [[chains]] configured; add at least one (kind = \"evm\" or \"fixture\")")
	}
	seen := map[string]bool{}
	live := 0
	for i, ch := range c.Chains {
		if ch.ID == "" || seen[ch.ID] {
			add("chains[%d] id %q must be non-empty and unique", i, ch.ID)
		}
		seen[ch.ID] = true
		if ch.Disabled {
			continue
		}
		live++
		switch ch.Kind {
		case "evm":
			if ch.RPC == "" || len(ch.Contract) != 42 || ch.Decimals < 0 || ch.Decimals > 36 {
				add("chains[%d] %q (evm) needs rpc, a 0x contract address and decimals 0..36", i, ch.ID)
			}
		case "fixture":
			if ch.File == "" {
				add("chains[%d] %q (fixture) needs file = path to a JSON {address: tokens}", i, ch.ID)
			}
		default:
			add("chains[%d] %q kind %q must be evm or fixture", i, ch.ID, ch.Kind)
		}
	}
	if live == 0 && len(c.Chains) > 0 {
		add("every chain is disabled; enable at least one")
	}
	return errors.Join(errs...)
}
