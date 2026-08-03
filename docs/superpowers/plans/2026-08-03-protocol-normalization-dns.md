# Full Protocol Support, Config Normalization and DNS — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Per the approved design `docs/superpowers/specs/2026-08-03-protocol-normalization-dns-design.md`: normalize every input shape (profile links, full engine configs, subscriptions incl. JSON arrays) into the existing neutral profiles; add full proxy-protocol coverage for both engines (vless, vmess, trojan, shadowsocks, shadowsocks-2022, hysteria2, wireguard, tuic [sing-box only], socks, http); add DNS configuration (UDP / DoH / DoT + query strategy) translated per engine; bump both engines to their latest releases (sing-box 1.13.15, Xray 26.7.28).

Work only in `/home/random/dev/kodi-advanced-proxy/.worktrees/proxy-integration-release`.

## Architecture

Keep the Kodi-free module split. `parsers.py` gains `parse_config` (JSON sing-box/Xray outbound extraction) and the new URI schemes; `subscriptions.py` gains the JSON branch in `decode_subscription`; `profiles.py` dedups config-sourced profiles by (protocol, server, port) with manual profiles winning; `build_singbox.py`/`build_xray.py` gain new protocol outbounds and the normalized DNS block; `helpers.py` gains `parse_dns_server` and the new settings; `build.sh`, `src/binary_manager.py`, and `tests/test_packaging.py` pin the new engine versions. All behavior changes are TDD-driven in `tests/test_core.py` first.

## Global Constraints

