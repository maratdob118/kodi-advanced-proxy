# -*- coding: utf-8 -*-
"""Parse proxy profile URIs into a neutral model usable by both engines.

Supported schemes: vless://, vmess://, ss:// (shadowsocks), hy2://
(hysteria2), trojan://, socks://, http://, wireguard://, tuic://.
The neutral dict feeds engine-specific builders (sing-box, xray) so the
parsing logic lives in exactly one place. Kodi-free.
"""
import base64
import json
from urllib.parse import unquote, parse_qs


def _g(params, key, default=""):
    return params.get(key, [default])[0]


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
    "vmess": ("vmess://",),
    "shadowsocks": ("ss://",),
    "hysteria2": ("hy2://", "hysteria2://"),
    "trojan": ("trojan://",),
    "socks": ("socks://",),
    "http": ("http://",),
    "wireguard": ("wireguard://", "wg://"),
    "tuic": ("tuic://",),
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
    if line.startswith("vmess://"):
        main, tag, params = _split(line, "vmess://")
        if "@" in main:
            # modern form: uuid@host:port?params#tag
            uuid, host, port = _hostport(main)
            return {
                "protocol": "vmess",
                "tag": tag or "vmess",
                "server": host,
                "port": port,
                "uuid": uuid,
                "alter_id": _int(_g(params, "aid", "0")),
                "security": _g(params, "security", "auto"),
                "sni": _g(params, "sni"),
                "network": _g(params, "type", "tcp"),
                "path": _g(params, "path"),
            }
        # legacy form: base64-encoded JSON
        try:
            payload = json.loads(base64.b64decode(
                main + "=" * (-len(main) % 4)).decode("utf-8"))
        except Exception:
            return None
        return {
            "protocol": "vmess",
            "tag": payload.get("ps") or tag or "vmess",
            "server": payload.get("add", ""),
            "port": int(payload.get("port", 0)),
            "uuid": payload.get("id", ""),
            "alter_id": int(payload.get("aid", 0)),
            "security": payload.get("scy", "auto"),
            "sni": payload.get("sni", ""),
            "network": payload.get("net", "tcp"),
            "path": payload.get("path", ""),
        }
    if line.startswith("ss://"):
        main, tag, params = _split(line, "ss://")
        if "@" in main:
            auth, hostport = main.rsplit("@", 1)
            host, port = hostport.rsplit(":", 1)
            if ":" in auth:
                method, password = auth.split(":", 1)
            else:
                # base64 method:password
                try:
                    decoded = base64.b64decode(
                        auth + "=" * (-len(auth) % 4)).decode("utf-8")
                    method, password = decoded.split(":", 1)
                except Exception:
                    return None
            return {
                "protocol": "shadowsocks",
                "tag": tag or "shadowsocks",
                "server": unquote(host),
                "port": int(port),
                "method": unquote(method),
                "password": unquote(password),
                "plugin": _g(params, "plugin"),
                "plugin_opts": _g(params, "plugin_opts"),
            }
        # base64 method:password@host:port
        try:
            decoded = base64.b64decode(
                main + "=" * (-len(main) % 4)).decode("utf-8")
            auth, hostport = decoded.rsplit("@", 1)
            method, password = auth.split(":", 1)
            host, port = hostport.rsplit(":", 1)
        except Exception:
            return None
        return {
            "protocol": "shadowsocks",
            "tag": tag or "shadowsocks",
            "server": host,
            "port": int(port),
            "method": method,
            "password": password,
            "plugin": _g(params, "plugin"),
            "plugin_opts": _g(params, "plugin_opts"),
        }
    if line.startswith("socks://"):
        main, tag, params = _split(line, "socks://")
        if "@" in main:
            creds, hostport = main.rsplit("@", 1)
            username, password = creds.split(":", 1) if ":" in creds else (creds, "")
        else:
            hostport, username, password = main, "", ""
        host, port = hostport.rsplit(":", 1)
        return {
            "protocol": "socks",
            "tag": tag or "socks",
            "server": unquote(host),
            "port": int(port),
            "username": unquote(username),
            "password": unquote(password),
        }
    if line.startswith("http://"):
        main, tag, params = _split(line, "http://")
        if "@" not in main:
            return None  # plain http:// URL is a subscription, not a profile
        creds, hostport = main.rsplit("@", 1)
        username, password = creds.split(":", 1) if ":" in creds else (creds, "")
        host, port = hostport.rsplit(":", 1)
        return {
            "protocol": "http",
            "tag": tag or "http",
            "server": unquote(host),
            "port": int(port),
            "username": unquote(username),
            "password": unquote(password),
        }
    if line.startswith("wireguard://") or line.startswith("wg://"):
        scheme = "wireguard://" if line.startswith("wireguard://") else "wg://"
        main, tag, params = _split(line, scheme)
        private_key, host, port = _hostport(main)
        return {
            "protocol": "wireguard",
            "tag": tag or "wireguard",
            "server": host,
            "port": port,
            "private_key": private_key,
            "public_key": _g(params, "pk"),
            "preshared_key": _g(params, "preshared_key"),
            "local_address": _g(params, "local_address"),
            "reserved": _g(params, "reserved"),
        }
    if line.startswith("tuic://"):
        main, tag, params = _split(line, "tuic://")
        uuid, host, port = _hostport(main)
        return {
            "protocol": "tuic",
            "tag": tag or "tuic",
            "server": host,
            "port": port,
            "uuid": uuid,
            "password": _g(params, "password"),
            "congestion_control": _g(params, "congestion_control"),
            "sni": _g(params, "sni", host),
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
