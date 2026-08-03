# Advanced Proxy: Full Protocol Support, Config Normalization and DNS

Date: 2026-08-03
Status: Draft for approval

## Engine versions

Both engines are bumped to their latest releases (verified asset availability
for all eight platforms from the upstream repos on 2026-08-03):

- sing-box **1.13.14 → 1.13.15** (latest release, `SagerNet/sing-box`).
- Xray-core **25.8.3 → 26.7.28** (latest tagged release with assets,
  `XTLS/Xray-core`). Hysteria2 support landed in Xray v26.6.1 and is present
  in 26.7.28; TUIC is NOT supported by Xray in any version.

`build.sh` pins both versions and their per-platform checksums. Runtime
stamps (`resources/bin/<platform>/version`, `xray_version`) follow.

## Scope

This design extends the addon in three connected directions, all feeding the
existing neutral-profile pipeline:

1. **Normalization of every input shape**: a single profile link
   (`vless://`, `vmess://`, `ss://`, `trojan://`, `hy2://`, `socks://`,
   `http://`...), a full engine config (sing-box JSON or Xray JSON), and a
   subscription (plain text, base64, or a JSON array of configs) all become
   the same neutral profile dicts the builders already consume.
2. **Full proxy protocol coverage** for both engines, not just vless/trojan:
   vless, vmess, trojan, shadowsocks, hysteria2, wireguard, tuic, socks,
   http. Each engine builds what it supports and reports the rest as
   skipped (existing per-engine skip mechanics stay).
3. **DNS configuration** (DoH / DoT / UDP) normalized into the settings and
   translated per engine.

## Design

### 1. Neutral profile model (extended)

The neutral dict gains the union of fields the new protocols need. Existing
fields (`protocol`, `tag`, `server`, `port`, `uuid`, `password`, `flow`,
`security`, `sni`, `fingerprint`, `reality_*`, `network`, `path`) stay.

| Protocol | Key fields added |
| --- | --- |
| vmess | `uuid`, `alter_id`, `security` ("auto"/"aes-128-gcm"/"zero"), `network`, `path` |
| shadowsocks | `method`, `password`, `plugin`, `plugin_opts` |
| wireguard | `private_key`, `public_key`, `preshared_key`, `local_address`, `reserved` |
| tuic | `uuid`, `password`, `congestion_control`, `sni` |
| socks | `username`, `password` |
| http | `username`, `password` |
| hysteria2 | (existing) `password`, `sni`, `fingerprint`, `up_mbps`, `down_mbps` |

`parsers.parse_uri` gains the new schemes; `parsers.parse_config` extracts
outbounds from a JSON config; both return the same neutral dicts.

### 2. Normalization entry points

```
input
 ├─ parse_uri(line)            single profile link  (vless/vmess/ss/trojan/hy2/socks/http)
 ├─ parse_config(json_text)    sing-box or Xray config (or array of configs)
 │     outbounds[] -> neutral profiles; non-proxy outbounds skipped
 └─ decode_subscription(body)  plain text -> parse_lines
                               base64     -> decode -> re-check text or JSON
                               JSON       -> parse_config per element
```

`parse_config` detects the engine by shape:
- sing-box: `outbounds[].type` (vless, vmess, trojan, shadowsocks,
  hysteria2, wireguard, tuic, socks, http, direct, block, dns, selector,
  urltest, ...).
- Xray: `outbounds[].protocol` (vless, vmess, trojan, shadowsocks, socks,
  http, wireguard, freedom, blackhole, dns, ...).

Only proxy outbounds are kept; `direct`/`block`/`dns`/`selector`/`urltest`/
`freedom`/`blackhole` are skipped with a reason. Per-outbound `tag` is used
when present; Xray falls back to `remarks` from the enclosing config when
the array element is a full config carrying `remarks`.

**Dedup by URI still applies** (manual wins). For configs there is no URI,
so dedup falls back to `(protocol, server, port)` identity; manual profiles
still win over subscription copies.

