# -*- coding: utf-8 -*-
"""Build a sing-box config from neutral profiles.

Modes:
  urltest - a urltest group auto-switches by latency (interval/tolerance).
  manual  - a selector outbound pinned to the user's active profile.
Kodi-free.
"""

import dns_utils


def _tls(p):
    sec = p.get("security", "none")
    if sec == "reality":
        return {
            "enabled": True,
            "server_name": p.get("sni", ""),
            "utls": {"enabled": True, "fingerprint": p.get("fingerprint", "chrome")},
            "reality": {
                "enabled": True,
                "public_key": p.get("reality_public_key", ""),
                "short_id": p.get("reality_short_id", ""),
            },
        }
    if sec == "tls":
        return {
            "enabled": True,
            "server_name": p.get("sni", ""),
            "utls": {"enabled": True, "fingerprint": p.get("fingerprint", "chrome")},
        }
    return None


def _outbound(p):
    proto = p["protocol"]
    if proto == "vless":
        if p.get("network") == "xhttp":
            return None  # not supported by sing-box 1.13
        ob = {
            "type": "vless",
            "tag": p["tag"],
            "server": p["server"],
            "server_port": p["port"],
            "uuid": p["uuid"],
            "network": "tcp",
        }
        if p.get("flow"):
            ob["flow"] = p["flow"]
        tls = _tls(p)
        if tls:
            ob["tls"] = tls
        return ob
    if proto == "vmess":
        ob = {
            "type": "vmess",
            "tag": p["tag"],
            "server": p["server"],
            "server_port": p["port"],
            "uuid": p["uuid"],
            "network": p.get("network", "tcp"),
            "security": p.get("security", "auto"),
        }
        if p.get("path"):
            ob["transport"] = {"type": "ws", "path": p["path"]}
        tls = _tls(p)
        if tls:
            ob["tls"] = tls
        return ob
    if proto == "hysteria2":
        return {
            "type": "hysteria2",
            "tag": p["tag"],
            "server": p["server"],
            "server_port": p["port"],
            "password": p["password"],
            "tls": {"enabled": True, "server_name": p.get("sni", p["server"])},
        }
    if proto == "trojan":
        ob = {
            "type": "trojan",
            "tag": p["tag"],
            "server": p["server"],
            "server_port": p["port"],
            "password": p["password"],
            "network": "tcp",
        }
        tls = _tls(p)
        if tls:
            ob["tls"] = tls
        return ob
    if proto == "shadowsocks":
        ob = {
            "type": "shadowsocks",
            "tag": p["tag"],
            "server": p["server"],
            "server_port": p["port"],
            "method": p.get("method", "aes-256-gcm"),
            "password": p.get("password", ""),
        }
        if p.get("plugin"):
            ob["plugin"] = p["plugin"]
        if p.get("plugin_opts"):
            ob["plugin_opts"] = p["plugin_opts"]
        return ob
    if proto == "wireguard":
        ob = {
            "type": "wireguard",
            "tag": p["tag"],
            "server": p["server"],
            "server_port": p["port"],
            "local_address": [part.strip() for part in
                              p.get("local_address", "").split(",")
                              if part.strip()],
            "private_key": p.get("private_key", ""),
            "peer_public_key": p.get("public_key", ""),
        }
        if p.get("reserved"):
            ob["reserved"] = p["reserved"]
        return ob
    if proto == "tuic":
        ob = {
            "type": "tuic",
            "tag": p["tag"],
            "server": p["server"],
            "server_port": p["port"],
            "uuid": p.get("uuid", ""),
            "password": p.get("password", ""),
        }
        if p.get("congestion_control"):
            ob["congestion_control"] = p["congestion_control"]
        ob["tls"] = {"enabled": True,
                     "server_name": p.get("sni", p["server"])}
        return ob
    if proto == "socks":
        ob = {
            "type": "socks",
            "tag": p["tag"],
            "server": p["server"],
            "server_port": p["port"],
        }
        if p.get("username"):
            ob["username"] = p["username"]
        if p.get("password"):
            ob["password"] = p["password"]
        return ob
    if proto == "http":
        ob = {
            "type": "http",
            "tag": p["tag"],
            "server": p["server"],
            "server_port": p["port"],
        }
        if p.get("username"):
            ob["username"] = p["username"]
        if p.get("password"):
            ob["password"] = p["password"]
        return ob
    return None


