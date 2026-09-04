# -*- coding: utf-8 -*-
"""DNS parsing, presets and bootstrap resolver discovery. Kodi-free.

The bootstrap resolver is the DNS the router handed out via DHCP (what
/etc/resolv.conf carries on Linux/LibreELEC). It resolves the hostnames of
hostname-based DoH/DoT servers and the proxy server addresses, so secure DNS
never has a chicken-and-egg problem and plain UDP spoofing only touches
local lookups.
"""
import os
import re
import socket

# Ordered preset list; index == the value stored by the dns_preset setting.
# Keep in sync with the <option> order in settings.xml and strings.po.
DNS_PRESETS = [
    ("auto", ""),  # router/system DNS only
    ("cloudflare-doh", "https://1.1.1.1/dns-query"),
    ("cloudflare-dot", "tls://1.1.1.1"),
    ("cloudflare-udp", "1.1.1.1"),
    ("google-doh", "https://8.8.8.8/dns-query"),
    ("google-dot", "tls://8.8.8.8"),
    ("google-udp", "8.8.8.8"),
    ("quad9-doh", "https://dns.quad9.net/dns-query"),
    ("quad9-dot", "tls://dns.quad9.net"),
    ("quad9-udp", "9.9.9.9"),
    ("adguard-doh", "https://dns.adguard-dns.com/dns-query"),
    ("adguard-dot", "tls://dns.adguard-dns.com"),
    ("adguard-udp", "94.140.14.14"),
    ("yandex-doh", "https://common.doh.dns.yandex.net/dns-query"),
    ("yandex-udp", "77.88.8.8"),
    ("cleanbrowsing-doh", "https://doh.cleanbrowsing.org/dns-query/security-filter/"),
    ("custom", None),  # free-form dns_server field
]
CUSTOM_PRESET = "custom"

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def is_ipv4(value):
    return bool(_IPV4_RE.match(value or ""))


def parse_dns_server(value):
    """Normalize a DNS server setting.

    Returns one of:
      {"kind": "udp", "host": ip, "port": 53}
      {"kind": "doh", "host": host, "port": 443, "path": "/dns-query"}
      {"kind": "dot", "host": host, "port": 853}
    None for empty or unrecognized values.
    """
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith("https://"):
        rest = value[len("https://"):]
        host, _, path = rest.partition("/")
        if not host:
            return None
        return {"kind": "doh", "host": host, "port": 443,
                "path": "/" + path if path else "/dns-query"}
    if value.startswith("tls://"):
        hostport = value[len("tls://"):].strip("/")
        if not hostport:
            return None
        host, _, port = hostport.partition(":")
        try:
            port = int(port) if port else 853
        except ValueError:
            return None
        return {"kind": "dot", "host": host, "port": port}
    if value.startswith("udp://"):
        value = value[len("udp://"):]
    host, _, port = value.partition(":")
    if is_ipv4(host):
        try:
            port = int(port) if port else 53
        except ValueError:
            return None
        return {"kind": "udp", "host": host, "port": port}
    return None


def preset_server(preset_id, custom_value=""):
    """Resolve a preset id to its DNS server string.

    "custom" returns CUSTOM_VALUE, "auto" and unknown ids return "".
    """
    for name, server in DNS_PRESETS:
        if name == preset_id:
            return custom_value if server is None else server
    return ""


def preset_id_by_index(index):
    try:
        return DNS_PRESETS[int(index)][0]
    except (IndexError, TypeError, ValueError):
        return DNS_PRESETS[0][0]


def preset_index_by_id(preset_id):
    for i, (name, _) in enumerate(DNS_PRESETS):
        if name == preset_id:
            return i
    return 0


def system_dns_servers(resolv_path="/etc/resolv.conf"):
    """Router-provided DNS servers from resolv.conf (the DHCP answer).

    Skips loopback entries (a local stub resolver like systemd-resolved is
    itself backed by the router DNS). Returns a list of IPv4 strings,
    [] when nothing usable is found.
    """
    servers = []
    try:
        with open(resolv_path) as f:
            for line in f:
                line = line.split("#", 1)[0].split(";", 1)[0].strip()
                if not line.lower().startswith("nameserver"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                ip = parts[1].split("%")[0]  # strip IPv6 zone id
                if is_ipv4(ip) and not ip.startswith("127."):
                    servers.append(ip)
    except OSError:
        pass
    return list(dict.fromkeys(servers))


def resolve_hostname(host, servers=None, timeout=3.0):
    """Resolve HOST through the system (router) resolver. Returns IPv4 list.

    Used to pin hostname-based DoH servers in Xray `hosts` so the engine
    itself never needs plaintext bootstrap lookups. Failures return [].
    """
    if is_ipv4(host):
        return [host]
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except (socket.gaierror, socket.error, OSError):
        return []
    finally:
        socket.setdefaulttimeout(old_timeout)
    ips = [info[4][0] for info in infos]
    return list(dict.fromkeys(ips))
