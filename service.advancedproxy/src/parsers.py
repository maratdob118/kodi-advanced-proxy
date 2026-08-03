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


_NON_PROXY = {
    # sing-box outbound types that are never profiles
    "direct", "block", "dns", "selector", "urltest", "cache-file",
    "shadowtls", "ssh", "tor", "wireguard-client",
    # Xray outbound protocols that are never profiles
    "freedom", "blackhole", "dokodemo", "loopback", "tun", "reverse",
}


def _sb_outbound(out):
    """sing-box outbound (outbounds[].type) -> neutral profile or None."""
    proto = out.get("type")
    if proto in _NON_PROXY:
        return None
    common = {
        "protocol": proto,
        "tag": out.get("tag") or "sing-box",
        "server": out.get("server", ""),
        "port": out.get("server_port", 0),
    }
    if proto == "vless":
        common.update({
            "uuid": out.get("uuid", ""),
            "flow": out.get("flow", ""),
            "network": out.get("network", "tcp"),
            "path": (out.get("transport") or {}).get("path", ""),
        })
    elif proto == "vmess":
        common.update({
            "uuid": out.get("uuid", ""),
            "alter_id": out.get("alter_id", 0),
            "network": out.get("network", "tcp"),
            "path": (out.get("transport") or {}).get("path", ""),
        })
    elif proto == "trojan":
        common.update({
            "password": out.get("password", ""),
            "network": out.get("network", "tcp"),
            "path": (out.get("transport") or {}).get("path", ""),
        })
    elif proto == "shadowsocks":
        common.update({
            "method": out.get("method", ""),
            "password": out.get("password", ""),
        })
    elif proto == "hysteria2":
        common.update({
            "password": out.get("password", ""),
            "sni": (out.get("tls") or {}).get("server_name", ""),
            "fingerprint": (out.get("tls") or {}).get("fingerprint", "chrome"),
        })
    elif proto == "wireguard":
        common.update({
            "private_key": out.get("private_key", ""),
            "public_key": (out.get("peer_public_key") or out.get("public_key") or ""),
            "local_address": out.get("local_address", ""),
        })
    elif proto == "tuic":
        common.update({
            "uuid": out.get("uuid", ""),
            "password": out.get("password", ""),
            "sni": (out.get("tls") or {}).get("server_name", ""),
            "congestion_control": out.get("congestion_control", ""),
        })
    elif proto in ("socks", "http"):
        common.update({
            "username": out.get("username", ""),
            "password": out.get("password", ""),
        })
    else:
        return None
    tls = out.get("tls") or {}
    if tls.get("enabled"):
        common["security"] = "tls"
        common["sni"] = tls.get("server_name", "")
        common["fingerprint"] = tls.get("fingerprint", "chrome")
        reality = tls.get("reality") or {}
        if reality.get("enabled"):
            common["security"] = "reality"
            common["reality_public_key"] = reality.get("public_key", "")
            common["reality_short_id"] = reality.get("short_id", "")
    return common


