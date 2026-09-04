# -*- coding: utf-8 -*-
"""Build an Xray-core config from neutral profiles.

Xray 26.7.28 supports vless, vmess, trojan, shadowsocks, socks, http,
wireguard and hysteria (the hysteria2 QUIC transport). TUIC is not supported
by Xray in any version, so those profiles are skipped (reported).
Modes:
  urltest - a leastPing balancer + burstObservatory (latency auto-switch).
  manual  - routing pinned to the user's active profile.
Kodi-free.
"""

import os

import dns_utils

_ENGINE_UNSUPPORTED = {"tuic"}


def _stream(p):
    sec = p.get("security", "none")
    ss = {}
    if sec == "reality":
        ss["security"] = "reality"
        ss["realitySettings"] = {
            "serverName": p.get("sni", ""),
            "fingerprint": p.get("fingerprint", "chrome"),
            "publicKey": p.get("reality_public_key", ""),
            "shortId": p.get("reality_short_id", ""),
        }
    elif sec == "tls":
        ss["security"] = "tls"
        ss["tlsSettings"] = {"serverName": p.get("sni", "")}
    network = p.get("network", "tcp")
    if network not in ("tcp", ""):
        ss["network"] = network
        if network == "ws" and p.get("path"):
            ss["wsSettings"] = {"path": p["path"]}
    return ss


def _outbound(p):
    proto = p["protocol"]
    if proto in _ENGINE_UNSUPPORTED:
        return None
    if proto == "vless":
        if p.get("network") == "xhttp":
            return None
        user = {"id": p["uuid"], "encryption": "none"}
        if p.get("flow"):
            user["flow"] = p["flow"]
        return {
            "tag": p["tag"],
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": p["server"], "port": p["port"], "users": [user],
            }]},
            "streamSettings": _stream(p),
        }
    if proto == "vmess":
        user = {"id": p["uuid"], "alterId": int(p.get("alter_id", 0)),
                "security": p.get("security", "auto")}
        return {
            "tag": p["tag"],
            "protocol": "vmess",
            "settings": {"vnext": [{
                "address": p["server"], "port": p["port"], "users": [user],
            }]},
            "streamSettings": _stream(p),
        }
    if proto == "trojan":
        return {
            "tag": p["tag"],
            "protocol": "trojan",
            "settings": {"servers": [{
                "address": p["server"], "port": p["port"],
                "password": p["password"],
            }]},
            "streamSettings": _stream(p),
        }
    if proto == "shadowsocks":
        return {
            "tag": p["tag"],
            "protocol": "shadowsocks",
            "settings": {"servers": [{
                "address": p["server"], "port": p["port"],
                "method": p.get("method", "aes-256-gcm"),
                "password": p.get("password", ""),
            }]},
        }
    if proto == "hysteria2":
        # Xray hysteria = hysteria2 via the QUIC hysteria transport (v2);
        # both the outbound settings and the hysteria transport carry version.
        return {
            "tag": p["tag"],
            "protocol": "hysteria",
            "settings": {
                "address": p["server"], "port": p["port"], "version": 2,
            },
            "streamSettings": {
                "network": "hysteria",
                "security": "tls",
                "tlsSettings": {"serverName": p.get("sni", p["server"])},
                "hysteriaSettings": {"version": 2,
                                     "auth": p.get("password", "")},
            },
        }
    if proto == "wireguard":
        return {
            "tag": p["tag"],
            "protocol": "wireguard",
            "settings": {
                "secretKey": p.get("private_key", ""),
                "address": [part.strip() for part in
                            p.get("local_address", "").split(",")
                            if part.strip()],
                "peers": [{
                    "publicKey": p.get("public_key", ""),
                    "endpoint": "%s:%s" % (p["server"], p["port"]),
                    "allowedIPs": ["0.0.0.0/0"],
                }],
            },
        }
    if proto in ("socks", "http"):
        server = {"address": p["server"], "port": p["port"]}
        if p.get("username"):
            server["users"] = [{"user": p["username"],
                                "pass": p.get("password", "")}]
        return {
            "tag": p["tag"],
            "protocol": proto,
            "settings": {"servers": [server]},
        }
    return None


def build_outbounds(profiles):
    outbounds, tags, skipped = [], [], []
    for p in profiles:
        try:
            ob = _outbound(p)
            if ob is None:
                skipped.append((p.get("tag", "?"), "xray-unsupported:%s" % p["protocol"]))
                continue
            outbounds.append(ob)
            tags.append(ob["tag"])
        except Exception as e:
            skipped.append((p.get("tag", "?"), "build-error:%s" % e))
    return outbounds, tags, skipped


