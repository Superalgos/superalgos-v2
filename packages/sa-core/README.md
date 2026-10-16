# sa-core

`import sa_core`: the verification core of Superalgos v2. Models, keys, Nostr, delegation, chains,
power, anchors, bundles, track record and governance. Python 3.12+, Pydantic v2, `mypy --strict`
clean. No network at import; network code is opt-in and its tests need `SA_ONLINE=1`.
Python 3.12 or 3.13 (`.python-version` pins 3.13; the range is `>=3.12,<3.14` in every package so
NautilusTrader 1.231 installs next to sa-core).

```
uv sync --all-packages
uv run pytest packages/sa-core --cov=sa_core      # from the repo root, or: make test
```

## Module map

| Module | What it does |
|---|---|
| `models` | Pydantic models: `Signal`, `Commitment`, `Delegation`, `Revocation`, `AnchorProof`, `PowerAllocation`, `Tier`, `ProviderRuleSet`, `Proposal`, `Vote`, `GovDelegation`, `Reveal`, `Bundle`, `NostrEvent`, kind constants. Frozen, `extra=forbid`, JSON round trip is lossless. |
| `keys` | `DeviceKey` (BIP-340), `SAKey` (EVM). Encrypted files only: scrypt + ChaCha20-Poly1305, header authenticated, mode 0600, no overwrite. |
| `nostr` | NIP-01 build/verify, NIP-44 v2 encrypt/decrypt (conversation key is the HKDF PRK; message keys are expand-only with the nonce as info), NIP-42 AUTH event, `RelayClient` (publish, query, subscribe, auto AUTH). |
| `delegation` | EIP-191 `sa-delegation:{pubkey}:{expires_at}` sign/verify, tag codec, library-level revocation. |
| `signals` | Build/verify kind 9317 (public and NIP-44 private) and 9318 commitments, reveals, off-relay text. |
| `chains` (+`chains.toml`) | Registry (BSC, Ethereum, zkSync) and the read-only balance oracle (fixture or JSON-RPC, timeout, retries, TTL cache). |
| `power` | Allocation sign/validate, D-delay, per-provider power, rule sets, tier resolution, release time. |
| `anchors` | `Anchor` protocol, `OtsAnchor`, `BscCommitAnchor`, `SimChain`, `JsonRpcChainReader`, `anchored_time()`, `verifier_map()` (kind -> verifier map for `trackrecord.compute` and the tally). |
| `tsa` (+`tsa_trust/`) | `TsaAnchor` (kind `tsa`): RFC 3161 tokens from k organisations over a Merkle root, verified offline against the versioned pinned-roots bundle; `TsaStamper` (batches, >= 15 s per TSA, stops on 429/refusal). |
| `bundle` | Export/import a provider's history; offline completeness check (`check_bundle`). |
| `trackrecord` | `compute(bundle, candles, ...)`: deterministic report. |
| `governance` | Proposal/vote/delegation events, `tally()` (pure), `collect()` / `tally_from_relays()` (relays), relay coverage. |
| `_evm`, `_canon` | Internals: EIP-191 recovery, canonical JSON. |

## Crypto libraries (none hand-written)

| Need | Library |
|---|---|
| BIP-340 Schnorr sign/verify, x-only keys, unhashed ECDH (NIP-44) | `coincurve` 21 (binding to libsecp256k1) |
| EIP-191 signing and address recovery | `eth-account` |
| NIP-44 ChaCha20 | `pycryptodome`; HMAC/HKDF-expand from the standard library `hmac` (RFC 5869 expand, ~6 lines, covered by the official vectors) |
| Key file encryption | `hashlib.scrypt` + ChaCha20-Poly1305 (`pycryptodome`) |
| RFC 3161 tokens: CMS/ASN.1, certificates, signatures | `asn1crypto`, `cryptography` |
| OpenTimestamps | `opentimestamps` |

