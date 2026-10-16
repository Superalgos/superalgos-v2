package config

import (
	"strings"
	"testing"
	"time"
)

const minimal = `
[[chains]]
id = "fixture"
kind = "fixture"
file = "balances.json"
`

func TestDefaultsAndOverrides(t *testing.T) {
	c, err := Parse([]byte(minimal + `
listen = "0.0.0.0:9000"
`))
	// keys after a table header belong to that table: this must not set the top-level listen
	if err == nil && c.Listen == "0.0.0.0:9000" {
		t.Fatal("top-level key after [[chains]] was applied to the root")
	}
	c, err = Parse([]byte(`
listen = "127.0.0.1:9000"
allocation_delay = "0s"
core_delay = "0s"
max_anchor_latency = "0s"
time_scale = 0.0166667
` + minimal + `
[limits]
skew = "3s"
`))
	if err != nil {
		t.Fatal(err)
	}
	if c.Listen != "127.0.0.1:9000" || c.AllocationDelay.D() != 0 || c.Limits.Skew.D() != 3*time.Second || c.TimeScale != 0.0166667 {
		t.Errorf("%+v", c)
	}
	d := Default()
	if d.AllocationDelay.D() != 7*24*time.Hour+24*time.Hour || d.Admin.Listen != "127.0.0.1:7782" || d.Listen != "127.0.0.1:7781" {
		t.Errorf("defaults changed: %+v", d)
	}
}

// SEP-3.35: sa-core counts D from an allocation's anchored time, the relay from receipt. The anchor proof
// can only lag the receipt, so the relay's D must cover sa-core's D plus the longest anchor latency;
// otherwise the relay would apply an allocation before sa-core does.
func TestRelayDelayIsNeverEarlierThanSaCore(t *testing.T) {
	d := Default()
	if d.AllocationDelay < d.CoreDelay+d.MaxAnchorLatency {
		t.Fatalf("default D %s is earlier than core D %s + anchor latency %s", d.AllocationDelay.D(), d.CoreDelay.D(), d.MaxAnchorLatency.D())
	}
	for _, c := range []struct {
		name, toml string
		ok         bool
	}{
		{"defaults", ``, true},
		{"exactly core + latency", "core_delay = \"168h\"\nmax_anchor_latency = \"2h\"\nallocation_delay = \"170h\"\n", true},
		{"one nanosecond short", "core_delay = \"168h\"\nmax_anchor_latency = \"2h\"\nallocation_delay = \"169h59m59.999999999s\"\n", false},
		{"sa-core D alone (the old default)", "allocation_delay = \"168h\"\n", false},
		{"demo opts out explicitly", "allocation_delay = \"0s\"\ncore_delay = \"0s\"\nmax_anchor_latency = \"0s\"\n", true},
		{"demo forgets core_delay", "allocation_delay = \"0s\"\nmax_anchor_latency = \"0s\"\n", false},
	} {
		_, err := Parse([]byte(c.toml + minimal))
		if c.ok != (err == nil) {
			t.Errorf("%s: err=%v, want ok=%v", c.name, err, c.ok)
		}
		if !c.ok && err != nil && !strings.Contains(err.Error(), "before sa-core does") {
			t.Errorf("%s: error should say why: %v", c.name, err)
		}
	}
}

// SEP-2.6: the shipped example config reads SA on BSC, Ethereum and zkSync, so the relay and sa-core
// registries agree.
func TestExampleConfigHasAllThreeChains(t *testing.T) {
	c, err := Load("../../sarelay.example.toml")
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"bsc":      "0xfb981ed9a92377ca4d75d924b9ca06df163924fd",
		"ethereum": "0xc17272c3e15074c55b810bceba02ba0c4481cd79",
		"zksync":   "0xe3d85bd3363b6f0f91351591f9a8fd0d0a1145ed", // chain 324
	}
	for _, ch := range c.Chains {
		if want[ch.ID] == "" {
			t.Errorf("unexpected chain %q", ch.ID)
			continue
		}
		if ch.Disabled || ch.Kind != "evm" || ch.Contract != want[ch.ID] || ch.Decimals != 18 {
			t.Errorf("chain %s: %+v", ch.ID, ch)
		}
		delete(want, ch.ID)
	}
	if len(want) != 0 {
		t.Errorf("example config lacks chains %v", want)
	}
}