def _duration_seconds(value, default=180):
    """Parse sing-box duration strings like "3m"/"30s"/"1h" into seconds."""
    try:
        text = str(value).strip()
        if text.endswith("ms"):
            return max(1, int(text[:-2]) // 1000)
        if text.endswith("m"):
            return int(text[:-1]) * 60
        if text.endswith("s"):
            return int(text[:-1])
        if text.endswith("h"):
            return int(text[:-1]) * 3600
        return int(text)
    except (TypeError, ValueError):
        return default


def build_outbounds(profiles):
    """Return (outbounds, tags, skipped) for enabled neutral profiles."""
    outbounds, tags, skipped = [], [], []
    for p in profiles:
        try:
            ob = _outbound(p)
            if ob is None:
                skipped.append((p.get("tag", "?"), "unsupported"))
                continue
            outbounds.append(ob)
            tags.append(ob["tag"])
        except Exception as e:
            skipped.append((p.get("tag", "?"), "build-error:%s" % e))
    return outbounds, tags, skipped


def _remote_dns_server(parsed, proxy_detour):
    """Typed sing-box DNS server entry for a parsed udp/doh/dot setting.

    Secure resolvers (DoH/DoT) dial through the proxy outbound when one
    exists: the ISP then sees only encrypted DNS inside the tunnel, and a
    resolver that substitutes DNS answers cannot poison the client's own
    lookups, because the queries are authenticated TLS all the way to the
    resolver - neither the ISP nor the tunnel endpoint can rewrite them.
    """
    if parsed["kind"] == "udp":
        entry = {"type": "udp", "tag": "remote", "server": parsed["host"]}
        if parsed.get("port", 53) != 53:
            entry["server_port"] = parsed["port"]
        return entry
    if parsed["kind"] == "dot":
        entry = {"type": "tls", "tag": "remote", "server": parsed["host"]}
        if parsed.get("port", 853) != 853:
            entry["server_port"] = parsed["port"]
    else:
        entry = {"type": "https", "tag": "remote", "server": parsed["host"],
                 "path": parsed.get("path", "/dns-query")}
    # detour only when there is a proxy outbound; the default (direct)
    # detour must stay implicit - sing-box rejects an explicit detour to a
    # bare direct outbound.
    if proxy_detour:
        entry["detour"] = "proxy"
    # Hostname-based DoH/DoT servers need a bootstrap resolver: the router
    # DNS answers that one lookup, everything else stays on secure DNS.
    if not dns_utils.is_ipv4(parsed["host"]):
        entry["domain_resolver"] = "bootstrap"
    return entry


def _dns_block(settings, proxy_detour=False):
    """Normalized DNS block.

    Layout (tags referenced by route rules / default_domain_resolver):
      bootstrap - router DNS (DHCP answer), direct, resolves DoH/DoT
                  hostnames and proxy server addresses
      local     - router DNS, serves domains routed direct (e.g. duckdns)
      remote    - the user's resolver (default: Cloudflare DoH); DoH/DoT
                  entries dial through the proxy so DNS answers can be
                  neither observed nor substituted on the path
    """
    server = (settings.get("dns_server") or "").strip()
    strategy = (settings.get("dns_query_strategy") or "").strip()
    if "dns_bootstrap" in settings:
        bootstrap = settings["dns_bootstrap"] or []
    else:
        bootstrap = dns_utils.system_dns_servers()
    router = bootstrap[0] if bootstrap else "77.88.8.8"

    servers = [
        {"type": "udp", "tag": "bootstrap", "server": router},
        {"type": "udp", "tag": "local", "server": router},
    ]
    parsed = dns_utils.parse_dns_server(server)
    if parsed is not None:
        servers.append(_remote_dns_server(parsed, proxy_detour))
    else:
        servers.append({"type": "https", "tag": "remote",
                        "server": "1.1.1.1", "path": "/dns-query"})
        if proxy_detour:
            servers[-1]["detour"] = "proxy"

    block = {
        "servers": servers,
        "rules": [{"domain_suffix": [".duckdns.org"], "server": "local"}],
        "final": "remote",
        "strategy": strategy or "prefer_ipv4",
        "independent_cache": True,
    }
    return block


def build_config(profiles, settings, active_tag=None):
    outbounds, tags, skipped = build_outbounds(profiles)

    mode = settings.get("mode", "urltest")
    extra_chooser = None
    if mode == "direct":
        chooser = None
        final = "direct"
    elif tags:
        if mode == "manual":
            default = active_tag if active_tag in tags else tags[0]
            chooser = {
                "type": "selector",
                "tag": "proxy",
                "outbounds": tags,
                "default": default,
                "interrupt_exist_connections": bool(settings.get("interrupt_connections", True)),
            }
        else:
            interval = settings.get("urltest_interval", "3m")
            # sing-box requires interval <= idle_timeout; a long user
            # interval must stretch the idle timeout instead of failing.
            idle = max(300, 2 * _duration_seconds(interval))
            # The Clash API cannot force-select inside a urltest group, so
            # the urltest sits behind a thin selector: normally the selector
            # points at "proxy-auto" and urltest does its job, and the
            # health monitor can pin a specific node via the selector when
            # the automatic choice stalls.
            urltest = {
                "type": "urltest",
                "tag": "proxy-auto",
                "outbounds": tags,
                "url": settings.get("test_url", "https://www.gstatic.com/generate_204"),
                "interval": interval,
                "tolerance": int(settings.get("urltest_tolerance", 50)),
                "idle_timeout": "%ds" % idle,
                "interrupt_exist_connections": False,
            }
            chooser = {
                "type": "selector",
                "tag": "proxy",
                "outbounds": ["proxy-auto"] + tags,
                "default": "proxy-auto",
                "interrupt_exist_connections": False,
            }
            extra_chooser = urltest
        final = "proxy"
    else:
        chooser = None
        final = "direct"

    inbounds = [{
        "type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1",
        "listen_port": int(settings.get("local_port", 1080)),
    }]

    log_cfg = {"level": settings.get("log_level", "info"), "timestamp": True}
    if settings.get("log_path"):
        log_cfg["output"] = settings["log_path"]

    rules = [
        {"action": "sniff"},
        {"protocol": "dns", "action": "hijack-dns"},
    ]
    # sing-box 1.12+ dropped embedded geoip.dat/geosite.dat, so geoip rules
    # cannot reference external databases; geo databases are Xray-only.
    if settings.get("direct_torrent"):
        rules.append({"protocol": "bittorrent", "action": "route",
                      "outbound": "direct"})
    rules.append({"ip_is_private": True, "action": "route",
                  "outbound": "direct"})

    chain = ([extra_chooser, chooser] if extra_chooser else
             ([chooser] if chooser else []))
    # Node outbounds only ship with a chooser; direct mode drops them.
    outbounds_all = (outbounds + chain) if chooser else []

    config = {
        "log": log_cfg,
        "dns": _dns_block(settings, proxy_detour=(final == "proxy")),
        "inbounds": inbounds,
        "outbounds": outbounds_all + [
            {"type": "direct", "tag": "direct"}],
        "route": {
            "rules": rules,
            "final": final,
            "auto_detect_interface": True,
            # Proxy server addresses resolve through the router DNS: they
            # must work before any secure resolver is reachable.
            "default_domain_resolver": "bootstrap",
        },
    }
    # Clash API lets the supervisor's health monitor see and steer the
    # urltest/selector group when the engine's own probing stalls.
    if chooser is not None and settings.get("clash_api_port"):
        config["experimental"] = {
            "clash_api": {
                "external_controller": "127.0.0.1:%d"
                                       % int(settings["clash_api_port"]),
            },
        }
    return config, skipped
