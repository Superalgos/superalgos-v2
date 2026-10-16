# Superalgos Enhancement Proposals (SEPs)

A SEP is the unit of specification for Superalgos v2, in the spirit of BIPs, NIPs and PEPs. One SEP describes one feature: why it exists (motivation), how it works (design), and where it stands (status). The project's scope and principles live in [SEP-0](sep-0000.md).

## Criteria

Each SEP lists criteria numbered `SEP-N.k`. A criterion is a testable statement of done, and each carries an indented `Probe:` line naming the check that proves it: the kind of probe, what is run, the pass condition, and the tool.

```
- [ ] SEP-2.1: A user signs in with an EIP-4361 message ...
  Probe: test: SIWE login round trip. Pass: session = recovered address. Tool: pytest.
```

- A checked box (`[x]`) means the probe was run and passed. A short evidence phrase (a test name or command) may follow.
- Container criteria group sub-criteria; they hold when every sub-criterion listed in their probe line holds.
- Anti-criteria start with `Anti:` and describe something that must never happen. Their tests stay forever.
- Code, tests and docs cite criteria by id, for example `SEP-16.2`. Ids are stable; new criteria are appended.

## Statuses

- **Draft**: proposed, no criterion verified yet.
- **Accepted**: agreed direction; some criteria verified, work continues.
- **Implemented**: every criterion verified.
- **Rejected**: considered and declined; kept for the record with the reason.

## Changing a SEP

SEPs change by pull request, in the same pull request as the code they describe. A new feature starts as a SEP with unchecked criteria and probes, reviewed before building. A criterion is closed only by evidence from a tool run.

## Index

| SEP | Title | Status | Criteria done/total |
|---|---|---|---|
| [0](sep-0000.md) | Project scope and principles | Accepted | n/a |
| [1](sep-0001.md) | Cross-cutting | Accepted | 8/16 |
| [2](sep-0002.md) | SA identity | Accepted | 4/7 |
| [3](sep-0003.md) | Verifiable signals | Accepted | 36/44 |
| [4](sep-0004.md) | Strategy authoring and backtest | Accepted | 3/7 |
| [5](sep-0005.md) | Visual designer | Implemented | 17/17 |
| [6](sep-0006.md) | More markets | Draft | 0/4 |
| [7](sep-0007.md) | Community and governance | Draft | 0/8 |
| [8](sep-0008.md) | Market chart | Implemented | 7/7 |
| [9](sep-0009.md) | Portfolio management | Draft | 0/0 |
| [10](sep-0010.md) | Contribution scoring (spec only, no payouts) | Draft | 0/4 |
| [11](sep-0011.md) | Mobile steering app | Draft | 0/4 |
| [12](sep-0012.md) | Guided install and setup (B2C) | Accepted | 5/23 |
| [13](sep-0013.md) | Indicators (data mining, v1 parity) | Draft | 0/6 |
| [14](sep-0014.md) | Follower risk controls | Draft | 0/4 |
| [15](sep-0015.md) | Explain and notify | Draft | 0/4 |
| [16](sep-0016.md) | Documentation | Draft | 0/6 |
| [17](sep-0017.md) | Discovery (relays and providers) | Draft | 0/6 |
| [18](sep-0018.md) | Signal tracing and logs | Draft | 0/8 |