func TestValidationMessages(t *testing.T) {
	for name, tc := range map[string]struct{ toml, sub string }{
		"no chains":     {``, "no [[chains]]"},
		"unknown key":   {"bogus = 1\n" + minimal, "unknown keys"},
		"bad duration":  {"allocation_delay = \"soon\"\n" + minimal, "not a duration"},
		"bad scale":     {"time_scale = 0\n" + minimal, "time_scale"},
		"bad listen":    {"listen = \"nope\"\n" + minimal, "host:port"},
		"negative D":    {"allocation_delay = \"-1s\"\n" + minimal, ">= 0"},
		"evm no rpc":    {"[[chains]]\nid=\"bsc\"\nkind=\"evm\"\n", "needs rpc"},
		"dup chain":     {minimal + minimal, "unique"},
		"unknown kind":  {"[[chains]]\nid=\"x\"\nkind=\"btc\"\n", "evm or fixture"},
		"all disabled":  {"[[chains]]\nid=\"x\"\nkind=\"fixture\"\nfile=\"f\"\ndisabled=true\n", "disabled"},
		"bad log level": {"[log]\nlevel=\"loud\"\n" + minimal, "log.level"},
		"bad rate":      {"[rate]\nzero_power_per_sec=0\n" + minimal, "rate limits"},
	} {
		_, err := Parse([]byte(tc.toml))
		if err == nil || !strings.Contains(err.Error(), tc.sub) {
			t.Errorf("%s: err=%v, want it to mention %q", name, err, tc.sub)
		}
	}
}

func TestEVMChain(t *testing.T) {
	_, err := Parse([]byte(`
[[chains]]
id = "bsc"
kind = "evm"
rpc = "https://bsc-dataseed.binance.org"
contract = "0xfb981ed9a92377ca4d75d924b9ca06df163924fd"
decimals = 18
`))
	if err != nil {
		t.Fatal(err)
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := Load("/nonexistent/sarelay.toml"); err == nil || !strings.Contains(err.Error(), "-config") {
		t.Errorf("%v", err)
	}
}

// The caps added after the security review have bounded defaults and refuse values that disable them.
func TestHardeningKeysHaveBoundedDefaultsAndValidate(t *testing.T) {
	c, err := Parse([]byte(minimal))
	if err != nil {
		t.Fatal(err)
	}
	if c.Limits.MaxReleaseDelay.D() != 7*24*time.Hour || c.Limits.MaxPendingPerProvider < 1 || c.Limits.MaxPendingTotal < c.Limits.MaxPendingPerProvider ||
		c.Limits.MaxScanEvents < 1 || c.Limits.MaxConnsPerIP < 1 || c.Policy.MinProviderPower < 1 || c.Policy.MinProposerPower < 1 ||
		c.Oracle.MaxLookupsPerSec <= 0 || c.Budget.MaxAnchor < 1 || c.Budget.Retention.D() <= 0 || c.Policy.ProviderIdleExpiry.D() <= 0 {
		t.Errorf("a cap is unbounded by default: %+v", c)
	}
	for name, body := range map[string]string{
		"max_release_delay":          "[limits]\nmax_release_delay = \"0s\"\n",
		"max_pending_per_provider":   "[limits]\nmax_pending_per_provider = 0\n",
		"max_scan_events":            "[limits]\nmax_scan_events = 0\n",
		"max_conns_per_ip":           "[limits]\nmax_conns_per_ip = -1\n",
		"max_anchor_events":          "[budget]\nmax_anchor_events = 0\n",
		"min_provider_power":         "[policy]\nmin_provider_power = -5\n",
		"max_lookups_per_sec":        "[oracle]\nmax_lookups_per_sec = 0\n",
		"backoff_max":                "[oracle]\nbackoff_base = \"10s\"\nbackoff_max = \"1s\"\n",
		"private commitment x frame": "[policy]\nprivate_commitment_delay_fraction = 2\n",
	} {
		if _, err := Parse([]byte(minimal + body)); err == nil {
			t.Errorf("%s: an invalid value was accepted", name)
		}
	}
	// 0 is allowed where the brief says so (demos): the registration costs
	if _, err := Parse([]byte(minimal + "[policy]\nmin_provider_power = 0\nmin_proposer_power = 0\nprovider_idle_expiry = \"0s\"\n")); err != nil {
		t.Errorf("demo values refused: %v", err)
	}
}
