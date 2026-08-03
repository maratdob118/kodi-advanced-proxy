# Full Protocol Support, Config Normalization and DNS — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Per the approved design `docs/superpowers/specs/2026-08-03-protocol-normalization-dns-design.md`: normalize every input shape (profile links, full engine configs, subscriptions incl. JSON arrays) into the existing neutral profiles; add full proxy-protocol coverage for both engines (vless, vmess, trojan, shadowsocks, shadowsocks-2022, hysteria2, wireguard, tuic [sing-box only], socks, http); add DNS configuration (UDP / DoH / DoT + query strategy) translated per engine; bump both engines to their latest releases (sing-box 1.13.15, Xray 26.7.28).

Work only in `/home/random/dev/kodi-advanced-proxy/.worktrees/proxy-integration-release`.

## Architecture

Keep the Kodi-free module split. `parsers.py` gains `parse_config` (JSON sing-box/Xray outbound extraction) and the new URI schemes; `subscriptions.py` gains the JSON branch in `decode_subscription` and URI-less dedup identity; `profiles.py` dedups config-sourced profiles by (protocol, server, port) with manual profiles winning; `build_singbox.py`/`build_xray.py` gain new protocol outbounds and the normalized DNS block; `helpers.py` gains `parse_dns_server` and the new settings; `build.sh` pins the new engine versions. All behavior changes are TDD-driven in `tests/test_core.py` first.

## Global Constraints

