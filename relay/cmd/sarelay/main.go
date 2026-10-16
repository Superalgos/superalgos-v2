// sarelay is the Superalgos v2 tiered Nostr relay. See relay/README.md.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/superalgos/sarelay/internal/config"
	"github.com/superalgos/sarelay/internal/relay"
)

var version = "0.1.0"

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "sarelay:", err)
		os.Exit(1)
	}
}

func run() error {
	cfgPath := flag.String("config", "sarelay.toml", "path to the TOML configuration")
	check := flag.Bool("check", false, "validate the configuration and exit")
	showVersion := flag.Bool("version", false, "print the version and exit")
	flag.Parse()
	if *showVersion {
		fmt.Println("sarelay", version)
		return nil
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		return err
	}
	if *check {
		fmt.Println("config ok")
		return nil
	}
	log := newLogger(cfg)
	if cfg.TimeScale != 1 {
		log.Warn("time_scale is not 1: every tier delay is multiplied by it. Use 1 in production; other values are for tests", "time_scale", cfg.TimeScale)
	}
	eng, err := relay.New(cfg, log)
	if err != nil {
		return err
	}
	defer eng.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := eng.Start(ctx); err != nil {
		return err
	}
	relayLn, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return fmt.Errorf("cannot listen on %s: %w; change listen in the config or stop the other process", cfg.Listen, err)
	}
	var adminLn net.Listener
	var token string
	if cfg.Admin.Listen != "" {
		if token, err = relay.AdminToken(cfg.Admin.TokenFile, cfg.StoragePath); err != nil {
			return err
		}
		if adminLn, err = net.Listen("tcp", cfg.Admin.Listen); err != nil {
			return fmt.Errorf("cannot listen on admin address %s: %w", cfg.Admin.Listen, err)
		}
		if host, _, _ := net.SplitHostPort(cfg.Admin.Listen); !isLoopback(host) {
			log.Warn("admin endpoints are bound to a non-loopback address; they still require the bearer token", "listen", cfg.Admin.Listen)
		}
	}
	log.Info("sarelay listening", "version", version, "relay", relayLn.Addr().String(), "admin", cfg.Admin.Listen,
		"storage", cfg.StoragePath, "allocation_delay", cfg.AllocationDelay.D().String(), "time_scale", cfg.TimeScale, "chains", len(cfg.Chains))
	return eng.Serve(ctx, relayLn, adminLn, token)
}

func isLoopback(host string) bool {
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func newLogger(cfg config.Config) *slog.Logger {
	var lvl slog.Level
	_ = lvl.UnmarshalText([]byte(strings.ToUpper(cfg.Log.Level)))
	opts := &slog.HandlerOptions{Level: lvl}
	if cfg.Log.Format == "text" {
		return slog.New(slog.NewTextHandler(os.Stderr, opts))
	}
	return slog.New(slog.NewJSONHandler(os.Stderr, opts))
}