### 3. Engine coverage

Verified against the upstream repos (tree of each pinned tag):

| Protocol | sing-box 1.13.15 | Xray 26.7.28 |
| --- | --- | --- |
| vless | yes | yes |
| vmess | yes | yes |
| trojan | yes | yes |
| shadowsocks | yes | yes |
| shadowsocks 2022 | yes | yes |
| hysteria2 | yes | yes |
| wireguard | yes | yes |
| socks | yes | yes |
| http | yes | yes |
| tuic | yes | **no** (Xray has no tuic in any version; skipped) |

Each builder keeps its `_outbound(profile) -> dict|None` shape; None is
reported as skipped with an engine-specific reason. No protocol is claimed
for an engine that does not support it.

Non-proxy outbounds (direct, block, dns, selector, urltest, freedom,
blackhole, dokodemo, loopback, tun, reverse, observatory, metrics, tun) are
never treated as profiles.

### 4. DNS: DoH / DoT / UDP

New settings:

- `dns_server` (string, empty = current behavior): one of
  - plain IP `8.8.8.8` → UDP,
  - `https://dns.google/dns-query` → DoH,
  - `tls://8.8.8.8` → DoT.
- `dns_query_strategy` (string, empty = engine default): `prefer_ipv4`,
  `ipv4_only`, `prefer_ipv6`, `ipv6_only`.

`helpers.parse_dns_server(value)` normalizes to
`{"kind": "udp"|"doh"|"dot", "host": ..., "port": ...}`. Each builder
translates:

| kind | sing-box `dns.servers[]` | Xray `dns.servers[]` |
| --- | --- | --- |
| udp | `{"address": "8.8.8.8"}` | `"8.8.8.8"` |
| doh | `{"address": "https://dns.google/dns-query"}` | `"https://dns.google/dns-query"` |
| dot | `{"address": "tls://8.8.8.8"}` | `"tcp+tls://8.8.8.8:853"` |

sing-box gets `strategy` from `dns_query_strategy`; Xray gets `queryStrategy`
(`prefer_ipv4`→`UseIPv4`, `ipv4_only`→`UseIPv4Only`, `prefer_ipv6`→`UseIPv6`,
`ipv6_only`→`UseIPv6Only`). Empty keeps each engine's current default (local
resolver for sing-box; current server list for Xray).

The existing hard-coded `1.1.1.1`/`77.88.8.8`/duckdns-rule DNS block is
replaced by the normalized single-server block when `dns_server` is set; the
duckdns local rule is preserved because bigping uses duckdns hostnames.

### 5. Settings / UI

`settings.xml` "subscriptions" category gains `dns_server` (string) and
`dns_query_strategy` (list). `strings.po` gains the labels. Both default
empty.

### Files touched

- `service.advancedproxy/src/parsers.py` — new schemes, `parse_config`,
  config-engine detection, extended neutral model.
- `service.advancedproxy/src/subscriptions.py` — `decode_subscription` JSON
  branch, `parse_links` handles config-derived profiles (URI-less dedup).
- `service.advancedproxy/src/profiles.py` — URI-less identity dedup for
  config-sourced profiles.
- `service.advancedproxy/src/build_singbox.py` — new protocol outbounds,
  DNS block.
- `service.advancedproxy/src/build_xray.py` — new protocol outbounds, DNS
  block.
- `service.advancedproxy/src/helpers.py` — `parse_dns_server`, new settings.
- `service.advancedproxy/resources/settings.xml`, `strings.po` — new fields.
- `tests/test_core.py` — focused RED/GREEN per behavior.

## Non-Goals

- Clash/Surge YAML subscriptions.
- SSH, shadowtls, naive, shadowsocksr outbounds in the neutral model (not
  needed by the confirmed sources; skipped if encountered in a config).
- Automatic per-server DNS rule generation beyond the existing duckdns rule.
- QR codes, automatic addon restarts, publication changes.