* Strict TDD for every behavior change: write the smallest focused test first, run it with `python3 -m unittest`, observe and record the expected failure, then make the minimal implementation change and rerun. During GREEN, run ONLY the newly written tests first; run broader/full suites after that observation is recorded.
* Use the actual existing test classes/fakes in `tests/test_core.py`; add focused methods or a behavior-named class in an existing test module; never create invented test modules or fixture architectures.
* Preserve existing public contracts (`parse_uri(line)`, `ProfileStore(path)`, `ProxySupervisor(...)`, `helpers.get_settings()`, builders' `build_config(profiles, settings, active_tag=None)`); extend with optional args only.
* Every protocol is claimed for an engine only when that engine's pinned version actually supports it (verified: sing-box 1.13.15 supports vless/vmess/trojan/ss/ss2022/hysteria2/wireguard/tuic/socks/http; Xray 26.7.28 supports all of those except tuic).
* Non-proxy outbounds (direct/block/dns/selector/urltest/freedom/blackhole/dokodemo/loopback/tun/reverse) are never profiles; they are skipped with a reason.
* Failed fetch/decode never touches existing profiles (records last_error); manual profiles always win dedup; disabled-protocol links skipped at parse (both manual pastes and refreshes).
* No new dependencies beyond the Python standard library. No QR, no automatic addon restarts, no publication changes, no changes to watchdog/lifecycle semantics.
* English PLAIN commit messages only. Prefix every git command with `GIT_MASTER=1`. Do not force push.

## Task 1: Bump engine versions in build.sh (RED → GREEN)

- [ ] RED: `tests/test_packaging.py` (or the existing engine-version contract) must currently pin 1.13.14 / 25.8.3; add/extend the assertion that `build.sh` declares SINGBOX_VERSION 1.13.15 and XRAY_VERSION 26.7.28 and that `scripts/check_versions.sh`-style stamps agree. Observe the failure.
- [ ] GREEN: update `build.sh` `SINGBOX_VERSION="1.13.15"`, `XRAY_VERSION="26.7.28"`, and the pinned `SINGBOX_SHA256` entries for all eight platform assets of 1.13.15 (fetch the new digests from the sing-box release). Xray digests are verified at build time from the `.dgst` file, so no static Xray checksum map change is required beyond the version.
- [ ] Verify: `./build.sh --print-version` still prints the addon version; run `bash scripts/check_versions.sh .` after a fresh build in Task 7 (the runtime stamp check needs binaries; unit-level version-contract tests must pass now).
- [ ] Run focused version-contract tests (GREEN evidence), then `tests/test_packaging` regressions.

## Task 2: New URI schemes in parsers (RED → GREEN)

- [ ] RED: `TestParsers` gains parse cases for `vmess://` (base64 JSON and modern `uuid@host:port` forms), `ss://` (base64 method:password and plain), `socks://`, `http://`, `wireguard://` (or wg:// with peer keys), `tuic://`; each returns the neutral dict with the spec's key fields. Unknown/legacy schemes (`ssr://`, `shadow-tls`, `naive`) are skipped with a reason.
- [ ] GREEN: extend `parsers.parse_uri` and `_PROTOCOL_PREFIXES` for the new schemes; keep all existing schemes working unchanged. `disabled_protocols` filtering continues to work for every new protocol name.
- [ ] Run focused `TestParsers` (GREEN evidence), then full `test_core` regressions.

## Task 3: parse_config — JSON config normalization (RED → GREEN)

- [ ] RED: `TestParsers`/new behavior class: `parse_config(json_text)` extracts proxy outbounds from a sing-box config (outbounds[].type) and from an Xray config (outbounds[].protocol); a JSON array of full configs (each with `remarks` + `outbounds`) yields profiles tagged from `remarks` or the outbound tag; non-proxy outbounds are skipped with reasons; invalid JSON raises; a config with no proxy outbounds yields ([], all skipped).
- [ ] RED: `decode_subscription` (subscriptions.py) gains the JSON branch: a body that is a JSON array of configs decodes into link-lines/identity tuples usable by the store; a body that is one JSON config decodes the same way; base64 that decodes to JSON also works.
- [ ] GREEN: implement `parsers.parse_config` (engine detection by shape; sing-box `outbounds[].type` vs Xray `outbounds[].protocol`; map each proxy outbound into the neutral dict with the spec's fields) and the JSON branch in `subscriptions.decode_subscription`/`parse_links` (config-derived profiles carry an identity key instead of `uri`).
- [ ] Run focused parser/subscription tests (GREEN evidence), then `test_core` + `TestSubscriptionStore` regressions.

## Task 4: URI-less dedup in profiles (RED → GREEN)

- [ ] RED: `TestProfileStore`/`TestSubscriptionStore`: two config-sourced profiles with the same (protocol, server, port) and different tags de-dup; a manual profile matching the same (protocol, server, port) wins over the subscription copy; `sync_subscription` keeps the user's enabled flag for config-sourced profiles.
- [ ] GREEN: `profiles.add_subscription_profiles`/`sync_subscription` use `(protocol, server, port)` as the identity when `uri` is absent; manual profiles (no subscription) still win.
- [ ] Run focused profile/subscription tests (GREEN evidence), then full `test_core` regressions.

## Task 5: sing-box builder — new protocols + DNS (RED → GREEN)

- [ ] RED: `TestBuildSingbox` gains outbound cases for vmess, shadowsocks (+ss2022), wireguard, tuic, socks, http, hysteria2 (already there) and the DNS block cases: `dns_server="8.8.8.8"` → UDP server entry; `https://...` → DoH; `tls://...` → DoT; `dns_query_strategy` maps to sing-box `strategy`; empty `dns_server` keeps the current duckdns-aware block.
- [ ] GREEN: extend `build_singbox._outbound` for the new protocols and replace the DNS block with the normalized builder (`helpers.parse_dns_server`).
- [ ] Run focused `TestBuildSingbox` (GREEN evidence), then core regressions.

## Task 6: Xray builder — new protocols + DNS (RED → GREEN)

- [ ] RED: `TestBuildXray` gains outbound cases for vmess, shadowsocks, wireguard, socks, http, hysteria2 (supported in 26.7.28) and skips tuic with an xray-unsupported reason; DNS block cases mirror Task 5 but with Xray syntax (`"8.8.8.8"`, `"https://..."`, `"tcp+tls://8.8.8.8:853"`, `queryStrategy` mapping).
- [ ] GREEN: extend `build_xray._outbound` (including hysteria2 per the 26.7.28 proxy/hysteria config shape) and the DNS block.
- [ ] Run focused `TestBuildXray` (GREEN evidence), then core + packaging regressions.

## Task 7: helpers + settings + UI (RED → GREEN)

- [ ] RED: `TestHelpers` gains `parse_dns_server` cases (plain IP → udp, https → doh, tls:// → dot, invalid → error) and the new settings (`dns_server`, `dns_query_strategy`) defaults/normalization. Settings XML contract: `dns_server` and `dns_query_strategy` present in the subscriptions category; `strings.po` labels present.
- [ ] GREEN: implement `helpers.parse_dns_server`, add the settings to `helpers.get_settings()`/defaults, `settings.xml`, and `strings.po`.
- [ ] Run focused helper tests (GREEN evidence), then `validate_addon.py` + full suite.

## Task 8: Verification (local + live)

- [ ] Run the full suite with `python3 -m unittest discover -s tests -v`; require all tests to pass.
- [ ] Run `bash scripts/check_versions.sh .` and `python3 scripts/validate_addon.py .`; require both to pass.
- [ ] Build all platforms (`./build.sh`), verify each per-platform ZIP and the universal ZIP (`scripts/verify_zip.sh`, `--universal`), confirm the new engine versions and their runtime stamps.
- [ ] Local integration: feed the bigping subscription (JSON array of configs) through `decode_subscription`/`parse_config`; confirm profiles extracted, mirror-sync works, and the built sing-box/Xray configs pass engine `check`.
- [ ] Deploy the fresh armv7 ZIP to `192.168.31.174`; live QA: add the real bigping subscription, confirm profiles appear with correct protocols (vless/hy2/trojan from the config outbounds), refresh, delete group, confirm activation works with the new engine.

## Verification checklist

- [ ] Full suite passes.
- [ ] `check_versions.sh` and `validate_addon.py` pass.
- [ ] `./build.sh` produces all platforms + universal with sing-box 1.13.15 and Xray 26.7.28; `verify_zip.sh` OK.
- [ ] Local bigping-subscription integration passes (JSON config extraction + mirror sync + engine check).
- [ ] Live QA on 192.168.31.174 passes (subscription add/refresh/delete, protocols from config, activation).
