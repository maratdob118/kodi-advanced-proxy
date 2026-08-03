# -*- coding: utf-8 -*-
"""Parse proxy profile URIs into a neutral model usable by both engines.

Supported schemes: vless://, hy2:// (hysteria2), trojan://.
The neutral dict feeds engine-specific builders (sing-box, xray) so the
parsing logic lives in exactly one place. Kodi-free.
"""
from urllib.parse import unquote, parse_qs


def _g(params, key, default=""):
    return params.get(key, [default])[0]


def _split(line, scheme):
    rest = line[len(scheme):]
    if "#" in rest:
        main, tag = rest.rsplit("#", 1)
    else:
        main, tag = rest, ""
    main, _, query = main.partition("?")
    return main, unquote(tag), parse_qs(query)


def _hostport(userinfo_host):
    userinfo, hostport = userinfo_host.rsplit("@", 1)
    host, port = hostport.rsplit(":", 1)
    return unquote(userinfo), host, int(port)


_PROTOCOL_PREFIXES = {
    "vless": ("vless://",),
    "hysteria2": ("hy2://", "hysteria2://"),
    "trojan": ("trojan://",),
}


def _disabled(line, disabled_protocols):
    for protocol in disabled_protocols:
        prefixes = _PROTOCOL_PREFIXES.get(protocol, (protocol + "://",))
        if any(line.startswith(prefix) for prefix in prefixes):
            return True
    return False


def parse_uri(line, disabled_protocols=()):
    """Return a neutral profile dict, or None if the scheme is unknown
    or the protocol is disabled."""
    line = line.strip()
    if _disabled(line, disabled_protocols):
        return None
    if line.startswith("vless://"):
        main, tag, params = _split(line, "vless://")
        uuid, host, port = _hostport(main)
        return {
            "protocol": "vless",
            "tag": tag or "vless",
            "server": host,
            "port": port,
            "uuid": uuid,
            "flow": _g(params, "flow"),
            "security": _g(params, "security", "none"),
            "sni": _g(params, "sni"),
            "fingerprint": _g(params, "fp", "chrome"),
            "reality_public_key": _g(params, "pbk"),
            "reality_short_id": _g(params, "sid"),
            "network": _g(params, "type", "tcp"),
            "path": _g(params, "path"),
        }
    if line.startswith("hy2://") or line.startswith("hysteria2://"):
        scheme = "hysteria2://" if line.startswith("hysteria2://") else "hy2://"
        main, tag, params = _split(line, scheme)
        main = main.rstrip("/")
        password, host, port = _hostport(main)
        return {
            "protocol": "hysteria2",
            "tag": tag or "hysteria2",
            "server": host,
            "port": port,
            "password": password,
            "security": "tls",
            "sni": _g(params, "sni", host),
            "fingerprint": _g(params, "fp", "chrome"),
            "network": "udp",
            "path": "",
        }
    if line.startswith("trojan://"):
        main, tag, params = _split(line, "trojan://")
        password, host, port = _hostport(main)
        return {
            "protocol": "trojan",
            "tag": tag or "trojan",
            "server": host,
            "port": port,
            "password": password,
            "security": _g(params, "security", "tls"),
            "sni": _g(params, "sni"),
            "fingerprint": _g(params, "fp", "chrome"),
            "reality_public_key": _g(params, "pbk"),
            "reality_short_id": _g(params, "sid"),
            "network": _g(params, "type", "tcp"),
            "path": _g(params, "path"),
        }
    return None


def is_subscription_url(line):
    """True when LINE is an http(s) URL that is not a profile link."""
    line = (line or "").strip()
    if parse_uri(line) is not None:
        return False
    return line.startswith("http://") or line.startswith("https://")


def parse_lines(lines, disabled_protocols=()):
    """Parse many URIs -> (profiles, skipped). skipped = [(line, reason)]."""
    profiles, skipped = [], []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            if _disabled(line, disabled_protocols):
                skipped.append((line[:40], "disabled-protocol"))
                continue
            p = parse_uri(line)
            if p is None:
                skipped.append((line[:40], "unknown-scheme"))
                continue
            profiles.append(p)
        except Exception as e:
            skipped.append((line[:40], "parse-error:%s" % e))
    return profiles, skipped