def _dns_block(settings):
    """Normalized DNS server list + queryStrategy for Xray.

    Remote (non-local) DNS servers are dispatched through Xray's routing
    engine, so the DoH/DoT entry travels inside the tunnel: DNS answers can
    be neither observed nor substituted on the path, even when the remote
    server itself performs DNS substitution for other traffic. The router
    DNS (the DHCP answer) stays as the last fallback and pins hostname-based
    DoH/DoT servers through `hosts`, resolved at config build time.
    """
    server = (settings.get("dns_server") or "").strip()
    if "dns_bootstrap" in settings:
        bootstrap = settings["dns_bootstrap"] or []
    else:
        bootstrap = dns_utils.system_dns_servers()
    parsed = dns_utils.parse_dns_server(server)
    servers = []
    hosts = {}
    if parsed is None:
        servers.append({"address": "https://1.1.1.1/dns-query"})
    elif parsed["kind"] == "doh":
        servers.append({"address": "https://%s%s"
                        % (parsed["host"], parsed.get("path", "/dns-query"))})
        if not dns_utils.is_ipv4(parsed["host"]):
            ips = dns_utils.resolve_hostname(parsed["host"])
            if ips:
                hosts[parsed["host"]] = ips
    elif parsed["kind"] == "dot":
        address = "tls://%s" % parsed["host"]
        if parsed.get("port", 853) != 853:
            address = "%s:%d" % (address, parsed["port"])
        servers.append({"address": address})
        if not dns_utils.is_ipv4(parsed["host"]):
            ips = dns_utils.resolve_hostname(parsed["host"])
            if ips:
                hosts[parsed["host"]] = ips
    else:
        entry = {"address": parsed["host"]}
        if parsed.get("port", 53) != 53:
            entry["port"] = parsed["port"]
        servers.append(entry)
    for ip in bootstrap:
        servers.append({"address": ip})
    block = {"servers": servers}
    if hosts:
        block["hosts"] = hosts
    strategy = (settings.get("dns_query_strategy") or "").strip()
    mapping = {
        "prefer_ipv4": "UseIPv4",
        "ipv4_only": "UseIPv4Only",
        "prefer_ipv6": "UseIPv6",
        "ipv6_only": "UseIPv6Only",
    }
    if strategy in mapping:
        block["queryStrategy"] = mapping[strategy]
    return block


def _geo_rules(settings, geo_paths):
    """Routing rules for downloaded geo databases, only when present on disk.

    A rule referencing a missing geoip.dat/geosite.dat makes Xray refuse the
    whole config, so the rule is emitted only when the file exists.
    """
    rules = []
    geoip = (geo_paths or {}).get("geoip")
    geosite = (geo_paths or {}).get("geosite")
    if (settings.get("geoip_url") or "").strip() and geoip and os.path.exists(geoip):
        rules.append({"type": "field", "ip": ["geoip:ru-blocked"],
                      "outboundTag": "direct"})
    if (settings.get("geosite_url") or "").strip() and geosite and os.path.exists(geosite):
        rules.append({"type": "field", "domain": ["geosite:ru-blocked"],
                      "outboundTag": "direct"})
    return rules


def build_config(profiles, settings, active_tag=None):
    outbounds, tags, skipped = build_outbounds(profiles)

    mode = settings.get("mode", "urltest")
    rules = [{"type": "field", "ip": ["geoip:private"],
              "outboundTag": "direct"}]
    if settings.get("direct_torrent"):
        rules.insert(0, {"type": "field", "protocol": ["bittorrent"],
                         "outboundTag": "direct"})
    for rule in reversed(_geo_rules(settings, settings.get("geo_paths"))):
        rules.insert(0, rule)
    balancer = None
    observatory = None
    final = "direct"
    if mode != "direct" and tags:
        if mode == "manual":
            default = active_tag if active_tag in tags else tags[0]
            rules.append({"type": "field", "network": "tcp,udp",
                          "outboundTag": default})
        else:
            balancer = {
                "tag": "proxy",
                "selector": tags,
                "strategy": {"type": "leastPing"},
            }
            observatory = {
                "subjectSelector": tags,
                "pingConfig": {
                    "destination": settings.get("test_url", "https://www.gstatic.com/generate_204"),
                    "interval": settings.get("urltest_interval", "3m"),
                    "timeout": "30s",
                    "sampling": 2,
                },
            }
            rules.append({"type": "field", "network": "tcp,udp",
                          "balancerTag": "proxy"})
        final = "proxy"

    config = {
        "log": {"loglevel": settings.get("log_level", "info").replace("warn", "warning")},
        "dns": _dns_block(settings),
        # Xray cannot multiplex SOCKS and HTTP on one listener: with both
        # bound to the same port, one inbound silently starves the other.
        # HTTP sits on the configured port (Kodi's proxy points there) and
        # SOCKS takes the next port; the supervisor reserves both.
        "inbounds": [
            {
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": int(settings.get("local_port", 1080)),
                "protocol": "http",
                "settings": {},
            },
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": int(settings.get("local_port", 1080)) + 1,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            },
        ],
        "outbounds": outbounds + [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {"domainStrategy": "IPIfNonMatch", "rules": rules,
                    "final": final},
    }
    if balancer:
        config["routing"]["balancers"] = [balancer]
    if observatory:
        config["burstObservatory"] = observatory
    return config, skipped