def _xray_user(out):
    """Xray outbound (outbounds[].protocol) -> neutral profile or None."""
    proto = out.get("protocol")
    if proto in _NON_PROXY:
        return None
    if proto == "hysteria":
        # Xray hysteria = hysteria2 (QUIC transport, version 2)
        settings = out.get("settings") or {}
        stream = out.get("streamSettings") or {}
        common = {
            "protocol": "hysteria2",
            "tag": out.get("tag") or "xray",
            "server": settings.get("address", ""),
            "port": settings.get("port", 0),
            "password": (stream.get("hysteriaSettings") or {}).get("auth", ""),
            "sni": (stream.get("tlsSettings") or {})
                   .get("serverName", ""),
        }
        return common
    if proto == "tuic":
        return None  # Xray has no tuic in any version
    settings = out.get("settings") or {}
    stream = out.get("streamSettings") or {}
    common = {
        "protocol": proto,
        "tag": out.get("tag") or "xray",
        "server": "",
        "port": 0,
    }
    if proto == "vless":
        vnext = (settings.get("vnext") or [{}])[0]
        user = (vnext.get("users") or [{}])[0]
        common.update({
            "server": vnext.get("address", ""),
            "port": vnext.get("port", 0),
            "uuid": user.get("id", ""),
            "flow": user.get("flow", ""),
        })
    elif proto == "vmess":
        vnext = (settings.get("vnext") or [{}])[0]
        user = (vnext.get("users") or [{}])[0]
        common.update({
            "server": vnext.get("address", ""),
            "port": vnext.get("port", 0),
            "uuid": user.get("id", ""),
            "alter_id": user.get("alterId", 0),
            "security": user.get("security", "auto"),
        })
    elif proto == "trojan":
        server = (settings.get("servers") or [{}])[0]
        common.update({
            "server": server.get("address", ""),
            "port": server.get("port", 0),
            "password": server.get("password", ""),
        })
    elif proto == "shadowsocks":
        server = (settings.get("servers") or [{}])[0]
        common.update({
            "server": server.get("address", ""),
            "port": server.get("port", 0),
            "method": server.get("method", ""),
            "password": server.get("password", ""),
        })
    elif proto == "wireguard":
        peer = (settings.get("peers") or [{}])[0]
        common.update({
            "server": peer.get("endpoint", "").rsplit(":", 1)[0],
            "port": _int(peer.get("endpoint", "").rsplit(":", 1)[-1]),
            "private_key": settings.get("secretKey", ""),
            "public_key": peer.get("publicKey", ""),
            "local_address": ",".join(settings.get("address", [])),
        })
    elif proto in ("socks", "http"):
        server = (settings.get("servers") or [{}])[0]
        user = (server.get("users") or [{}])[0]
        common.update({
            "server": server.get("address", ""),
            "port": server.get("port", 0),
            "username": user.get("user", ""),
            "password": user.get("pass", ""),
        })
    else:
        return None
    sec = stream.get("security")
    if sec in ("tls", "reality"):
        common["security"] = sec
        tls = stream.get("tlsSettings") or {}
        reality = stream.get("realitySettings") or {}
        common["sni"] = tls.get("serverName") or reality.get("serverName", "")
        common["fingerprint"] = reality.get("fingerprint", "chrome")
        if sec == "reality":
            common["reality_public_key"] = reality.get("publicKey", "")
            common["reality_short_id"] = reality.get("shortId", "")
    network = stream.get("network")
    if network and network != "tcp":
        common["network"] = network
        ws = stream.get("wsSettings") or {}
        if ws.get("path"):
            common["path"] = ws["path"]
    return common


def parse_config(text):
    """Extract neutral proxy profiles from a JSON engine config.

    Accepts a sing-box config (outbounds[].type), an Xray config
    (outbounds[].protocol), a JSON array of full configs, or an already
    parsed dict/list. Returns (profiles, skipped). Profiles carry
    protocol/server/port (no uri). Raises ValueError on invalid JSON.
    """
    if isinstance(text, (dict, list)):
        data = text
    else:
        try:
            data = json.loads(text if isinstance(text, str)
                              else text.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ValueError("config is not valid JSON: %s" % e)
    documents = data if isinstance(data, list) else [data]
    profiles, skipped = [], []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        outbounds = doc.get("outbounds") or []
        remarks = doc.get("remarks", "")
        for out in outbounds:
            if not isinstance(out, dict):
                continue
            if "type" in out:
                profile = _sb_outbound(out)
            elif "protocol" in out:
                if out.get("protocol") == "tuic":
                    skipped.append((out.get("tag") or "?",
                                    "xray-unsupported:tuic"))
                    continue
                profile = _xray_user(out)
            else:
                profile = None
            if profile is None:
                skipped.append((out.get("tag") or "?", "non-proxy"))
                continue
            if not profile.get("tag") or profile["tag"] in ("sing-box", "xray"):
                profile["tag"] = remarks or profile["tag"]
            profiles.append(profile)
    return profiles, skipped


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