Vectors vendored as tests: BIP-340 (`tests/test_bip340.py`, from bip-0340/test-vectors.csv) and the
official NIP-44 v2 file `tests/data/nip44.vectors.json` from
https://github.com/paulmillr/nip44/blob/main/nip44.vectors.json (sha256
`269ed0f69e4c192512cc779e78c555090cebc7c785b609e338a62afc3ce25040`, fetched 2026-10-13; the test
checks the hash). The Go relay decrypts a Python-encrypted private signal (`relay/internal/protocol/crosslang_test.go`).

## Wire details this package fixes (seam additions to ARCHITECTURE.md)

All EIP-191 texts are UTF-8, addresses lowercase, `pk` = 64-hex device pubkey.

| Record | Kind | Signed text |
|---|---|---|
| Delegation tag `["sa-delegation", addr, expires_at, sig]` | on 9317/9318/9331/37318 | `sa-delegation:{pk}:{expires_at}` |
| Revocation (library only) | none yet | `sa-revocation:{pk}:{effective_at}` |
| Allocation (content `{v,address,nonce,governance,providers,sig}`) | 9332 | `sa-allocation:{address}:{nonce}:{governance}:{pk1}={bps1},{pk2}={bps2}` (providers sorted by pk; empty string when none) |
| Rule set (content `{v,provider,sa_address,timeframe,tiers\|private,sig}`, tags `d`=provider + `sa-delegation`) | 37318 | `sa-ruleset:{provider}:{timeframe}:{min@delay,...}` ascending by min_power, delay as `%.4f`; private: `private@{min_power}` |
| Governance delegation (content `{v,address,nonce,to,sig}`) | 9333 | `sa-govdelegation:{address}:{nonce}:{addr1}={bps1},...` sorted |
| Vote (content `{v,proposal,option,nonce}`, tags `e`=proposal + `sa-delegation`) | 9331 | none: the voter is the delegating SA address |
| Commitment (content `{v,strategy_id,seq,prev,digest}`, `sa-delegation` tag) | 9318 | none |
| Anchor attestation (content = `proof.data`, tags `e`=digest, `sa-anchor`=kind) | 1040 | none |

Signal `order_type` is `market`, `limit` (needs `limit_price`) or `stop` (needs `stop_price`). A
equal-nonce allocation pair (SA-identity equivocation) resolves to the smallest event id, here and
in the relay.

Other fixed rules: `min_power` is in whole SA tokens; tier index 0 is the best tier; a client below
every threshold gets the lowest-`min_power` public tier (private: nothing); `delay_fraction` has at
most 4 decimals; `prev` of a committed (private) signal is the id of its commitment event.

## SEP criterion -> test

All names are in `tests/`. "prop" = Hypothesis property test.

