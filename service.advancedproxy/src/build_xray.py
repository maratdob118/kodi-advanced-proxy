# -*- coding: utf-8 -*-
"""Build an Xray-core config from neutral profiles.

Xray does not support Hysteria2, so those profiles are skipped (reported).
Modes:
  urltest - a leastPing balancer + burstObservatory (latency auto-switch).
  manual  - routing pinned to the user's active profile.
Kodi-free.
"""

_ENGINE_UNSUPPORTED = {"hysteria2"}


def _stream(p):
    sec = p.get("security", "none")
    ss = {"network": "tcp"}
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


def build_config(profiles, settings, active_tag=None):
    outbounds, tags, skipped = build_outbounds(profiles)
    if not tags:
        raise RuntimeError("no usable profiles for xray (%d skipped)" % len(skipped))

    mode = settings.get("mode", "urltest")
    rules = []
    balancer = None
    observatory = None
    if mode == "manual":
        default = active_tag if active_tag in tags else tags[0]
        rules.append({"type": "field", "network": "tcp,udp", "outboundTag": default})
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
        rules.append({"type": "field", "network": "tcp,udp", "balancerTag": "proxy"})

    config = {
        "log": {"loglevel": settings.get("log_level", "info").replace("warn", "warning")},
        "dns": {"servers": ["1.1.1.1", "77.88.8.8", "localhost"]},
        "inbounds": [
            {
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "port": int(settings.get("local_port", 1080)),
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            },
        ],
        "outbounds": outbounds + [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {"domainStrategy": "IPIfNonMatch", "rules": rules},
    }
    if balancer:
        config["routing"]["balancers"] = [balancer]
    if observatory:
        config["burstObservatory"] = observatory
    return config, skipped
