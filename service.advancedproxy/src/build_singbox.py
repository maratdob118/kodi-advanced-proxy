# -*- coding: utf-8 -*-
"""Build a sing-box config from neutral profiles.

Modes:
  urltest - a urltest group auto-switches by latency (interval/tolerance).
  manual  - a selector outbound pinned to the user's active profile.
Kodi-free.
"""


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


def _dns_block(settings):
    """Normalized DNS block: user server (udp/doh/dot) + duckdns local rule."""
    server = (settings.get("dns_server") or "").strip()
    strategy = (settings.get("dns_query_strategy") or "").strip()
    if server:
        # sing-box uses the first server as the implicit final resolver, so
        # the user's server answers everything except the duckdns rule.
        servers = [{"address": server}]
        final = None
    else:
        servers = [
            {"type": "udp", "tag": "remote", "server": "1.1.1.1"},
            {"type": "udp", "tag": "local", "server": "77.88.8.8"},
        ]
        final = "remote"
    # The duckdns local rule needs a "local" entry in both shapes so
    # route.default_domain_resolver stays valid.
    if not any(s.get("tag") == "local" for s in servers):
        servers.append({"type": "udp", "tag": "local", "server": "77.88.8.8"})
    block = {
        "servers": servers,
        "rules": [{"domain_suffix": [".duckdns.org"], "server": "local"}],
    }
    if strategy:
        block["strategy"] = strategy
    if final:
        block["final"] = final
    return block


def build_config(profiles, settings, active_tag=None):
    outbounds, tags, skipped = build_outbounds(profiles)
    if not tags:
        raise RuntimeError("no usable profiles for sing-box (%d skipped)" % len(skipped))

    mode = settings.get("mode", "urltest")
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
        chooser = {
            "type": "urltest",
            "tag": "proxy",
            "outbounds": tags,
            "url": settings.get("test_url", "https://www.gstatic.com/generate_204"),
            "interval": settings.get("urltest_interval", "3m"),
            "tolerance": int(settings.get("urltest_tolerance", 50)),
            "idle_timeout": "5m",
            "interrupt_exist_connections": False,
        }

    inbounds = [{
        "type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1",
        "listen_port": int(settings.get("local_port", 1080)),
    }]

    log_cfg = {"level": settings.get("log_level", "info"), "timestamp": True}
    if settings.get("log_path"):
        log_cfg["output"] = settings["log_path"]

    return {
        "log": log_cfg,
        "dns": _dns_block(settings),
        "inbounds": inbounds,
        "outbounds": outbounds + [chooser, {"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "action": "route", "outbound": "direct"},
            ],
            "final": "proxy",
            "auto_detect_interface": True,
            "default_domain_resolver": "local",
        },
    }, skipped
