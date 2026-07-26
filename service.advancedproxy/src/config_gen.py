# -*- coding: utf-8 -*-
"""Build a sing-box config from a subscription URL.

Kodi-free: pure stdlib, returns a config dict. The subscription returns one
proxy URI per line (vless://, hy2://, trojan://). Unsupported protocols and
transports are skipped per `skip_protocols`.

Generates: individual outbounds + a urltest group (auto-switching) + inbounds
(local SOCKS5 + HTTP, optional LAN mixed) + dns + route (sing-box 1.13 format).
"""
import urllib.parse
import urllib.request
from urllib.parse import unquote, parse_qs


def fetch_subscription(sub_url, timeout=20):
    with urllib.request.urlopen(sub_url, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", "replace")
    return [l.strip() for l in data.strip().split("\n") if l.strip()]


def _g(params, key, default=""):
    return params.get(key, [default])[0]


def _parse_vless(rest, skip, tag_default):
    if "#" in rest:
        main, tag = rest.rsplit("#", 1)
    else:
        main, tag = rest, tag_default
    tag = unquote(tag)
    userinfo_host, _, query = main.partition("?")
    uuid, hostport = userinfo_host.rsplit("@", 1)
    host, port = hostport.rsplit(":", 1)
    params = parse_qs(query)

    net_type = _g(params, "type", "tcp")
    if net_type in skip or net_type == "xhttp":
        return None, "transport:%s" % net_type

    ob = {
        "type": "vless",
        "tag": tag,
        "server": host,
        "server_port": int(port),
        "uuid": uuid,
        "network": "tcp",
    }
    flow = _g(params, "flow")
    if flow:
        ob["flow"] = flow

    security = _g(params, "security", "none")
    sni = _g(params, "sni")
    fp = _g(params, "fp", "chrome")
    if security == "reality":
        ob["tls"] = {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": fp},
            "reality": {
                "enabled": True,
                "public_key": _g(params, "pbk"),
                "short_id": _g(params, "sid"),
            },
        }
    elif security == "tls":
        ob["tls"] = {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": fp},
        }
    return ob, None


def _parse_hysteria2(rest, tag_default):
    if "#" in rest:
        main, tag = rest.rsplit("#", 1)
    else:
        main, tag = rest, tag_default
    tag = unquote(tag)
    userinfo_host, _, query = main.partition("?")
    userinfo_host = userinfo_host.rstrip("/")
    password, hostport = userinfo_host.rsplit("@", 1)
    host, port = hostport.rsplit(":", 1)
    params = parse_qs(query)
    return {
        "type": "hysteria2",
        "tag": tag,
        "server": host,
        "server_port": int(port),
        "password": unquote(password),
        "tls": {"enabled": True, "server_name": _g(params, "sni", host)},
    }, None


def _parse_trojan(rest, skip, tag_default):
    if "trojan" in skip:
        return None, "protocol:trojan"
    if "#" in rest:
        main, tag = rest.rsplit("#", 1)
    else:
        main, tag = rest, tag_default
    tag = unquote(tag)
    userinfo_host, _, query = main.partition("?")
    password, hostport = userinfo_host.rsplit("@", 1)
    host, port = hostport.rsplit(":", 1)
    params = parse_qs(query)
    ob = {
        "type": "trojan",
        "tag": tag,
        "server": host,
        "server_port": int(port),
        "password": unquote(password),
        "network": "tcp",
    }
    security = _g(params, "security", "tls")
    sni = _g(params, "sni")
    fp = _g(params, "fp", "chrome")
    if security == "reality":
        ob["tls"] = {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": fp},
            "reality": {
                "enabled": True,
                "public_key": _g(params, "pbk"),
                "short_id": _g(params, "sid"),
            },
        }
    elif security == "tls":
        ob["tls"] = {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": fp},
        }
    return ob, None


def parse_lines(lines, skip_protocols=None):
    """Parse subscription lines -> (outbounds, tags, skipped list)."""
    skip = set()
    for p in (skip_protocols or "").split(","):
        p = p.strip().lower()
        if p:
            skip.add(p)

    outbounds, tags, skipped = [], [], []
    for i, line in enumerate(lines):
        try:
            if line.startswith("vless://"):
                ob, reason = _parse_vless(line[len("vless://"):], skip, "vless-%d" % i)
            elif line.startswith("hy2://"):
                ob, reason = _parse_hysteria2(line[len("hy2://"):], "hy2-%d" % i)
            elif line.startswith("hysteria2://"):
                ob, reason = _parse_hysteria2(line[len("hysteria2://"):], "hy2-%d" % i)
            elif line.startswith("trojan://"):
                ob, reason = _parse_trojan(line[len("trojan://"):], skip, "trojan-%d" % i)
            else:
                skipped.append((line[:40], "unknown-scheme"))
                continue

            if ob is None:
                skipped.append((line[:40], reason))
                continue
            outbounds.append(ob)
            tags.append(ob["tag"])
        except Exception as e:
            skipped.append((line[:40], "parse-error:%s" % e))
    return outbounds, tags, skipped


def build_config(outbounds, tags, settings):
    """Assemble the full sing-box config dict."""
    log_path = settings.get("log_path")
    log_cfg = {
        "level": settings.get("log_level", "info"),
        "timestamp": True,
    }
    if log_path:
        log_cfg["output"] = log_path

    inbounds = [
        {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1",
         "listen_port": int(settings.get("local_port", 1080))},
    ]
    if settings.get("lan_mixed_enabled"):
        inbounds.append({
            "type": "mixed", "tag": "lan-mixed-in", "listen": "0.0.0.0",
            "listen_port": int(settings.get("lan_mixed_port", 1080)),
        })

    urltest = {
        "type": "urltest",
        "tag": "proxy-auto",
        "outbounds": tags,
        "url": settings.get("test_url", "https://www.gstatic.com/generate_204"),
        "interval": settings.get("urltest_interval", "3m"),
        "tolerance": int(settings.get("urltest_tolerance", 50)),
        "idle_timeout": "5m",
        "interrupt_exist_connections": bool(settings.get("interrupt_connections", True)),
    }

    return {
        "log": log_cfg,
        "dns": {
            "servers": [
                {"type": "udp", "tag": "remote", "server": "1.1.1.1"},
                {"type": "udp", "tag": "local", "server": "77.88.8.8"},
            ],
            "rules": [
                {"domain_suffix": [".duckdns.org"], "server": "local"},
            ],
            "final": "remote",
        },
        "inbounds": inbounds,
        "outbounds": outbounds + [urltest, {"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
            ],
            "final": "proxy-auto",
            "auto_detect_interface": True,
            "default_domain_resolver": "local",
        },
    }


def generate(sub_url, settings):
    """Fetch subscription, parse, build config. Returns (config, stats)."""
    lines = fetch_subscription(sub_url)
    outbounds, tags, skipped = parse_lines(lines, settings.get("skip_protocols", ""))
    if not tags:
        raise RuntimeError("No usable outbounds parsed from subscription (%d skipped)" % len(skipped))
    config = build_config(outbounds, tags, settings)
    stats = {
        "fetched": len(lines),
        "used": len(tags),
        "skipped": skipped,
    }
    return config, stats