| Claim | Test |
|---|---|
| SEP-1.11 no plaintext key on disk | `test_keys.py::test_sep_1_11_device_key_file_holds_no_plaintext_secret`, `::test_sep_1_11_sa_key_file_holds_no_plaintext_secret` |
| SEP-2.3 delegation accepted only within expiry | `test_delegation.py::test_sep_2_3_accepted_only_within_expiry` |
| SEP-2.4 expired or revoked delegation fails | prop `test_delegation.py::test_sep_2_4_expired_delegation_is_always_rejected`, `::test_sep_2_4_revoked_delegation_is_always_rejected`; prop `test_signals.py::test_sep_2_4_property_signal_from_an_expired_delegation_always_fails`, `::test_sep_2_4_property_signal_from_a_revoked_device_key_always_fails`; `test_signals.py::test_sep_2_4_expired_and_wrongly_delegated_signals_fail` |
| SEP-2.6 power = sum over chains | `test_chains.py::test_sep_2_6_power_is_the_sum_over_every_enabled_chain`; live: `::test_online_bsc_balance_and_zksync_contract_exist` (`SA_ONLINE=1`) |
| SEP-2.7 balance change visible within 1 h | `test_chains.py::test_sep_2_7_cache_ttl_bounds_staleness` (TTL capped at 3600 s) |
| SEP-3.2 required fields | prop `test_models.py::test_sep_3_2_property_any_required_field_missing`, `::test_sep_3_2_signal_missing_a_required_field_is_rejected`, `::test_sep_3_2_provider_identity_and_signature_belong_to_the_event` |
| SEP-3.3 serialise -> parse equal | prop `test_models.py::test_sep_3_3_signal_json_round_trip_is_lossless` (plus round trips of every other model in the same file) |
| SEP-3.4 altered payload fails | prop `test_nostr.py::test_sep_3_4_any_mutation_fails_verification`, prop `test_signals.py::test_sep_3_4_altering_the_payload_after_signing_fails_verification` |
| SEP-3.8 PnL | `test_trackrecord.py::test_sep_3_8_pnl_matches_the_hand_computed_value` |
| SEP-3.9 win rate | `test_trackrecord.py::test_sep_3_9_win_rate_matches` |
| SEP-3.10 max drawdown | `test_trackrecord.py::test_sep_3_10_max_drawdown_matches` |
| SEP-3.11 byte-identical | `test_trackrecord.py::test_sep_3_11_two_runs_are_byte_identical_and_hash_seed_independent` (two processes, different `PYTHONHASHSEED`) |
| TSA anchor: signature, imprint, chain at genTime, EKU, policy | `test_tsa.py::test_valid_two_of_three_is_accepted_and_the_kth_earliest_bound_is_the_time`, `::test_token_for_a_different_hash_is_rejected`, `::test_token_from_an_unpinned_ca_is_rejected`, `::test_certificate_expired_today_but_valid_at_gentime_still_verifies`, `::test_certificate_not_valid_at_gentime_is_rejected`, `::test_wrong_policy_oid_is_rejected`, `::test_signer_without_the_timestamping_eku_is_rejected`, `::test_tampered_and_forged_tokens_are_rejected`; 13 real saved tokens: `::test_saved_real_token_verifies_offline` |
| TSA k-of-n counts organisations; k-th earliest bound | `test_tsa.py::test_two_tokens_from_one_organisation_count_once`, `::test_digicert_and_quovadis_are_one_operator_as_are_sectigo_and_entrust`, `::test_kth_earliest_holds_against_k_minus_one_liars` |
| TSA stamper: >= 15 s per TSA, stops on 429 or refusal | `test_tsa.py::test_no_tsa_is_contacted_more_often_than_every_15_seconds`, `::test_http_429_stops_that_tsa_for_good_until_resumed`, `::test_a_tsa_rejection_status_stops_it_and_too_few_operators_raises`; live: `::test_online_two_of_three_public_tsas_round_trip` (`SA_ONLINE=1`) |
| `trackrecord.compute` accepts `tsa` proofs | `test_tsa.py::test_trackrecord_compute_accepts_tsa_proofs_and_scores_a_late_one_as_a_miss` |
| NIP-44 v2 to spec (official vectors) | `test_nip44_vectors.py` (conversation keys, message keys, padding, encrypt/decrypt, long messages, invalid inputs) |
| BIP-340 vectors | `test_bip340.py::test_bip340_vectors` |
| Stop orders | `test_models.py::test_stop_orders_need_a_stop_price`, `test_trackrecord.py::test_stop_orders_trigger_on_the_candle_that_reaches_the_stop_or_stay_unfilled`, `::test_sell_stop_gapping_through_fills_at_the_open` |
| SEP-3.16 backdated signal never earns anything (scored as a miss, not dropped) | prop `test_trackrecord.py::test_sep_3_16_property_backdated_signals_never_earn_anything`, `::test_sep_3_16_a_signal_anchored_after_the_move_it_claims_is_a_miss_not_hidden` |
| Security review H3: withholding or delaying anchors never raises pnl or win_rate | prop `test_trackrecord.py::test_h3_withholding_or_delaying_any_subset_of_anchors_never_raises_pnl_or_win_rate`, `::test_h3_a_hidden_loser_cannot_be_cherry_picked_out_of_the_record` |
| Security review M4: exposure cap, `max_gross_exposure`, `pnl_per_exposure` | `test_trackrecord.py::test_m4_opens_stack_only_up_to_full_capital_and_the_excess_is_rejected`, prop `::test_m4_gross_exposure_never_exceeds_one_whatever_is_published` |
| Security review H1: kind 9332 needs a delegation from `content.address` | `test_power.py::test_h1_a_stranger_rewrapping_a_valid_allocation_is_rejected`, `::test_h1_a_missing_or_foreign_sa_delegation_tag_is_rejected` |
| Security review M2: proposer needs a delegation and `min_proposer_power` | `test_governance.py::test_m2_a_proposal_without_a_delegation_tag_is_rejected_everywhere`, `::test_m2_a_proposal_from_an_address_below_min_proposer_power_is_rejected` |
| Security review M3: kind 9334 revocations | `test_delegation.py::test_m3_revocation_event_round_trips_and_any_publisher_may_sign_it`, `test_governance.py::test_m3_a_vote_from_a_revoked_device_key_does_not_count`, `test_trackrecord.py::test_m3_a_revoked_device_key_speaks_for_nobody_from_effective_at` |
| Security review L3: scrypt `n*r` bound on load | `test_keys.py::test_l3_a_crafted_key_file_cannot_make_load_burn_memory_or_cpu` |
| SEP-3.21 more power never waits longer | prop `test_power.py::test_sep_3_21_more_allocated_power_never_waits_longer` |
| SEP-3.43 zero power still served (tier fallback) | `test_power.py::test_sep_3_42_zero_power_degrades_instead_of_blocking` |
| SEP-3.40 timeframe bound by rule set | `test_power.py::test_sep_3_40_timeframe_is_bound_by_the_rule_set` |
| SEP-3.22 tier from allocated power only | `test_power.py::test_sep_3_22_tier_comes_from_power_allocated_to_that_provider`, prop `::test_sep_3_22_property_other_shares_and_balance_do_not_move_the_tier` |
| SEP-3.32 allocation signed by SA, newer wins | prop `test_power.py::test_sep_3_32_allocation_not_signed_by_its_address_is_rejected`, prop `::test_sep_3_32_only_a_higher_nonce_replaces_the_current_allocation`, `::test_sep_3_32_allocation_of_another_identity_cannot_replace` |
| SEP-3.33 over 100% rejected | prop `test_power.py::test_sep_3_33_allocations_over_100_percent_are_rejected_everywhere` |
| SEP-3.34 power not used twice | prop `test_power.py::test_sep_3_34_a_unit_of_power_never_counts_for_governance_and_signals`, `test_governance.py::test_sep_3_34_power_allocated_to_a_provider_does_not_vote` |
| SEP-3.24 rule set signed via SA | `test_power.py::test_sep_3_24_rule_set_signed_via_the_provider_sa_identity_is_accepted`, prop `::test_sep_3_24_property_rule_sets_not_signed_via_the_sa_identity_are_rejected` |
| SEP-3.26 private signal decrypts and verifies | `test_signals.py::test_sep_3_26_private_signal_is_decrypted_and_verified_by_the_entitled_follower` |
| SEP-3.27 non-entitled cannot read | `test_signals.py::test_sep_3_27_non_entitled_client_cannot_read_a_private_signal` (the relay-store half is the Go relay's) |
| SEP-3.28 commitment predates outcome | prop `test_trackrecord.py::test_sep_3_28_property_reveal_accepted_iff_commitment_predates_the_outcome`, `::test_sep_3_28_a_revealed_signal_counts_only_if_its_commitment_predates_the_outcome`, `test_signals.py::test_sep_3_28_commitment_digest_binds_the_plaintext` |
| SEP-3.29 off-relay signals | `test_signals.py::test_sep_3_29_off_relay_signal_text_gets_the_same_checks`, prop `::test_sep_3_29_property_wrong_delegation_or_tamper_is_rejected` |
| SEP-3.36 relay coverage | `test_governance.py::test_sep_3_36_result_lists_answering_relays_and_is_incomplete_when_one_is_down`, `::test_sep_3_36_end_to_end_with_one_of_three_relays_stopped` |
| SEP-3.37 post-deadline excluded | prop `test_governance.py::test_sep_3_37_votes_and_allocations_anchored_after_the_deadline_never_change_the_result`, `::test_sep_3_37_a_backdated_created_at_without_a_proof_is_unanchored` |
| SEP-3.35 snapshot + delay D | prop `test_power.py::test_sep_3_35_allocation_takes_effect_only_d_after_its_anchored_time`, prop `test_governance.py::test_sep_3_35_reallocating_during_an_open_vote_changes_neither_the_vote_nor_a_tier_before_d`, `::test_sep_3_35_an_allocation_in_force_at_the_snapshot_fixes_the_weight`, `::test_sep_3_35_an_allocation_published_before_the_snapshot_but_younger_than_d_is_not_yet_in_force` |
| SEP-3.14 offline = relay report | `test_bundle.py::test_sep_3_14_report_from_an_imported_bundle_equals_the_report_from_relay_events` |
| SEP-3.15 omitted/reordered/edited flagged | prop `test_bundle.py::test_sep_3_15_dropped_swapped_or_edited_signals_are_never_verified_as_complete`, prop `::test_sep_3_15_dropping_any_non_tail_item_is_caught_without_an_external_head`, `::test_sep_3_15_tail_truncation_needs_an_external_head`, `::test_sep_3_15_problems_are_specific` |
| SEP-3.12 unrevealed = miss | prop `test_trackrecord.py::test_sep_3_12_an_unrevealed_commitment_is_always_counted_as_a_miss`, `::test_sep_3_12_long_and_short_committed_only_one_revealed` |
| SEP-3.13 late reveal excluded | `test_governance.py::test_sep_3_13_a_vote_first_readable_after_the_deadline_is_excluded_despite_an_early_anchor`, `::test_sep_3_13_a_vote_nobody_observed_is_not_trusted_when_the_tally_runs_late` |

Not in sa-core (relay, engine or app claims): SEP-3.5, SEP-3.6, SEP-3.17, SEP-3.38, SEP-3.39, SEP-3.41, SEP-3.25, SEP-3.44, SEP-3.30 (follower execution).

## Track record rules (read before trusting a number)

- An item is on time only if an anchor proof verifies and the anchored time is <= `created_ms/1000 + tolerance_s` (default 60 s). A published signal that is not on time (`unanchored`, `late_anchor`) is scored as a miss, never dropped: its round trip counts as a loss in `win_rate` and earns at most 0. OTS has hours of slack, so signals need a fast anchor (TSA, or BSC commit).
- Total open notional is capped at 1.0 of capital (`exposure_cap` rejections); the report carries `max_gross_exposure` and `pnl_per_exposure`.
- Fill is the open of the first 1m candle at or after `created_ms` (and not before the strategy's previous fill); the anchor time does not move it. Limit orders fill at the limit price if a candle touches it within `timeframe`, else `unfilled`. A stop order triggers on the first candle whose high (buy) or low (sell) reaches `stop_price` within `timeframe` and fills at the stop, or the open if worse; else `unfilled`. No fees or slippage.
- `size_fraction`: for `open`/`adjust` the notional (fraction of capital) added (buy +, sell -); for `close` the fraction of the current position closed. Position is netted per (strategy, instrument).
- An unrevealed commitment is a miss: a loss in the win-rate denominator, 0 in PnL, listed by event id.
- `completeness` repeats the bundle check. Tail truncation is detectable only with `expected_heads`; otherwise `head_checked` is false.

## Limits

- TSA revocation is not checked (no CRL/OCSP in the verifier); name constraints, certificate policies
  and path-length constraints are not checked either. Keep the tokens: they are evidence, and OTS is
  the long-term backstop. The pinned bundle (`src/sa_core/tsa_trust/`, version `2026-10-12`) rests on
  vendor downloads for roots that are in neither the Mozilla nor the macOS store (FreeTSA, SwissSign,
  DFN, Sectigo qualified); see its README.
- A TSA proof carries its tokens (about 6 KB each, 2-3 per batch), so a bundle with TSA proofs is
  larger than one with BSC commits.
- Public TSAs state no service level; the stamper keeps >= 15 s per TSA and stops on 429.