* Strict TDD for every behavior change: write the smallest focused test first, run it with `python3 -m unittest`, observe and record the expected failure, then make the minimal implementation change and rerun. During GREEN, run ONLY the newly written tests first; run broader/full suites after that observation is recorded.
* Use the actual existing test classes/fakes in `tests/test_core.py`; add focused methods or a behavior-named class in an existing test module; never create invented test modules or fixture architectures.
* Preserve existing public contracts (`parse_uri(line)`, `ProfileStore(path)`, `ProxySupervisor(...)`, `helpers.get_settings()`, builders' `build_config(profiles, settings, active_tag=None)`); extend with optional args only.
* Every protocol is claimed for an engine only when that engine's pinned version actually supports it. Verified against the pinned tags: sing-box 1.13.15 supports vless/vmess/trojan/ss/ss2022/hysteria2/wireguard/tuic/socks/http; Xray 26.7.28 supports all of those except tuic (Xray has no tuic in any version).
* Xray's hysteria2 is the `hysteria` protocol (QUIC transport, version 2) — the neutral profile `hysteria2` maps to Xray `protocol: "hysteria"` with `settings.address/port/version: 2` plus the hysteria transport carrying `auth`. Confirm the exact transport key name against `infra/conf/hysteria.go` + `transport/internet/hysteria/config.proto` of v26.7.28 during GREEN and pin it in the test.
* Non-proxy outbounds (direct/block/dns/selector/urltest/freedom/blackhole/dokodemo/loopback/tun/reverse/observatory/metrics) are never profiles; skipped with a reason.
* Failed fetch/decode never touches existing profiles (records last_error); manual profiles always win dedup; disabled-protocol links skipped at parse (both manual pastes and refreshes).
* No new dependencies beyond the Python standard library. No QR, no automatic addon restarts, no publication changes, no changes to watchdog/lifecycle semantics.
* English PLAIN commit messages only. Prefix every git command with `GIT_MASTER=1`. Do not force push.

## Task 1: Bump engine versions everywhere (RED → GREEN)

- [ ] RED: add a focused `TestEngineVersionContract` class in `tests/test_core.py` that reads `build.sh` AND `src/binary_manager.py` and asserts they declare `SINGBOX_VERSION=1.13.15` and `XRAY_VERSION=26.7.28` (both files, since `scripts/check_versions.sh` treats build.sh vs binary_manager.py drift as fatal). Also bump `SB_VERSION`/`XR_VERSION` constants in `tests/test_packaging.py` to the new versions and observe the contract failure.
- [ ] GREEN: update `build.sh` (`SINGBOX_VERSION="1.13.15"`, `XRAY_VERSION="26.7.28"`) and its `SINGBOX_SHA256` map for all eight platform assets of 1.13.15. Obtain the digests from the sing-box GitHub release API: `https://api.github.com/repos/SagerNet/sing-box/releases/tags/v1.13.15` → each asset's `digest` field. Xray needs no static checksum map (verified at build time from `.dgst`). Update `src/binary_manager.py` `SINGBOX_VERSION`/`XRAY_VERSION` to match.
- [ ] Run the focused version-contract tests (GREEN evidence), then `tests/test_packaging` regressions. Do NOT build binaries in this task; `check_versions.sh` runs in Task 8 after a fresh build.

## Task 2: New URI schemes in parsers (RED → GREEN)

- [ ] RED: `TestParsers` gains parse cases for `vmess://` (base64-JSON form and modern `uuid@host:port` form), `ss://` (base64 `method:password@host:port` and plain), `socks://`, `http://`, `wireguard://` (peer public key + private key + local address), `tuic://`; each returns the neutral dict with the spec's key fields. Unknown/legacy schemes (`ssr://`, `shadowtls://`, `naive://`) are skipped with a reason via `parse_lines`.
- [ ] GREEN: extend `parsers.parse_uri` and `_PROTOCOL_PREFIXES` for the new schemes; keep all existing schemes working unchanged; `disabled_protocols` filtering continues to work for every new protocol name.
- [ ] Run focused `TestParsers` (GREEN evidence), then full `test_core` regressions.

## Task 3: parse_config — JSON config normalization (RED → GREEN)

- [ ] RED: new behavior class in `tests/test_core.py` for `parse_config`:
  - a sing-box config (outbounds[].type) yields neutral profiles for vless/vmess/trojan/ss/ss2022/hysteria2/wireguard/tuic/socks/http and skips direct/block/dns/selector/urltest with reasons;
  - an Xray config (outbounds[].protocol) yields the same minus tuic (skipped as xray-unsupported); the Xray `hysteria` protocol (QUIC, version 2, auth) maps to the `hysteria2` neutral profile;
  - a JSON array of full configs (each with `remarks` + `outbounds`) yields profiles tagged from the outbound tag when present, else `remarks`;
  - a 2-3 element minimal fixture modeled on the real bigping shape (array of configs with vless/hy2/trojan outbounds + a `block`) extracts exactly the three proxies with the right tags and protocols;
  - invalid JSON raises; a config with no proxy outbounds yields ([], all skipped).
- [ ] RED: `TestSubscriptionDecode`/`TestSubscriptionStore` gains the JSON branch: a body that is a JSON array of configs decodes into profiles usable by the store (add/refresh/mirror-sync/cascade-delete all work with config-sourced profiles); a single JSON config body works; base64 of a JSON body works. `parse_links` is NOT involved for JSON bodies — `decode_subscription` detects JSON first and routes to `parse_config` per element.
- [ ] GREEN: implement `parsers.parse_config` (engine detection: presence of `outbounds[].type` = sing-box, `outbounds[].protocol` = Xray; map each proxy outbound into the neutral dict per the spec's field tables; Xray hysteria → neutral hysteria2) and the JSON branch in `subscriptions.decode_subscription` (returns profile dicts carrying `protocol`/`server`/`port` instead of a `uri`).
- [ ] Run focused parser/subscription tests (GREEN evidence), then `test_core` + `TestSubscriptionStore` regressions.

## Task 4: URI-less dedup in profiles (RED → GREEN)

- [ ] RED: `TestProfileStore`/`TestSubscriptionStore`: two config-sourced profiles with the same (protocol, server, port) and different tags de-dup; a manual profile matching the same (protocol, server, port) wins over the subscription copy; `sync_subscription` keeps the user's enabled flag for config-sourced profiles.
- [ ] GREEN: `profiles.add_subscription_profiles`/`sync_subscription` use `(protocol, server, port)` as the identity when `uri` is absent (fallback key helper); manual profiles (no subscription) still win.
- [ ] Run focused profile/subscription tests (GREEN evidence), then full `test_core` regressions.

## Task 5: sing-box builder — new protocols + DNS (RED → GREEN)

- [ ] RED: `TestBuildSingbox` gains outbound cases for vmess, shadowsocks, ss2022 (method starting `2022-`), wireguard, tuic, socks, http (hysteria2 already covered) and DNS cases:
  - `dns_server="8.8.8.8"` → `dns.servers` contains `{"address": "8.8.8.8"}` AND the existing `domain_suffix: [".duckdns.org"] → local` rule is preserved;
  - `https://dns.google/dns-query` → DoH entry; `tls://8.8.8.8` → DoT entry;
  - `dns_query_strategy="prefer_ipv4"` → the sing-box DNS strategy field set; empty → no strategy;
  - empty `dns_server` → the current duckdns-aware block unchanged;
  - when `dns_server` is set, `route.default_domain_resolver` must not reference a removed `local` tag (either preserved via a local entry or the field adjusted).
- [ ] GREEN: extend `build_singbox._outbound` for the new protocols and replace the DNS block with the normalized builder (`helpers.parse_dns_server`) while preserving the duckdns local rule and a valid `default_domain_resolver`.
- [ ] Run focused `TestBuildSingbox` (GREEN evidence), then core regressions.

## Task 6: Xray builder — new protocols + DNS (RED → GREEN)

- [ ] RED: `TestBuildXray` gains outbound cases for vmess, shadowsocks, wireguard, socks, http, hysteria2 (mapped from neutral hysteria2 to Xray `hysteria` protocol with address/port/version 2 + transport auth per the pinned 26.7.28 shapes) and skips tuic with an xray-unsupported reason. DNS cases mirror Task 5 with Xray syntax: UDP `"8.8.8.8"`, DoH `"https://dns.google/dns-query"`, DoT `"tcp+tls://8.8.8.8:853"`, `queryStrategy` mapping (`prefer_ipv4`→`UseIPv4`, `ipv4_only`→`UseIPv4Only`, `prefer_ipv6`→`UseIPv6`, `ipv6_only`→`UseIPv6Only`); empty `dns_server` keeps the current server list.
- [ ] GREEN: extend `build_xray._outbound` (including the Xray hysteria shape) and the DNS block; update the module docstring (it currently claims Xray has no Hysteria2).
- [ ] Run focused `TestBuildXray` (GREEN evidence), then core + packaging regressions.

## Task 7: helpers + settings + UI (RED → GREEN)

- [ ] RED: `TestHelpers` gains `parse_dns_server` cases (plain IP → udp, `https://` → doh, `tls://` → dot, garbage → error/None) and the new settings (`dns_server`, `dns_query_strategy`, `direct_torrent`) defaults/normalization. Settings XML contract: `dns_server`, `dns_query_strategy`, and `direct_torrent` present in the subscriptions category; `strings.po` has the new labels (use the next unused msgctxt IDs after 32240, e.g. 32241-32243 — verify uniqueness against the file).
- [ ] GREEN: implement `helpers.parse_dns_server`, add the settings to `helpers.get_settings()`/defaults, `settings.xml`, and `strings.po` (msgid texts: e.g. "DNS server (8.8.8.8 / https://… / tls://…)", "DNS query strategy", "Direct BitTorrent traffic").
- [ ] Run focused helper tests (GREEN evidence), then `python3 scripts/validate_addon.py .` (must pass: every label/help/heading ID referenced in settings.xml must exist in strings.po) + full suite.

## Task 7b: Torrent direct routing (RED → GREEN)

- [ ] RED: `TestBuildSingbox` and `TestBuildXray` gain cases: with `direct_torrent=True` the sing-box config has a `route.rules` entry `{"protocol": "bittorrent", "action": "route", "outbound": "direct"}` before the private-IP rule, and the Xray config has `{"type": "field", "protocol": ["bittorrent"], "outboundTag": "direct"}` in `routing.rules`; with `direct_torrent=False` (default) neither rule is present.
- [ ] GREEN: add the bittorrent rule to both builders, gated on `settings.get("direct_torrent")`.
- [ ] Run focused builder tests (GREEN evidence), then full suite.

## Task 8: Verification (local + live)

- [ ] Run the full suite with `python3 -m unittest discover -s tests -v`; require all tests to pass.
- [ ] Run `bash scripts/check_versions.sh .` (now that build.sh and binary_manager.py agree) and `python3 scripts/validate_addon.py .`; require both to pass.
- [ ] Build all platforms (`./build.sh`), verify each per-platform ZIP and the universal ZIP (`scripts/verify_zip.sh`, `--universal`), confirm the new engine versions and their runtime stamps.
- [ ] Local integration: feed the real bigping subscription (JSON array of configs) through `decode_subscription`/`parse_config`; confirm profiles extracted (vless/hy2/trojan), mirror-sync works, and the built sing-box and Xray configs pass engine `check`.
- [ ] Deploy the fresh armv7 ZIP to `192.168.31.174`; live QA: add the real bigping subscription, confirm profiles appear with correct protocols from the config outbounds, refresh, delete group, confirm activation works with the new engine.

## Verification checklist

- [ ] Full suite passes.
- [ ] `check_versions.sh` and `validate_addon.py` pass.
- [ ] `./build.sh` produces all platforms + universal with sing-box 1.13.15 and Xray 26.7.28; `verify_zip.sh` OK.
- [ ] Local bigping-subscription integration passes (JSON config extraction + mirror sync + engine check).
- [ ] Live QA on 192.168.31.174 passes (subscription add/refresh/delete, protocols from config, activation).

## Task 8a: Xray geo files (RED → GREEN)

- [ ] RED: packaging contract tests assert `build.sh` extracts `geoip.dat` and `geosite.dat` next to the xray binary for Xray platforms, and `verify_zip.sh` requires them; `binary_manager` copies them into the work dir beside the engine when xray is installed (both from the bundle and after a download).
- [ ] GREEN: extend `fetch_xray` in `build.sh` to extract geoip.dat/geosite.dat; extend `scripts/verify_zip.sh` to require them for Xray platforms; extend `binary_manager._sync_from_bundle`/`_download_binary` to copy the two geo files next to `work_binary`.
- [ ] Run focused packaging/binary-manager tests (GREEN evidence), then full suite.
