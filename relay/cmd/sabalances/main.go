// sabalances reads SA balances through the relay's own oracle code (internal/chains) and the relay's
// chain registry, and prints them as JSON. It exists so the cross-implementation check (SEP-2.6) can
// compare the Go oracle with the Python one on the same addresses; it is read-only (eth_call).
//
//	sabalances -config sarelay.example.toml 0xaddr...
//
// Output: {"0xaddr": {"per_chain": {"bsc": "wei", ...}, "total": "wei"}}, all amounts in 18-decimal units.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/superalgos/sarelay/internal/chains"
	"github.com/superalgos/sarelay/internal/config"
)

type result struct {
	PerChain map[string]string `json:"per_chain"`
	Total    string            `json:"total"`
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "sabalances:", err)
		os.Exit(1)
	}
}

func run() error {
	cfgPath := flag.String("config", "sarelay.toml", "relay TOML configuration (its [[chains]] are read)")
	flag.Parse()
	if flag.NArg() == 0 {
		return fmt.Errorf("give at least one 0x address to look up")
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		return err
	}
	var cs []chains.Chain
	for _, c := range cfg.Chains {
		if c.Disabled {
			continue
		}
		switch c.Kind {
		case "evm":
			cs = append(cs, chains.Chain{ID: c.ID, Oracle: chains.EVM{RPCURL: c.RPC, Contract: c.Contract, Decimals: c.Decimals}})
		case "fixture":
			cs = append(cs, chains.Chain{ID: c.ID, Oracle: chains.Fixture{Path: c.File}})
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	power := &chains.Power{Chains: cs, TTL: 0, MaxStale: 0}
	out := map[string]result{}
	for _, addr := range flag.Args() {
		r := result{PerChain: map[string]string{}}
		for _, c := range cs {
			b, err := c.Oracle.Balance(ctx, addr)
			if err != nil {
				return fmt.Errorf("chain %s, address %s: %w", c.ID, addr, err)
			}
			r.PerChain[c.ID] = b.String()
		}
		// the total comes from the relay's own summation path (Power.Total), not from the loop above
		tot, err := power.Total(ctx, addr)
		if err != nil {
			return fmt.Errorf("power total for %s: %w", addr, err)
		}
		r.Total = tot.String()
		out[addr] = r
	}
	return json.NewEncoder(os.Stdout).Encode(out)
}
