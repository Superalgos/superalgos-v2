# Security policy

Superalgos v2 signs signals, verifies delegations, holds encrypted keys and places orders on users' own accounts. A
security bug here can cost someone money, so please report it privately.

## How to report

Use GitHub's private reporting: open the **Security** tab of this repository and click **Report a vulnerability**.
Do not open a public issue, pull request or discussion for a security problem.

Please include:

- what an attacker can do, and what they need first (network position, a key, a config value);
- the component: relay, provider or follower bot, key storage, signing and delegation, track record, API or dashboard;
- steps or a proof of concept against `sa demo` or a local setup, never against someone else's machine or account;
- the commit you tested.

Never include real private keys, passphrases or exchange API keys, yours or anyone else's.

## What is in scope

Anything that breaks the invariants in [`AGENTS.md`](AGENTS.md), for example:

- a key, passphrase or exchange secret leaving the user's machine or written in plaintext;
- a signal accepted with a forged, expired or revoked delegation, or a track record that can be falsified;
- a relay releasing a signal to a tier before its time, or a private signal to someone not entitled;
- an order placed from a signal that failed verification or was received only as history;
- remote code execution, or a write path in the read-only API.

## What to expect

This is a volunteer project. We acknowledge reports as soon as we can, work on a fix in private, and credit you in
the advisory unless you prefer otherwise. There is no paid bounty. Only the latest `main` is supported.
