# -*- coding: utf-8 -*-
"""Unit tests for the Kodi-free core modules (dual-engine redesign).

Run:  python3 tests/test_core.py
No Kodi required; xbmc modules are never imported by the tested code.
"""
import contextlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


class _LogRecorder(object):
    """Records log messages for assertions."""
    
    def __init__(self):
        self.entries = []
    
    def __call__(self, msg, level="info"):
        self.entries.append((level, msg))


class _FakeProcessForStop(object):
    """Fake subprocess.Popen for BinaryManager.stop() tests."""
    
    def __init__(self):
        self._exit_delay = 0
        self._terminated = False
        self._killed = False
        self._exit_code = None
        self._exit_before_terminate = False
        self._calls = []
        self.pid = 12345
        self.returncode = None
        self._wait_calls = 0
    
    def terminate(self):
        self._calls.append("terminate")
        if self._exit_before_terminate:
            # The process is already gone: simulate the race window where it
            # exits between is_running() and terminate().
            self._exit_code = 0
            self.returncode = 0
            raise ProcessLookupError(3, "No such process")
        self._terminated = True
    
    def kill(self):
        self._calls.append("kill")
        if self._exit_before_terminate:
            # The process is gone: the same race makes kill() fail too.
            raise ProcessLookupError(3, "No such process")
        self._killed = True
    
    def poll(self):
        if self._exit_delay > 0:
            self._exit_delay -= 1
            return None
        if self._terminated and self._exit_code is None:
            self._exit_code = 0
        if self._exit_code is not None and self.returncode is None:
            self.returncode = self._exit_code
        return self._exit_code
    
    def wait(self, timeout=None):
        self._wait_calls += 1
        if self._wait_calls <= self._exit_delay:
            raise subprocess.TimeoutExpired([], timeout)
        if (self._terminated or self._killed) and self._exit_code is None:
            self._exit_code = 0
        return self._exit_code


class _FakeBinaryClock(object):
    """`time` module stand-in for BinaryManager polling bounds."""

    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _write_executable(path):
    with open(path, "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)
    return path


def _popen_returning(fake_proc):
    """Patch side_effect for subprocess.Popen returning fake_proc on the engine
    launch while passing the custom-binary `version` probe through to the real
    Popen (ensure_binary validates custom paths by running them)."""
    real_popen = subprocess.Popen

    def _popen(args, **kwargs):
        if args[-1] == "version":
            return real_popen(args, **kwargs)
        return fake_proc
    return _popen

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "service.advancedproxy", "src")
ADDON_DIR = os.path.abspath(os.path.join(HERE, "..", "service.advancedproxy"))
sys.path.insert(0, os.path.abspath(SRC))

import binary_manager  # noqa: E402
import build_singbox  # noqa: E402
import build_xray  # noqa: E402
import helpers  # noqa: E402
import osarch  # noqa: E402
import parsers  # noqa: E402
import port_utils  # noqa: E402
import profiles  # noqa: E402
import supervisor  # noqa: E402

VLESS = ("vless://701b248a-248c-4457-b74e-7a376812a355@bigping.duckdns.org:443"
         "?encryption=none&security=reality&flow=xtls-rprx-vision&sni=vkvideo.ru"
         "&fp=chrome&pbk=PBK&sid=SID&type=tcp#AUTO:VLESS")
HY2 = "hy2://pass123@bigping.duckdns.org:443/?sni=bigping.duckdns.org#AUTO:Hysteria2"
TROJAN = ("trojan://pw@bigping.duckdns.org:443?security=reality&sni=security.ubuntu.com"
          "&fp=chrome&pbk=PBK2&sid=SID2#AUTO:Trojan")
XHTTP = ("vless://u@bigping-uae.duckdns.org:443?security=tls&type=xhttp"
         "&path=/xhttp&sni=bigping-uae.duckdns.org#UAE:xHTTP")


class TestParsers(unittest.TestCase):
    def test_vless(self):
        p = parsers.parse_uri(VLESS)
        self.assertEqual(p["protocol"], "vless")
        self.assertEqual(p["tag"], "AUTO:VLESS")
        self.assertEqual(p["uuid"], "701b248a-248c-4457-b74e-7a376812a355")
        self.assertEqual(p["flow"], "xtls-rprx-vision")
        self.assertEqual(p["security"], "reality")
        self.assertEqual(p["reality_public_key"], "PBK")

    def test_hy2(self):
        p = parsers.parse_uri(HY2)
        self.assertEqual(p["protocol"], "hysteria2")
        self.assertEqual(p["password"], "pass123")

    def test_trojan(self):
        p = parsers.parse_uri(TROJAN)
        self.assertEqual(p["protocol"], "trojan")
        self.assertEqual(p["security"], "reality")

    def test_unknown(self):
        self.assertIsNone(parsers.parse_uri("ss://whatever"))

    def test_parse_lines(self):
        profs, skipped = parsers.parse_lines([VLESS, HY2, "garbage", TROJAN])
        self.assertEqual(len(profs), 3)
        self.assertEqual(len(skipped), 1)

    def test_urlencoded_password(self):
        p = parsers.parse_uri("hy2://a%2Bb%2F@h:443/?sni=h#T:Hy2")
        self.assertEqual(p["password"], "a+b/")

    def test_disabled_protocols_vless(self):
        self.assertIsNone(parsers.parse_uri(VLESS, disabled_protocols=("vless",)))

    def test_disabled_protocols_trojan(self):
        self.assertIsNone(parsers.parse_uri(TROJAN, disabled_protocols=("trojan",)))

    def test_disabled_protocols_hysteria2(self):
        self.assertIsNone(parsers.parse_uri(HY2, disabled_protocols=("hysteria2",)))

    def test_disabled_protocols_does_not_affect_others(self):
        p = parsers.parse_uri(VLESS, disabled_protocols=("trojan",))
        self.assertIsNotNone(p)

    def test_parse_lines_reports_disabled_as_skipped_not_error(self):
        profs, skipped = parsers.parse_lines(
            [VLESS, TROJAN], disabled_protocols=("trojan",))
        self.assertEqual(len(profs), 1)
        self.assertEqual(len(skipped), 1)
        self.assertIn("disabled", skipped[0][1])

    def test_is_subscription_url_https(self):
        self.assertTrue(parsers.is_subscription_url("https://example.com/sub"))

    def test_is_subscription_url_http(self):
        self.assertTrue(parsers.is_subscription_url("http://example.com/sub"))

    def test_is_subscription_url_false_for_profile(self):
        self.assertFalse(parsers.is_subscription_url(VLESS))

    def test_is_subscription_url_false_for_junk(self):
        self.assertFalse(parsers.is_subscription_url("not a url at all"))

    def test_vmess_modern_form(self):
        p = parsers.parse_uri(
            "vmess://uuid-1111@h.example:443?security=auto&type=tcp#VM:T1")
        self.assertEqual(p["protocol"], "vmess")
        self.assertEqual(p["tag"], "VM:T1")
        self.assertEqual(p["server"], "h.example")
        self.assertEqual(p["port"], 443)
        self.assertEqual(p["uuid"], "uuid-1111")

    def test_vmess_base64_json_form(self):
        import base64, json
        payload = json.dumps({
            "v": "2", "ps": "VM:B64", "add": "h2.example", "port": 8443,
            "id": "uuid-2222", "aid": 2, "net": "ws", "path": "/ws",
            "scy": "auto", "sni": "h2.example",
        }).encode()
        uri = "vmess://%s" % base64.b64encode(payload).decode()
        p = parsers.parse_uri(uri)
        self.assertEqual(p["protocol"], "vmess")
        self.assertEqual(p["tag"], "VM:B64")
        self.assertEqual(p["server"], "h2.example")
        self.assertEqual(p["port"], 8443)
        self.assertEqual(p["uuid"], "uuid-2222")
        self.assertEqual(p["alter_id"], 2)
        self.assertEqual(p["network"], "ws")
        self.assertEqual(p["path"], "/ws")

    def test_shadowsocks_plain_form(self):
        p = parsers.parse_uri("ss://chacha20-ietf-poly1305:pass@h.example:8388#SS:1")
        self.assertEqual(p["protocol"], "shadowsocks")
        self.assertEqual(p["tag"], "SS:1")
        self.assertEqual(p["method"], "chacha20-ietf-poly1305")
        self.assertEqual(p["password"], "pass")
        self.assertEqual(p["port"], 8388)

    def test_shadowsocks_base64_form(self):
        import base64
        auth = base64.b64encode(b"aes-256-gcm:pw").decode()
        p = parsers.parse_uri("ss://%s@h.example:8388#SS:2" % auth)
        self.assertEqual(p["protocol"], "shadowsocks")
        self.assertEqual(p["method"], "aes-256-gcm")
        self.assertEqual(p["password"], "pw")

    def test_socks(self):
        p = parsers.parse_uri("socks://user:pass@h.example:1080#SOCKS:1")
        self.assertEqual(p["protocol"], "socks")
        self.assertEqual(p["username"], "user")
        self.assertEqual(p["password"], "pass")

    def test_http(self):
        p = parsers.parse_uri("http://user:pass@h.example:8080#HTTP:1")
        self.assertEqual(p["protocol"], "http")
        self.assertEqual(p["username"], "user")
        self.assertEqual(p["password"], "pass")

    def test_wireguard(self):
        p = parsers.parse_uri(
            "wireguard://privatekey123@h.example:51820"
            "?pk=peerpubkey456&local_address=10.0.0.2/32#WG:1")
        self.assertEqual(p["protocol"], "wireguard")
        self.assertEqual(p["private_key"], "privatekey123")
        self.assertEqual(p["public_key"], "peerpubkey456")
        self.assertEqual(p["local_address"], "10.0.0.2/32")

    def test_tuic(self):
        p = parsers.parse_uri(
            "tuic://uuid-3333@h.example:443?password=pw&congestion_control=bbr"
            "&sni=h.example#TUIC:1")
        self.assertEqual(p["protocol"], "tuic")
        self.assertEqual(p["uuid"], "uuid-3333")
        self.assertEqual(p["password"], "pw")
        self.assertEqual(p["congestion_control"], "bbr")
        self.assertEqual(p["sni"], "h.example")

    def test_legacy_schemes_skipped(self):
        profs, skipped = parsers.parse_lines(
            ["ssr://base64stuff", "shadowtls://x@h:443", "naive://u@h:443"])
        self.assertEqual(profs, [])
        self.assertEqual(len(skipped), 3)

    def test_disabled_protocols_new_schemes(self):
        for uri, proto in (
                ("vmess://u@h:443#T", "vmess"),
                ("ss://aes-256-gcm:p@h:8388#T", "shadowsocks"),
                ("socks://h:1080#T", "socks"),
                ("http://user:pass@h:8080#T", "http"),
                ("wireguard://k@h:51820#T", "wireguard"),
                ("tuic://u@h:443#T", "tuic")):
            self.assertIsNone(parsers.parse_uri(uri, disabled_protocols=(proto,)),
                              "%s must be filterable" % proto)


class TestProfileStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = profiles.ProfileStore(os.path.join(self.tmp, "profiles.json"))

    def test_add(self):
        p, err = self.store.add_uri(VLESS)
        self.assertIsNone(err)
        self.assertEqual(p["tag"], "AUTO:VLESS")
        self.assertEqual(self.store.active_tag, "AUTO:VLESS")

    def test_dedup_by_tag(self):
        self.store.add_uri(VLESS)
        self.store.add_uri(VLESS)
        self.assertEqual(len(self.store.profiles), 1)

    def test_toggle(self):
        self.store.add_uri(VLESS)
        self.store.add_uri(HY2)
        self.store.toggle("AUTO:VLESS")
        self.assertFalse(self.store.get("AUTO:VLESS")["enabled"])
        self.assertEqual(len(self.store.enabled()), 1)

    def test_remove_updates_active(self):
        self.store.add_uri(VLESS)
        self.store.add_uri(HY2)
        self.store.set_active("AUTO:VLESS")
        self.store.remove("AUTO:VLESS")
        self.assertEqual(self.store.active_tag, "AUTO:Hysteria2")

    def test_persistence(self):
        self.store.add_uri(VLESS)
        store2 = profiles.ProfileStore(os.path.join(self.tmp, "profiles.json"))
        self.assertEqual(len(store2.profiles), 1)
        self.assertEqual(store2.active_tag, "AUTO:VLESS")

    def test_active_fallback(self):
        self.store.add_uri(VLESS)
        self.assertEqual(self.store.active()["tag"], "AUTO:VLESS")

    def test_add_uri_persists_subscription_field(self):
        self.store.add_uri(VLESS, subscription="sub-abc123")
        self.assertEqual(self.store.get("AUTO:VLESS")["subscription"],
                         "sub-abc123")

    def test_add_uri_without_subscription_is_none(self):
        self.store.add_uri(VLESS)
        self.assertIsNone(self.store.get("AUTO:VLESS").get("subscription"))

    def test_add_subscription_profiles_sets_group_and_enables(self):
        parsed, _ = parsers.parse_lines([VLESS, HY2])
        n = self.store.add_subscription_profiles(parsed, "sub-abc123")
        self.assertEqual(n, 2)
        self.assertTrue(all(p.get("subscription") == "sub-abc123"
                            for p in self.store.profiles))
        self.assertEqual(self.store.active_tag, "AUTO:VLESS")

    def test_add_subscription_profiles_skips_manual_dup_by_uri(self):
        import subscriptions
        self.store.add_uri(VLESS)  # manual wins
        parsed, _ = subscriptions.parse_links([VLESS, HY2])
        n = self.store.add_subscription_profiles(parsed, "sub-abc123")
        self.assertEqual(n, 1)  # only HY2 added; VLESS skipped
        self.assertEqual(len(self.store.profiles), 2)
        self.assertIsNone(self.store.get("AUTO:VLESS").get("subscription"))
        self.assertEqual(self.store.get("AUTO:Hysteria2")["subscription"],
                         "sub-abc123")

    def test_remove_by_subscription_removes_only_that_group(self):
        self.store.add_uri(VLESS)  # manual
        parsed, _ = parsers.parse_lines([HY2])
        self.store.add_subscription_profiles(parsed, "sub-abc123")
        self.store.remove_by_subscription("sub-abc123")
        self.assertEqual([p["tag"] for p in self.store.profiles],
                         ["AUTO:VLESS"])
        self.assertEqual(self.store.active_tag, "AUTO:VLESS")

    def test_remove_by_subscription_repicks_active(self):
        parsed, _ = parsers.parse_lines([VLESS, HY2])
        self.store.add_subscription_profiles(parsed, "sub-abc123")
        self.store.set_active("AUTO:VLESS")
        self.store.remove_by_subscription("sub-abc123")
        self.assertEqual(self.store.profiles, [])
        self.assertIsNone(self.store.active_tag)

    def test_sync_subscription_adds_removes_and_keeps_enabled(self):
        import subscriptions
        parsed, _ = subscriptions.parse_links([VLESS, HY2])
        self.store.add_subscription_profiles(parsed, "sub-abc123")
        self.store.toggle("AUTO:VLESS")  # user disables one profile
        self.store.set_active("AUTO:Hysteria2")
        # refresh body drops HY2, keeps VLESS
        new_parsed, _ = subscriptions.parse_links([VLESS])
        added, removed = self.store.sync_subscription(new_parsed, "sub-abc123")
        self.assertEqual(added, [])
        self.assertEqual(removed, ["AUTO:Hysteria2"])
        self.assertEqual([p["tag"] for p in self.store.profiles],
                         ["AUTO:VLESS"])
        self.assertFalse(self.store.get("AUTO:VLESS")["enabled"],
                         "sync must keep the user's enabled flag")
        self.assertIsNone(self.store.active_tag,
                          "no enabled profile remains, so no active profile")

    def test_sync_subscription_adds_new_links(self):
        import subscriptions
        parsed, _ = subscriptions.parse_links([VLESS])
        self.store.add_subscription_profiles(parsed, "sub-abc123")
        new_parsed, _ = subscriptions.parse_links([VLESS, HY2])
        added, removed = self.store.sync_subscription(new_parsed, "sub-abc123")
        self.assertEqual(added, ["AUTO:Hysteria2"])
        self.assertEqual(removed, [])

    def test_config_profiles_same_endpoint_dedup(self):
        parsed = [
            {"protocol": "vless", "tag": "cfg-a", "server": "h",
             "port": 443, "uuid": "u"},
            {"protocol": "vless", "tag": "cfg-b", "server": "h",
             "port": 443, "uuid": "u"},
        ]
        n = self.store.add_subscription_profiles(parsed, "sub-cfg")
        self.assertEqual(n, 1, "same endpoint must dedup config profiles")
        self.assertEqual(len(self.store.profiles), 1)

    def test_config_profile_loses_to_manual_same_endpoint(self):
        parsed, _ = parsers.parse_lines([VLESS])
        self.store.add_uri(parsed[0].get("uri") or VLESS)  # manual
        cfg = [{"protocol": "vless", "tag": "cfg", "server":
                parsers.parse_uri(VLESS)["server"],
                "port": parsers.parse_uri(VLESS)["port"], "uuid": "u"}]
        n = self.store.add_subscription_profiles(cfg, "sub-cfg")
        self.assertEqual(n, 0, "manual profile must win over config copy")
        self.assertEqual(len(self.store.profiles), 1)
        self.assertIsNone(self.store.profiles[0].get("subscription"))

    def test_sync_keeps_enabled_for_config_profiles(self):
        parsed = [
            {"protocol": "vless", "tag": "c1", "server": "h1",
             "port": 443, "uuid": "u1"},
            {"protocol": "hysteria2", "tag": "c2", "server": "h2",
             "port": 8443, "password": "p"},
        ]
        self.store.add_subscription_profiles(parsed, "sub-cfg")
        self.store.toggle("c1")
        kept = [{"protocol": "vless", "tag": "c1", "server": "h1",
                 "port": 443, "uuid": "u1"}]
        added, removed = self.store.sync_subscription(kept, "sub-cfg")
        self.assertEqual(removed, ["c2"])
        self.assertFalse(self.store.get("c1")["enabled"],
                         "sync must keep enabled flag for config profiles")


class TestBuildSingbox(unittest.TestCase):
    def _settings(self, **kw):
        s = {"local_port": 1080, "mode": "urltest", "urltest_interval": "3m",
             "urltest_tolerance": 50, "interrupt_connections": True,
             "test_url": "https://x/204", "log_level": "info"}
        s.update(kw)
        return s

    def test_urltest(self):
        profs, _ = parsers.parse_lines([VLESS, HY2, TROJAN])
        cfg, skipped = build_singbox.build_config(profs, self._settings())
        ut = [o for o in cfg["outbounds"] if o["type"] == "urltest"][0]
        self.assertEqual(len(ut["outbounds"]), 3)
        self.assertEqual(cfg["route"]["final"], "proxy")

    def test_manual(self):
        profs, _ = parsers.parse_lines([VLESS, HY2])
        cfg, _ = build_singbox.build_config(profs, self._settings(mode="manual"),
                                            active_tag="AUTO:Hysteria2")
        sel = [o for o in cfg["outbounds"] if o["type"] == "selector"][0]
        self.assertEqual(sel["default"], "AUTO:Hysteria2")

    def test_private_ip_traffic_goes_direct(self):
        profs, _ = parsers.parse_lines([VLESS, HY2])
        cfg, _ = build_singbox.build_config(profs, self._settings())
        self.assertEqual(cfg["route"]["rules"], [
            {"action": "sniff"},
            {"protocol": "dns", "action": "hijack-dns"},
            {"ip_is_private": True, "action": "route", "outbound": "direct"},
        ])
        self.assertEqual(cfg["route"]["final"], "proxy")

    def test_vmess_outbound(self):
        prof = parsers.parse_uri(
            "vmess://uuid-1@h1.example:443?security=auto#VM:1")
        cfg, _ = build_singbox.build_config([prof], self._settings())
        ob = [o for o in cfg["outbounds"] if o["type"] == "vmess"][0]
        self.assertEqual(ob["uuid"], "uuid-1")
        self.assertEqual(ob["server"], "h1.example")

    def test_shadowsocks_outbound(self):
        prof = parsers.parse_uri("ss://chacha20-ietf-poly1305:pw@h:8388#SS:1")
        cfg, _ = build_singbox.build_config([prof], self._settings())
        ob = [o for o in cfg["outbounds"] if o["type"] == "shadowsocks"][0]
        self.assertEqual(ob["method"], "chacha20-ietf-poly1305")
        self.assertEqual(ob["password"], "pw")

    def test_ss2022_outbound(self):
        prof = {"protocol": "shadowsocks", "tag": "ss2022",
                "server": "h", "port": 8388,
                "method": "2022-blake3-aes-128-gcm", "password": "p"}
        cfg, _ = build_singbox.build_config([prof], self._settings())
        ob = [o for o in cfg["outbounds"] if o["type"] == "shadowsocks"][0]
        self.assertEqual(ob["method"], "2022-blake3-aes-128-gcm")

    def test_wireguard_outbound(self):
        prof = {"protocol": "wireguard", "tag": "wg", "server": "h",
                "port": 51820, "private_key": "k", "public_key": "pk",
                "local_address": "10.0.0.2/32,10.0.0.3/32"}
        cfg, _ = build_singbox.build_config([prof], self._settings())
        ob = [o for o in cfg["outbounds"] if o["type"] == "wireguard"][0]
        self.assertEqual(ob["local_address"], ["10.0.0.2/32", "10.0.0.3/32"])
        self.assertEqual(ob["private_key"], "k")
        self.assertEqual(ob["peer_public_key"], "pk")

    def test_tuic_outbound(self):
        prof = parsers.parse_uri(
            "tuic://uuid-3@h:443?password=pw&congestion_control=bbr#TU:1")
        cfg, _ = build_singbox.build_config([prof], self._settings())
        ob = [o for o in cfg["outbounds"] if o["type"] == "tuic"][0]
        self.assertEqual(ob["uuid"], "uuid-3")
        self.assertEqual(ob["password"], "pw")
        self.assertEqual(ob["congestion_control"], "bbr")

    def test_socks_outbound(self):
        prof = parsers.parse_uri("socks://user:pass@h:1080#SOCKS:1")
        cfg, _ = build_singbox.build_config([prof], self._settings())
        ob = [o for o in cfg["outbounds"] if o["type"] == "socks"][0]
        self.assertEqual(ob["username"], "user")
        self.assertEqual(ob["password"], "pass")

    def test_http_outbound(self):
        prof = parsers.parse_uri("http://user:pass@h:8080#HTTP:1")
        cfg, _ = build_singbox.build_config([prof], self._settings())
        ob = [o for o in cfg["outbounds"] if o["type"] == "http"][0]
        self.assertEqual(ob["username"], "user")
        self.assertEqual(ob["password"], "pass")

    # ----- DNS -------------------------------------------------------

    def test_dns_udp_server_and_duckdns_rule(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_singbox.build_config(
            profs, self._settings(dns_server="8.8.8.8"))
        servers = cfg["dns"]["servers"]
        self.assertIn({"address": "8.8.8.8"}, servers)
        self.assertTrue(any(r.get("domain_suffix") == [".duckdns.org"]
                            for r in cfg["dns"]["rules"]))

    def test_dns_doh_server(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_singbox.build_config(
            profs, self._settings(dns_server="https://dns.google/dns-query"))
        self.assertIn({"address": "https://dns.google/dns-query"},
                      cfg["dns"]["servers"])

    def test_dns_dot_server(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_singbox.build_config(
            profs, self._settings(dns_server="tls://8.8.8.8"))
        self.assertIn({"address": "tls://8.8.8.8"}, cfg["dns"]["servers"])

    def test_dns_query_strategy(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_singbox.build_config(
            profs, self._settings(dns_server="8.8.8.8",
                                  dns_query_strategy="prefer_ipv4"))
        self.assertEqual(cfg["dns"]["strategy"], "prefer_ipv4")

    def test_dns_empty_keeps_current_block(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_singbox.build_config(profs, self._settings())
        self.assertTrue(any("1.1.1.1" == s.get("server")
                            for s in cfg["dns"]["servers"]))
        self.assertEqual(cfg["route"]["default_domain_resolver"], "local")

    def test_dns_set_keeps_resolver_tag_valid(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_singbox.build_config(
            profs, self._settings(dns_server="8.8.8.8"))
        self.assertEqual(cfg["route"]["default_domain_resolver"], "local")
        self.assertTrue(any(s.get("tag") == "local"
                            for s in cfg["dns"]["servers"]))

    def test_torrent_direct_rule_when_enabled(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_singbox.build_config(
            profs, self._settings(direct_torrent=True))
        bittorrent = [r for r in cfg["route"]["rules"]
                      if r.get("protocol") == "bittorrent"]
        self.assertEqual(len(bittorrent), 1)
        self.assertEqual(bittorrent[0]["outbound"], "direct")
        private_idx = next(i for i, r in enumerate(cfg["route"]["rules"])
                           if r.get("ip_is_private"))
        bt_idx = next(i for i, r in enumerate(cfg["route"]["rules"])
                      if r.get("protocol") == "bittorrent")
        self.assertLess(bt_idx, private_idx)

    def test_torrent_direct_rule_absent_by_default(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_singbox.build_config(profs, self._settings())
        self.assertFalse(any(r.get("protocol") == "bittorrent"
                             for r in cfg["route"]["rules"]))

    def test_skip_xhttp(self):
        profs, _ = parsers.parse_lines([VLESS, XHTTP])
        cfg, skipped = build_singbox.build_config(profs, self._settings())
        self.assertEqual(len(skipped), 1)
        tags = [o["tag"] for o in cfg["outbounds"]
                if o["type"] not in ("urltest", "direct")]
        self.assertNotIn("UAE:xHTTP", tags)

    def test_urltest_interrupt_connections_true(self):
        profs, _ = parsers.parse_lines([VLESS, HY2])
        cfg, _ = build_singbox.build_config(profs, self._settings(mode="urltest", interrupt_connections=True))
        ut = [o for o in cfg["outbounds"] if o["type"] == "urltest"][0]
        self.assertEqual(ut["interrupt_exist_connections"], False)

    def test_urltest_interrupt_connections_false(self):
        profs, _ = parsers.parse_lines([VLESS, HY2])
        cfg, _ = build_singbox.build_config(profs, self._settings(mode="urltest", interrupt_connections=False))
        ut = [o for o in cfg["outbounds"] if o["type"] == "urltest"][0]
        self.assertEqual(ut["interrupt_exist_connections"], False)

    def test_manual_interrupt_connections_true(self):
        profs, _ = parsers.parse_lines([VLESS, HY2])
        cfg, _ = build_singbox.build_config(profs, self._settings(mode="manual", interrupt_connections=True),
                                            active_tag="AUTO:Hysteria2")
        sel = [o for o in cfg["outbounds"] if o["type"] == "selector"][0]
        self.assertEqual(sel["interrupt_exist_connections"], True)

    def test_manual_interrupt_connections_false(self):
        profs, _ = parsers.parse_lines([VLESS, HY2])
        cfg, _ = build_singbox.build_config(profs, self._settings(mode="manual", interrupt_connections=False),
                                            active_tag="AUTO:Hysteria2")
        sel = [o for o in cfg["outbounds"] if o["type"] == "selector"][0]
        self.assertEqual(sel["interrupt_exist_connections"], False)


class TestBuildXray(unittest.TestCase):
    def _settings(self, **kw):
        s = {"local_port": 1080, "mode": "urltest", "urltest_interval": "3m",
             "test_url": "https://x/204", "log_level": "info"}
        s.update(kw)
        return s

    def test_hysteria2_supported_in_xray_26(self):
        profs, _ = parsers.parse_lines([VLESS, HY2, TROJAN])
        cfg, skipped = build_xray.build_config(profs, self._settings())
        self.assertEqual(skipped, [])
        self.assertEqual(len(cfg["routing"]["balancers"][0]["selector"]), 3)
        hy = [o for o in cfg["outbounds"] if o["protocol"] == "hysteria"][0]
        self.assertEqual(hy["settings"]["address"], "bigping.duckdns.org")
        self.assertEqual(hy["settings"]["version"], 2)
        self.assertEqual(hy["streamSettings"]["hysteriaSettings"]["version"], 2)
        self.assertEqual(hy["streamSettings"]["hysteriaSettings"]["auth"],
                         "pass123")

    def test_leastping(self):
        profs, _ = parsers.parse_lines([VLESS, TROJAN])
        cfg, _ = build_xray.build_config(profs, self._settings())
        bal = cfg["routing"]["balancers"][0]
        self.assertEqual(bal["strategy"]["type"], "leastPing")
        self.assertIn("burstObservatory", cfg)

    def test_manual(self):
        profs, _ = parsers.parse_lines([VLESS, TROJAN])
        cfg, _ = build_xray.build_config(profs, self._settings(mode="manual"),
                                         active_tag="AUTO:Trojan")
        rule = [r for r in cfg["routing"]["rules"] if r.get("outboundTag") == "AUTO:Trojan"]
        self.assertTrue(rule)

    def test_private_ip_traffic_goes_direct(self):
        profs, _ = parsers.parse_lines([VLESS, TROJAN])
        for mode in ("urltest", "manual"):
            with self.subTest(mode=mode):
                cfg, _ = build_xray.build_config(
                    profs, self._settings(mode=mode), active_tag="AUTO:Trojan")
                rules = cfg["routing"]["rules"]
                self.assertEqual(rules[0], {
                    "type": "field",
                    "ip": ["geoip:private"],
                    "outboundTag": "direct",
                })
                self.assertEqual(rules[1]["network"], "tcp,udp")
                expected = ("balancerTag", "proxy") if mode == "urltest" else (
                    "outboundTag", "AUTO:Trojan")
                self.assertEqual(rules[1][expected[0]], expected[1])

    def test_socks_inbound(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(profs, self._settings())
        socks = [i for i in cfg["inbounds"] if i["protocol"] == "socks"][0]
        self.assertEqual(socks["port"], 1080)

    def _xray_outbound(self, prof, protocol):
        cfg, skipped = build_xray.build_config([prof], self._settings())
        self.assertEqual(skipped, [])
        return [o for o in cfg["outbounds"] if o["protocol"] == protocol][0]

    def test_vmess_outbound(self):
        prof = parsers.parse_uri(
            "vmess://uuid-1@h1.example:443?security=auto#VM:1")
        ob = self._xray_outbound(prof, "vmess")
        self.assertEqual(ob["settings"]["vnext"][0]["users"][0]["id"],
                         "uuid-1")

    def test_shadowsocks_outbound(self):
        prof = parsers.parse_uri("ss://aes-256-gcm:pw@h:8388#SS:1")
        ob = self._xray_outbound(prof, "shadowsocks")
        srv = ob["settings"]["servers"][0]
        self.assertEqual(srv["method"], "aes-256-gcm")
        self.assertEqual(srv["password"], "pw")

    def test_wireguard_outbound(self):
        prof = {"protocol": "wireguard", "tag": "wg", "server": "h",
                "port": 51820, "private_key": "k", "public_key": "pk",
                "local_address": "10.0.0.2/32,10.0.0.3/32"}
        ob = self._xray_outbound(prof, "wireguard")
        self.assertEqual(ob["settings"]["secretKey"], "k")
        self.assertEqual(ob["settings"]["address"],
                         ["10.0.0.2/32", "10.0.0.3/32"])
        self.assertEqual(ob["settings"]["peers"][0]["publicKey"], "pk")
        self.assertEqual(ob["settings"]["peers"][0]["endpoint"], "h:51820")

    def test_socks_outbound(self):
        prof = parsers.parse_uri("socks://user:pass@h:1080#SOCKS:1")
        ob = self._xray_outbound(prof, "socks")
        user = ob["settings"]["servers"][0]["users"][0]
        self.assertEqual(user["user"], "user")
        self.assertEqual(user["pass"], "pass")

    def test_http_outbound(self):
        prof = parsers.parse_uri("http://user:pass@h:8080#HTTP:1")
        ob = self._xray_outbound(prof, "http")
        user = ob["settings"]["servers"][0]["users"][0]
        self.assertEqual(user["user"], "user")

    def test_skips_tuic(self):
        prof = parsers.parse_uri("tuic://uuid-3@h:443?password=pw#TU:1")
        outbounds, tags, skipped = build_xray.build_outbounds([prof])
        self.assertEqual(outbounds, [])
        self.assertEqual(tags, [])
        self.assertTrue(any("tuic" in reason for _, reason in skipped))
        with self.assertRaises(RuntimeError):
            build_xray.build_config([prof], self._settings())

    # ----- DNS -------------------------------------------------------

    def test_dns_udp_server(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(
            profs, self._settings(dns_server="8.8.8.8"))
        self.assertIn("8.8.8.8", cfg["dns"]["servers"])

    def test_dns_doh_server(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(
            profs, self._settings(dns_server="https://dns.google/dns-query"))
        self.assertIn("https://dns.google/dns-query", cfg["dns"]["servers"])

    def test_dns_dot_server(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(
            profs, self._settings(dns_server="tls://8.8.8.8"))
        self.assertIn("tcp+tls://8.8.8.8:853", cfg["dns"]["servers"])

    def test_dns_query_strategy(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(
            profs, self._settings(dns_server="8.8.8.8",
                                  dns_query_strategy="prefer_ipv4"))
        self.assertEqual(cfg["dns"]["queryStrategy"], "UseIPv4")

    def test_dns_empty_keeps_current_list(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(profs, self._settings())
        self.assertIn("1.1.1.1", cfg["dns"]["servers"])
        self.assertIn("localhost", cfg["dns"]["servers"])

    def test_torrent_direct_rule_when_enabled(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(
            profs, self._settings(direct_torrent=True))
        bt = [r for r in cfg["routing"]["rules"]
              if r.get("protocol") == ["bittorrent"]]
        self.assertEqual(len(bt), 1)
        self.assertEqual(bt[0]["outboundTag"], "direct")
        self.assertIs(cfg["routing"]["rules"][0], bt[0],
                      "bittorrent rule must be the first routing rule")

    def test_torrent_direct_rule_absent_by_default(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(profs, self._settings())
        self.assertFalse(any(r.get("protocol") == ["bittorrent"]
                             for r in cfg["routing"]["rules"]))


class TestHelpers(unittest.TestCase):
    def test_defaults(self):
        s = helpers.get_settings(reader=lambda: {})
        self.assertEqual(s["engine"], "sing-box")
        self.assertEqual(s["mode"], "urltest")
        self.assertEqual(s["local_port"], 1080)
        self.assertIs(s["autostart"], True)
        self.assertIs(s["notify"], True)

    def test_normalization(self):
        raw = {"engine": "1", "mode": "1", "autostart": "false",
               "notify": "false", "local_port": "8080", "log_level": "2"}
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["engine"], "xray")
        self.assertEqual(s["mode"], "manual")
        self.assertIs(s["autostart"], False)
        self.assertIs(s["notify"], False)
        self.assertEqual(s["local_port"], 8080)
        self.assertEqual(s["log_level"], "warn")

    def test_auto_configure_integration_defaults_true(self):
        self.assertTrue(helpers.get_settings(reader=lambda: {})["auto_configure_integration"])

    def test_auto_configure_integration_can_be_disabled(self):
        raw = {"auto_configure_integration": "false"}
        self.assertFalse(helpers.get_settings(reader=lambda: raw)["auto_configure_integration"])

    def test_subscription_settings_defaults(self):
        s = helpers.get_settings(reader=lambda: {})
        self.assertEqual(s["subscription_interval_hours"], 0)
        self.assertIs(s["disable_proto_vless"], False)
        self.assertIs(s["disable_proto_trojan"], False)
        self.assertIs(s["disable_proto_hysteria2"], False)

    def test_subscription_settings_normalized(self):
        raw = {"subscription_interval_hours": "24",
               "disable_proto_vless": "true",
               "disable_proto_trojan": "false",
               "disable_proto_hysteria2": "true"}
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["subscription_interval_hours"], 24)
        self.assertIs(s["disable_proto_vless"], True)
        self.assertIs(s["disable_proto_trojan"], False)
        self.assertIs(s["disable_proto_hysteria2"], True)

    def test_disabled_protocols_empty_by_default(self):
        self.assertEqual(helpers.disabled_protocols(
            reader=lambda: {}), ())

    def test_disabled_protocols_from_toggles(self):
        raw = {"disable_proto_vless": "true",
               "disable_proto_trojan": "false",
               "disable_proto_hysteria2": "true"}
        got = helpers.disabled_protocols(reader=lambda: raw)
        self.assertEqual(sorted(got), ["hysteria2", "vless"])

    def test_disabled_protocols_merges_legacy_skip_list(self):
        raw = {"disable_proto_vless": "false",
               "disable_proto_trojan": "false",
               "disable_proto_hysteria2": "false",
               "skip_protocols": "trojan,xhttp"}
        got = helpers.disabled_protocols(reader=lambda: raw)
        self.assertEqual(sorted(got), ["trojan", "xhttp"])

    def test_parse_dns_server_udp(self):
        self.assertEqual(helpers.parse_dns_server("8.8.8.8"),
                         {"kind": "udp", "host": "8.8.8.8", "port": 53})

    def test_parse_dns_server_doh(self):
        self.assertEqual(
            helpers.parse_dns_server("https://dns.google/dns-query"),
            {"kind": "doh", "host": "dns.google", "port": 443})

    def test_parse_dns_server_dot(self):
        self.assertEqual(helpers.parse_dns_server("tls://8.8.8.8"),
                         {"kind": "dot", "host": "8.8.8.8", "port": 853})

    def test_parse_dns_server_empty(self):
        self.assertIsNone(helpers.parse_dns_server(""))
        self.assertIsNone(helpers.parse_dns_server(None))

    def test_parse_dns_server_garbage(self):
        self.assertIsNone(helpers.parse_dns_server("not a dns server"))

    def test_dns_settings_defaults(self):
        s = helpers.get_settings(reader=lambda: {})
        self.assertEqual(s["dns_server"], "")
        self.assertEqual(s["dns_query_strategy"], "")
        self.assertIs(s["direct_torrent"], False)

    def test_dns_settings_normalized(self):
        raw = {"dns_server": "tls://1.1.1.1",
               "dns_query_strategy": "prefer_ipv4",
               "direct_torrent": "true"}
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["dns_server"], "tls://1.1.1.1")
        self.assertEqual(s["dns_query_strategy"], "prefer_ipv4")
        self.assertIs(s["direct_torrent"], True)

    def test_dns_strategy_integer_mapping(self):
        for raw_value, expected in (("0", ""), ("1", "prefer_ipv4"),
                                    ("2", "ipv4_only"), ("3", "prefer_ipv6"),
                                    ("4", "ipv6_only")):
            s = helpers.get_settings(reader=lambda: {
                "dns_query_strategy": raw_value})
            self.assertEqual(s["dns_query_strategy"], expected,
                             "strategy %s must map to %r" % (raw_value,
                                                              expected))

    def test_pick_reachable_returns_preferred_when_reachable(self):
        profs = [
            {"tag": "A", "server": "h1", "port": 1, "enabled": True},
            {"tag": "B", "server": "h2", "port": 2, "enabled": True},
        ]
        tag, err = helpers.pick_reachable(profs, "A", prober=lambda *a: 10)
        self.assertEqual(tag, "A")
        self.assertIsNone(err)

    def test_pick_reachable_skips_unreachable_preferred(self):
        profs = [
            {"tag": "A", "server": "h1", "port": 1, "enabled": True},
            {"tag": "B", "server": "h2", "port": 2, "enabled": True},
        ]
        def prober(host, port, timeout):
            return None if port == 1 else 10
        tag, err = helpers.pick_reachable(profs, "A", prober=prober)
        self.assertEqual(tag, "B")
        self.assertIsNone(err)

    def test_pick_reachable_returns_error_when_none_reachable(self):
        profs = [
            {"tag": "A", "server": "h1", "port": 1, "enabled": True},
            {"tag": "B", "server": "h2", "port": 2, "enabled": True},
        ]
        tag, err = helpers.pick_reachable(profs, "A", prober=lambda *a: None)
        self.assertIsNone(tag)
        self.assertIsNotNone(err)

    def test_pick_reachable_skips_disabled_profiles(self):
        profs = [
            {"tag": "A", "server": "h1", "port": 1, "enabled": False},
            {"tag": "B", "server": "h2", "port": 2, "enabled": True},
        ]
        tag, err = helpers.pick_reachable(profs, "A", prober=lambda *a: 10)
        self.assertEqual(tag, "B")


class TestBinaryManager(unittest.TestCase):
    def test_paths(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bm = binary_manager.BinaryManager(addon, work, platform_override="linux_x64")
            self.assertTrue(bm.bundled_binary.endswith(
                os.path.join("resources", "bin", "linux_x64", "sing-box")))
            self.assertTrue(bm.work_binary.endswith(
                os.path.join("bin", "sing-box", "linux_x64", "sing-box")))
            self.assertEqual(bm.platform, "linux_x64")

    def test_custom_path_valid(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            fake = os.path.join(addon, "sing-box")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(fake, 0o755)
            bm = binary_manager.BinaryManager(addon, work, custom_path=fake)
            self.assertEqual(bm.ensure_binary(), fake)

    def test_custom_path_invalid_falls_back(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bm = binary_manager.BinaryManager(addon, work, custom_path="/nonexistent/x")
            self.assertIsNone(bm._resolve_custom())

    def test_xray_geo_files_copied_to_work_dir(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bin_dir = os.path.join(addon, "resources", "bin", "linux_x64")
            os.makedirs(bin_dir)
            with open(os.path.join(bin_dir, "xray"), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(os.path.join(bin_dir, "xray"), 0o755)
            for name in ("geoip.dat", "geosite.dat"):
                with open(os.path.join(bin_dir, name), "w") as f:
                    f.write(name)
            bm = binary_manager.BinaryManager(
                addon, work, engine="xray", platform_override="linux_x64")
            path = bm.ensure_binary()
            self.assertEqual(path, bm.work_binary)
            for name in ("geoip.dat", "geosite.dat"):
                self.assertTrue(
                    os.path.exists(os.path.join(bm.work_dir_bin, name)),
                    "%s must be copied next to the engine" % name)

    def test_singbox_does_not_require_geo_files(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bin_dir = os.path.join(addon, "resources", "bin", "linux_x64")
            os.makedirs(bin_dir)
            with open(os.path.join(bin_dir, "sing-box"), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(os.path.join(bin_dir, "sing-box"), 0o755)
            bm = binary_manager.BinaryManager(
                addon, work, platform_override="linux_x64")
            bm.ensure_binary()
            self.assertFalse(
                os.path.exists(os.path.join(bm.work_dir_bin, "geoip.dat")),
                "sing-box needs no geo files")

    def test_stop_sigterm_bounded_wait(self):
        """Test SIGTERM + bounded wait: process.terminate() is called, handle is retained until exit is confirmed, and self.proc is set to None only after exit."""
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bm = binary_manager.BinaryManager(addon, work)
            fake_proc = _FakeProcessForStop()
            # 3 simulated steps: the poll() in is_running() consumes one, each
            # wait() one more, so both term and kill waits time out while the
            # handle must stay retained (same budget as the refusal case).
            fake_proc._exit_delay = 3
            bm.proc = fake_proc

            bm.stop()
            self.assertEqual(fake_proc._calls, ["terminate", "kill"],
                             "SIGTERM must be sent before SIGKILL")
            self.assertIsNotNone(bm.proc, "Process handle should be retained until exit is confirmed")

            # Both term and kill waits timed out above (SIGKILL escalation), so
            # the first stop() leaves the handle retained; only after the
            # process is seen to exit does a later stop() clear it.
            fake_proc._exit_delay = 0
            fake_proc.poll()  # Force exit confirmation

            # Call stop() again to confirm handle is cleared
            bm.stop()
            self.assertIsNone(bm.proc, "Process handle should be cleared after exit is confirmed")

    def test_stop_sigkill_escalation(self):
        """Test SIGKILL escalation: wait(term_timeout) times out, SIGKILL is sent, and wait(kill_timeout) is called after SIGKILL."""
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bm = binary_manager.BinaryManager(addon, work)
            fake_proc = _FakeProcessForStop()
            fake_proc._exit_delay = 2  # Ensure term_timeout and kill_timeout expire
            bm.proc = fake_proc
            
            # Call stop() and assert terminate() is called
            bm.stop(term_timeout=0.1, kill_timeout=0.1)
            self.assertIn("terminate", fake_proc._calls)
            self.assertIn("kill", fake_proc._calls)
            self.assertIsNone(bm.proc, "Process handle should be cleared after SIGKILL")

    def test_stop_handles_process_exiting_before_terminate(self):
        """A process that exits between is_running() and terminate() is a
        successful stop: terminate() raises ProcessLookupError, the handle is
        cleared, and stop() returns True instead of retaining a dead handle."""
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            log_recorder = _LogRecorder()
            bm = binary_manager.BinaryManager(addon, work, logger=log_recorder)
            fake_proc = _FakeProcessForStop()
            fake_proc._exit_before_terminate = True
            bm.proc = fake_proc

            result = bm.stop(term_timeout=0.1, kill_timeout=0.1)
            self.assertTrue(result, "a process that already exited is a successful stop")
            self.assertIsNone(bm.proc, "handle must be cleared once exit is confirmed")

    def test_stop_refusal_case(self):
        """Test refusal case: both term and kill waits time out, stop() returns False, process handle is retained, and a log is emitted."""
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            log_recorder = _LogRecorder()
            bm = binary_manager.BinaryManager(addon, work, logger=log_recorder)
            fake_proc = _FakeProcessForStop()
            fake_proc._exit_delay = 3  # Ensure term_timeout and kill_timeout expire
            bm.proc = fake_proc
            
            # Call stop() and assert it returns False
            result = bm.stop(term_timeout=0.1, kill_timeout=0.1)
            self.assertFalse(result, "stop() should return False if both waits time out")
            self.assertIsNotNone(bm.proc, "Process handle should be retained if both waits time out")
            self.assertTrue(any(lvl == "warn" and "did not exit after SIGKILL" in m
                                and "handle retained" in m
                                for lvl, m in log_recorder.entries),
                            log_recorder.entries)

    def test_stop_waits_for_listener_release(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bm = binary_manager.BinaryManager(addon, work)
            fake_proc = _FakeProcessForStop()
            bm.proc = fake_proc
            clock = _FakeBinaryClock()
            with patch.object(port_utils, "port_in_use",
                              side_effect=[True, True, False]), \
                    patch.object(binary_manager, "time", clock):
                ok = bm.stop(port=1080, release_timeout=5.0)
            self.assertTrue(ok)
            self.assertIsNone(bm.proc)
            self.assertEqual(clock.sleeps, [0.1, 0.1],
                             "release polling must step in 100 ms until the listener is free")

    def test_stop_logs_busy_listener_but_still_returns_true(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            log_recorder = _LogRecorder()
            bm = binary_manager.BinaryManager(addon, work, logger=log_recorder)
            fake_proc = _FakeProcessForStop()
            bm.proc = fake_proc
            clock = _FakeBinaryClock()
            with patch.object(port_utils, "port_in_use",
                              side_effect=lambda *a, **k: True), \
                    patch.object(binary_manager, "time", clock):
                ok = bm.stop(port=1080, release_timeout=0.3)
            self.assertTrue(ok, "process death was confirmed, so stop() stays True")
            self.assertIsNone(bm.proc)
            self.assertEqual(clock.sleeps, [0.1, 0.1, 0.1],
                             "polling must continue until release_timeout elapses")
            self.assertTrue(any(lvl == "warn" and "1080" in m
                                for lvl, m in log_recorder.entries),
                            log_recorder.entries)

    def test_start_waits_for_listener_before_returning(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bm = binary_manager.BinaryManager(
                addon, work, custom_path=_write_executable(os.path.join(addon, "sing-box")))
            fake_proc = _FakeProcessForStop()
            clock = _FakeBinaryClock()
            with patch.object(binary_manager.subprocess, "Popen",
                              side_effect=_popen_returning(fake_proc)), \
                    patch.object(port_utils, "port_in_use",
                                 side_effect=[False, False, True]), \
                    patch.object(binary_manager, "time", clock):
                proc = bm.start(os.path.join(work, "engine.json"), port=1080,
                                ready_timeout=1.0)
            self.assertIs(proc, fake_proc)
            self.assertEqual(clock.sleeps, [0.1, 0.1],
                             "readiness must poll in 100 ms steps until the listener is up")
            self.assertIsNone(fake_proc.poll(),
                              "the process must stay alive after start returns")

    def test_start_readiness_timeout_stops_spawned_process_and_raises(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            log_recorder = _LogRecorder()
            bm = binary_manager.BinaryManager(
                addon, work, custom_path=_write_executable(os.path.join(addon, "sing-box")),
                logger=log_recorder)
            fake_proc = _FakeProcessForStop()
            clock = _FakeBinaryClock()
            with patch.object(binary_manager.subprocess, "Popen",
                              side_effect=_popen_returning(fake_proc)), \
                    patch.object(port_utils, "port_in_use",
                                 side_effect=lambda *a, **k: False), \
                    patch.object(binary_manager, "time", clock):
                with self.assertRaises(RuntimeError):
                    bm.start(os.path.join(work, "engine.json"), port=1080,
                             ready_timeout=0.3)
            self.assertIsNone(bm.proc,
                              "no live process handle may survive a failed start")
            self.assertIn("terminate", fake_proc._calls,
                          "the spawned process must be stopped through the hardened path")
            self.assertTrue(any("stopping" in m.lower() for lvl, m in log_recorder.entries),
                            log_recorder.entries)

    def test_start_fails_when_process_exits_during_readiness(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bm = binary_manager.BinaryManager(
                addon, work, custom_path=_write_executable(os.path.join(addon, "sing-box")))
            fake_proc = _FakeProcessForStop()
            fake_proc._exit_code = 1
            clock = _FakeBinaryClock()
            with patch.object(binary_manager.subprocess, "Popen",
                              side_effect=_popen_returning(fake_proc)), \
                    patch.object(port_utils, "port_in_use",
                                 side_effect=lambda *a, **k: False), \
                    patch.object(binary_manager, "time", clock):
                with self.assertRaises(RuntimeError):
                    bm.start(os.path.join(work, "engine.json"), port=1080,
                             ready_timeout=0.3)
            self.assertIsNone(bm.proc,
                              "a dead process must not survive as a ready handle")
            self.assertNotIn("terminate", fake_proc._calls,
                             "no signal is needed once the process already exited")

    def test_restart_forwards_port_to_stop_and_start(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bm = binary_manager.BinaryManager(addon, work)
            with patch.object(bm, "stop") as stop_mock, \
                    patch.object(bm, "start") as start_mock:
                result = bm.restart("/cfg.json", port=1080, ready_timeout=7.5)
            stop_mock.assert_called_once_with(port=1080)
            start_mock.assert_called_once_with("/cfg.json", port=1080,
                                               ready_timeout=7.5)
            self.assertIs(result, start_mock.return_value)


class TestPluginArgs(unittest.TestCase):
    def test_basic_with_query(self):
        handle, params = helpers.parse_plugin_args(
            ["default.py", "0", "?action=add"])
        self.assertEqual(handle, 0)
        self.assertEqual(params, {"action": "add"})

    def test_no_query(self):
        handle, params = helpers.parse_plugin_args(["default.py", "42"])
        self.assertEqual(handle, 42)
        self.assertEqual(params, {})

    def test_empty_argv(self):
        handle, params = helpers.parse_plugin_args(["default.py"])
        self.assertEqual(handle, -1)
        self.assertEqual(params, {})

    def test_tag_url_encoded_colon_decoded(self):
        handle, params = helpers.parse_plugin_args(
            ["default.py", "1", "?action=activate&tag=AUTO%3AVLESS"])
        self.assertEqual(handle, 1)
        self.assertEqual(params["action"], "activate")
        self.assertEqual(params["tag"], "AUTO:VLESS")

    def test_query_without_question_prefix(self):
        handle, params = helpers.parse_plugin_args(
            ["default.py", "5", "action=test"])
        self.assertEqual(handle, 5)
        self.assertEqual(params, {"action": "test"})


class TestMeasureLatencies(unittest.TestCase):
    def test_measures_enabled_profiles_only(self):
        profs = [
            {"tag": "A", "server": "h1", "port": 1, "enabled": True},
            {"tag": "B", "server": "h2", "port": 2, "enabled": False},
            {"tag": "C", "server": "h3", "port": 3, "enabled": True},
        ]

        def fake_prober(host, port, timeout):
            return 10

        result = helpers.measure_latencies(profs, prober=fake_prober)
        self.assertEqual(result["A"], 10)
        self.assertEqual(result["C"], 10)
        self.assertEqual(result["B"], None)

    def test_timeout_and_failure_map_to_none(self):
        profs = [
            {"tag": "ok", "server": "h", "port": 1, "enabled": True},
            {"tag": "fail", "server": "h", "port": 2, "enabled": True},
        ]

        def fake_prober(host, port, timeout):
            if port == 1:
                return 5
            raise socket.error("boom")

        result = helpers.measure_latencies(profs, prober=fake_prober)
        self.assertEqual(result["ok"], 5)
        self.assertIsNone(result["fail"])

    def test_concurrent_not_serial(self):
        profs = [
            {"tag": "A", "server": "h", "port": 1, "enabled": True},
            {"tag": "B", "server": "h", "port": 2, "enabled": True},
            {"tag": "C", "server": "h", "port": 3, "enabled": True},
        ]

        def fake_prober(host, port, timeout):
            time.sleep(0.2)
            return 1

        t0 = time.time()
        result = helpers.measure_latencies(profs, prober=fake_prober, timeout=0.2)
        elapsed = time.time() - t0
        self.assertEqual(len(result), 3)
        self.assertLess(elapsed, 0.5, "concurrent probing ran serially")

    def test_all_profiles_included_even_when_empty(self):
        result = helpers.measure_latencies([], prober=lambda h, p, t: 1)
        self.assertEqual(result, {})


class TestDirectoryEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = profiles.ProfileStore(os.path.join(self.tmp, "profiles.json"))
        self.base = "plugin://service.advancedproxy/"

    def _entries(self, mode="urltest", latencies=None):
        return helpers.build_directory_entries(self.store, mode, self.base, latencies=latencies)

    def _actions(self):
        return [a["action"] for a in self._entries() if a["kind"] == "action"]

    def test_empty_listing_has_add_and_settings_not_clear(self):
        a = self._actions()
        self.assertIn("add", a)
        self.assertIn("settings", a)
        self.assertNotIn("clear", a)
        info = [e for e in self._entries() if e["kind"] == "info"]
        self.assertEqual(len(info), 1)

    def test_profile_entries_preserve_order_and_active_first(self):
        self.store.add_uri(VLESS)
        self.store.add_uri(HY2)
        profs = [e for e in self._entries() if e["kind"] == "profile"]
        self.assertEqual(len(profs), 2)
        self.assertEqual(profs[0]["tag"], "AUTO:VLESS")
        self.assertTrue(profs[0]["is_active"])
        self.assertTrue(profs[0]["enabled"])
        self.assertEqual(profs[1]["tag"], "AUTO:Hysteria2")
        self.assertFalse(profs[1]["is_active"])
        self.assertTrue(profs[1]["enabled"])

    def test_action_url_tag_is_url_encoded(self):
        self.store.add_uri(VLESS)
        prof = [e for e in self._entries() if e["kind"] == "profile"][0]
        self.assertIn("tag=AUTO%3AVLESS", prof["click_url"])
        self.assertIn("action=activate", prof["click_url"])
        self.assertIn("tag=AUTO%3AVLESS", prof["toggle_url"])
        self.assertIn("action=toggle", prof["toggle_url"])
        self.assertIn("tag=AUTO%3AVLESS", prof["remove_url"])
        self.assertIn("action=remove", prof["remove_url"])

    def test_profile_entry_carries_copy_url(self):
        self.store.add_uri(VLESS)
        prof = [e for e in self._entries() if e["kind"] == "profile"][0]
        self.assertIn("action=copy", prof["copy_url"])
        self.assertIn("tag=AUTO%3AVLESS", prof["copy_url"])

    def test_group_rows_emitted_when_subscriptions_present(self):
        groups = [{"id": "sub-abc", "url": "https://example.com/sub",
                   "last_updated": 0, "last_error": None}]
        entries = helpers.build_directory_entries(
            self.store, "urltest", self.base, subscriptions=groups)
        rows = [e for e in entries if e["kind"] == "subscription"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://example.com/sub")
        self.assertIn("action=sub_refresh", rows[0]["refresh_url"])
        self.assertIn("id=sub-abc", rows[0]["refresh_url"])
        self.assertIn("action=sub_remove", rows[0]["remove_url"])

    def test_no_group_rows_without_subscriptions(self):
        entries = helpers.build_directory_entries(
            self.store, "urltest", self.base)
        rows = [e for e in entries if e["kind"] == "subscription"]
        self.assertEqual(rows, [])

    def test_disabled_profile_not_active(self):
        self.store.add_uri(VLESS)
        self.store.add_uri(HY2)
        self.store.toggle("AUTO:VLESS")
        prof = [e for e in self._entries()
                if e["kind"] == "profile" and e["tag"] == "AUTO:VLESS"][0]
        self.assertFalse(prof["enabled"])
        self.assertFalse(prof["is_active"])

    def test_clear_present_when_profiles_exist(self):
        self.store.add_uri(VLESS)
        self.assertIn("clear", self._actions())

    def test_actions_order_profiles_first(self):
        self.store.add_uri(VLESS)
        entries = self._entries()
        kinds = [e["kind"] for e in entries]
        self.assertEqual(kinds.index("mode_toggle"), 0)
        self.assertGreater(kinds.index("profile"), kinds.index("mode_toggle"))
        self.assertGreater(kinds.index("action"), kinds.index("profile"))

    def test_protocol_in_profile_entry(self):
        self.store.add_uri(VLESS)
        prof = [e for e in self._entries() if e["kind"] == "profile"][0]
        self.assertEqual(prof["protocol"], "vless")

    def test_no_info_entry_when_profiles_present(self):
        self.store.add_uri(VLESS)
        info = [e for e in self._entries() if e["kind"] == "info"]
        self.assertEqual(len(info), 0)

    def test_mode_toggle_entry_at_top(self):
        self.store.add_uri(VLESS)
        entries = self._entries(mode="urltest")
        self.assertEqual(entries[0]["kind"], "mode_toggle")
        self.assertEqual(entries[0]["mode"], "urltest")
        self.assertIn("action=toggle_mode", entries[0]["url"])

    def test_profile_entry_has_latency_ms_when_passed(self):
        self.store.add_uri(VLESS)
        latencies = {"AUTO:VLESS": 142}
        prof = [e for e in self._entries(latencies=latencies) if e["kind"] == "profile"][0]
        self.assertEqual(prof["latency_ms"], 142)

    def test_disabled_profile_latency_ms_is_none(self):
        self.store.add_uri(VLESS)
        self.store.toggle("AUTO:VLESS")
        prof = [e for e in self._entries(latencies={})
                if e["kind"] == "profile"][0]
        self.assertIsNone(prof["latency_ms"])


class TestOsarch(unittest.TestCase):
    def test_supported(self):
        self.assertTrue(osarch.is_supported(osarch.get_platform()))

    def test_override(self):
        self.assertEqual(osarch.get_platform("linux_armv7"), "linux_armv7")

    def test_override_traversal_falls_back(self):
        """Malicious platform override must not be returned verbatim."""
        self.assertIn(osarch.get_platform("../"), osarch.SUPPORTED)
        self.assertIn(osarch.get_platform("..\\"), osarch.SUPPORTED)
        self.assertIn(osarch.get_platform("foo/bar"), osarch.SUPPORTED)
        self.assertIn(osarch.get_platform("../../etc/passwd"), osarch.SUPPORTED)
        self.assertIn(osarch.get_platform("linux_x64/../"), osarch.SUPPORTED)

    def test_valid_override_still_works(self):
        for p in ("linux_x64", "linux_armv7", "windows_x64", "android_arm"):
            self.assertEqual(osarch.get_platform(p), p)


class TestLogLevelMapping(unittest.TestCase):
    def test_settings_order_matches_helpers(self):
        raw = {"log_level": "0"}
        self.assertEqual(helpers.get_settings(reader=lambda: raw)["log_level"], "debug")
        raw["log_level"] = "1"
        self.assertEqual(helpers.get_settings(reader=lambda: raw)["log_level"], "info")
        raw["log_level"] = "2"
        self.assertEqual(helpers.get_settings(reader=lambda: raw)["log_level"], "warn")
        raw["log_level"] = "3"
        self.assertEqual(helpers.get_settings(reader=lambda: raw)["log_level"], "error")

    def test_default_log_level(self):
        self.assertEqual(helpers.get_settings(reader=lambda: {})["log_level"], "info")


class TestNoEnabledProfilesString(unittest.TestCase):
    def test_action_test_uses_dedicated_no_enabled_string(self):
        with open(os.path.join(HERE, "..", "service.advancedproxy", "default.py")) as f:
            src = f.read()
        start = src.index("def _action_test(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn("32218", body)
        self.assertNotIn("32214", body)


class TestProfileStoreActivation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = profiles.ProfileStore(os.path.join(self.tmp, "profiles.json"))

    def test_set_active_disabled_profile_fails(self):
        self.store.add_uri(VLESS)
        self.store.add_uri(HY2)
        self.store.toggle("AUTO:VLESS")
        ok = self.store.set_active("AUTO:VLESS")
        self.assertFalse(ok)
        self.assertNotEqual(self.store.active_tag, "AUTO:VLESS")

    def test_set_active_enabled_profile_succeeds(self):
        self.store.add_uri(VLESS)
        self.store.add_uri(HY2)
        ok = self.store.set_active("AUTO:Hysteria2")
        self.assertTrue(ok)
        self.assertEqual(self.store.active_tag, "AUTO:Hysteria2")

    def test_add_first_sets_active(self):
        self.store.add_uri(VLESS)
        self.assertEqual(self.store.active_tag, "AUTO:VLESS")


class TestMeasureLatencies(unittest.TestCase):
    def test_real_prober_module_imports(self):
        self.assertTrue(hasattr(helpers, "time"))
        self.assertTrue(callable(helpers._real_prober))


class _FakeBin(object):
    """BinaryManager stand-in that records the forwarded effective port."""

    def __init__(self, name, calls, engine="sing-box", platform="linux_x64"):
        self.name = name
        self.engine = engine
        self.platform = platform
        self._calls = calls
        self._running = True

    def is_running(self):
        return self._running

    def stop(self, port=None):
        self._calls.append(("stop", self.name))
        self._running = False

    def start(self, config_path, port=None):
        self._calls.append(("start", self.name, port))
        self._running = True

    def restart(self, config_path, port=None):
        self._calls.append(("restart", self.name, port))
        self._running = True

    def check(self, config_path):
        return True, ""


class TestSupervisorReconfigureEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        settings = {"engine": "sing-box", "mode": "manual", "local_port": 1080,
                    "autostart": True}
        self.sup = supervisor.ProxySupervisor(
            settings=settings, addon_dir=self.tmp, work_dir=self.tmp)
        self.sup.store.add_uri(VLESS)
        self.calls = []
        self.sup.bin = _FakeBin("old", self.calls)
        self.sup._make_binary_manager = lambda: _FakeBin("new", self.calls)
        # Config building is covered elsewhere; these pin ordering + port.
        self.sup.build_and_write_config = lambda: True

    def test_stops_old_binary_before_swapping_in_new_one(self):
        self.sup.reconfigure_engine()
        self.assertEqual(self.calls[0], ("stop", "old"))
        self.assertEqual(self.sup.bin.name, "new")

    def test_starts_new_binary_after_old_one_was_running(self):
        self.sup.reconfigure_engine()
        self.assertIn(("start", "new", self.sup.effective_port), self.calls)
        stop_index = self.calls.index(("stop", "old"))
        start_index = self.calls.index(
            ("start", "new", self.sup.effective_port))
        self.assertLess(stop_index, start_index)

    def test_does_not_start_if_was_stopped_and_autostart_off(self):
        self.sup.settings["autostart"] = False
        self.sup.bin = _FakeBin("old", self.calls)
        self.sup.bin.stop()
        self.calls[:] = []
        self.sup.reconfigure_engine()
        self.assertNotIn(("start", "new", self.sup.effective_port), self.calls)

    def test_reconfigure_resolves_port_exactly_once_and_forwards_it(self):
        real_resolve = self.sup._resolve_effective_port
        resolve_calls = []

        def counting():
            resolve_calls.append(1)
            return real_resolve()

        self.sup._resolve_effective_port = counting
        self.sup.reconfigure_engine()
        self.assertEqual(resolve_calls, [1])
        self.assertIn(("start", "new", self.sup.effective_port), self.calls)


class TestPortUtils(unittest.TestCase):
    def _free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_free_port_detected(self):
        port = self._free_port()
        self.assertFalse(port_utils.port_in_use(port))
        self.assertEqual(port_utils.find_free_port(port), port)

    def test_busy_port_detected(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            self.assertTrue(port_utils.port_in_use(port))
        finally:
            s.close()

    def test_fallback_skips_busy_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]
        s.listen(1)
        try:
            picked = port_utils.find_free_port(busy)
            self.assertGreater(picked, busy)
            self.assertLess(picked, busy + 100)
            self.assertFalse(port_utils.port_in_use(picked))
        finally:
            s.close()

    def test_invalid_port_is_busy(self):
        self.assertTrue(port_utils.port_in_use(0))
        self.assertTrue(port_utils.port_in_use(70000))


class TestSupervisorPortFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        settings = {
            "engine": "sing-box", "mode": "urltest", "local_port": 1080,
            "autostart": True, "urltest_interval": "3m", "urltest_tolerance": 50,
            "test_url": "https://www.gstatic.com/generate_204",
            "interrupt_connections": True, "skip_protocols": "trojan,xhttp",
            "log_level": "info", "binary_platform_override": "auto",
            "binary_custom_path": "",
        }
        self.sup = supervisor.ProxySupervisor(
            settings=settings, addon_dir=self.tmp, work_dir=self.tmp)
        self.sup.store.add_uri(VLESS)
        self.sup.store.add_uri(HY2)
        self.sup.bin = _FakeBin("sing-box", [])
        self.sup.bin.check = lambda cfg: (True, "")

    def test_uses_next_free_port_when_configured_port_busy(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 1080))
        blocker.listen(1)
        try:
            self.assertTrue(self.sup.start())
            self.assertEqual(self.sup.effective_port, 1081)
            with open(self.sup.config_path) as f:
                cfg = json.load(f)
            self.assertEqual(cfg["inbounds"][0]["listen_port"], 1081)
        finally:
            blocker.close()

    def test_keeps_configured_port_when_free(self):
        self.assertTrue(self.sup.start())
        self.assertEqual(self.sup.effective_port, 1080)
        with open(self.sup.config_path) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["inbounds"][0]["listen_port"], 1080)

    def test_state_json_reports_effective_port(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 1080))
        blocker.listen(1)
        try:
            self.sup.start()
            with open(self.sup.state_path) as f:
                st = json.load(f)
            self.assertEqual(st["port"], 1081)
            self.assertTrue(st["running"])
        finally:
            blocker.close()

    def test_start_resolves_port_exactly_once_and_forwards_it(self):
        real_resolve = self.sup._resolve_effective_port
        resolve_calls = []

        def counting():
            resolve_calls.append(1)
            return real_resolve()

        self.sup._resolve_effective_port = counting
        self.assertTrue(self.sup.start())
        self.assertEqual(resolve_calls, [1])
        self.assertIn(("start", "sing-box", self.sup.effective_port),
                      self.sup.bin._calls)
        with open(self.sup.config_path) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["inbounds"][0]["listen_port"],
                         self.sup.effective_port)

    def test_restart_forwards_the_effective_port(self):
        self.assertTrue(self.sup.start())
        self.sup.bin._calls[:] = []
        self.sup.restart()
        self.assertIn(("restart", "sing-box", self.sup.effective_port),
                      self.sup.bin._calls)

    def test_tick_never_resolves_port_or_rewrites_config(self):
        self.assertTrue(self.sup.start())
        with open(self.sup.config_path) as f:
            before = f.read()
        real_resolve = self.sup._resolve_effective_port
        resolve_calls = []

        def counting():
            resolve_calls.append(1)
            return real_resolve()

        self.sup._resolve_effective_port = counting
        self.sup.bin._calls[:] = []
        self.sup.tick()
        self.assertEqual(resolve_calls, [])
        self.assertEqual(self.sup.bin._calls, [])
        with open(self.sup.config_path) as f:
            after = f.read()
        self.assertEqual(after, before)


class TestDefaultPyUsesHelpersLatency(unittest.TestCase):
    def test_no_duplicate_latency_prober(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        self.assertNotIn("def _tcp_latency", src)
        body = re.search(r"def _action_test\(handle\):.*?(?=\ndef |\Z)", src, re.DOTALL)
        self.assertIsNotNone(body)
        self.assertIn("helpers._real_prober", body.group(0))


class TestPluginActionRefresh(unittest.TestCase):
    def test_runplugin_action_refreshes_container_instead_of_rendering_handle_minus_one(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()

        self.assertIn("def _finish_action(handle):", src)
        start = src.index("def _finish_action(handle):")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn("if handle < 0:", body)
        self.assertIn('xbmc.executebuiltin("Container.Refresh")', body)


class TestSubscriptionUiContract(unittest.TestCase):
    """Subscription actions and settings.xml contracts."""

    def test_default_dispatches_subscription_actions(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        for action in ("sub_add", "sub_refresh", "sub_remove", "copy"):
            self.assertIn('action == "%s"' % action, src,
                          "default.py must dispatch %s" % action)

    def test_default_has_subscription_action_handlers(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        for handler in ("_action_sub_add", "_action_sub_refresh",
                        "_action_sub_remove", "_action_copy"):
            self.assertIn("def %s(" % handler, src)

    def test_default_activation_uses_availability_probe(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        self.assertIn("helpers.pick_reachable", src,
                      "activation must use the reachability probe")

    def test_default_sub_refresh_applies_protocol_filter(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        start = src.index("def _action_sub_refresh(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn("helpers.disabled_protocols()", body,
                      "manual refresh must skip disabled protocols")
        self.assertIn("disabled_protocols=", body)

    def test_settings_xml_has_subscriptions_category(self):
        path = os.path.join(HERE, "..", "service.advancedproxy",
                            "resources", "settings.xml")
        with open(path) as f:
            xml = f.read()
        self.assertIn('category id="subscriptions"', xml)
        for setting in ("subscription_url", "subscription_interval_hours",
                        "disable_proto_vless", "disable_proto_trojan",
                        "disable_proto_hysteria2"):
            self.assertIn('id="%s"' % setting, xml)

    def test_settings_xml_drops_legacy_skip_protocols_field(self):
        path = os.path.join(HERE, "..", "service.advancedproxy",
                            "resources", "settings.xml")
        with open(path) as f:
            xml = f.read()
        self.assertNotIn('id="skip_protocols"', xml)

    def test_settings_xml_has_open_subscriptions_action(self):
        path = os.path.join(HERE, "..", "service.advancedproxy",
                            "resources", "settings.xml")
        with open(path) as f:
            xml = f.read()
        self.assertIn('id="open_subscriptions"', xml)
        self.assertIn('id="subscription_add"', xml)


class _FakeAddon(object):
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def getSettingInt(self, key):
        self.calls.append(("getSettingInt", key))
        return self.values.get(key, 0)

    def getSetting(self, key):
        self.calls.append(("getSetting", key))
        return str(self.values.get(key, ""))

    def setSettingInt(self, key, value):
        self.calls.append(("setSettingInt", key, value))
        return True

    def setSettingBool(self, key, value):
        self.calls.append(("setSettingBool", key, value))
        return True

    def setSetting(self, key, value):
        self.calls.append(("setSetting", key, value))
        return True


class _FakeXbmcAddon(object):
    def __init__(self, addon=None, missing=()):
        self._addon = addon or _FakeAddon()
        self._missing = set(missing)

    def Addon(self, addon_id=None):
        if addon_id in self._missing:
            raise RuntimeError("Addon '%s' not installed" % addon_id)
        return self._addon


class _FakeXbmc(object):
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def executeJSONRPC(self, request):
        self.calls.append(request)
        if not self._responses:
            return ""
        resp = self._responses.pop(0)
        return resp(request) if callable(resp) else resp


@contextlib.contextmanager
def _kodi_module(module_name, fake):
    saved = sys.modules.get(module_name)
    sys.modules[module_name] = fake
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = saved


class TestKodiAdapters(unittest.TestCase):
    ADAPTERS = (
        "read_kodi_proxy_setting", "write_kodi_proxy_setting", "addon_available",
        "read_addon_setting", "write_addon_setting", "integration_backup_path",
    )


    def test_all_adapter_names_exported_and_callable(self):
        for name in self.ADAPTERS:
            self.assertTrue(callable(getattr(helpers, name, None)), name)

    def test_helpers_module_has_no_module_level_xbmc_import(self):
        with open(os.path.join(SRC, "helpers.py")) as f:
            src = f.read()
        for lineno, line in enumerate(src.splitlines(), 1):
            if re.match(r"^(import xbmc|from xbmc)", line):
                self.fail("module-level xbmc import at line %d: %s" % (lineno, line))

    def test_lazy_xbmc_imports_present_and_indented(self):
        with open(os.path.join(SRC, "helpers.py")) as f:
            src = f.read()
        self.assertTrue(re.search(r"^\s+import xbmc$", src, re.M),
                        "missing lazy 'import xbmc'")
        self.assertTrue(re.search(r"^\s+import xbmcaddon$", src, re.M),
                        "missing lazy 'import xbmcaddon'")


    def test_read_kodi_proxy_setting_returns_value(self):
        fake = _FakeXbmc(['{"id":1,"jsonrpc":"2.0","result":{"value":1080}}'])
        with _kodi_module("xbmc", fake):
            self.assertEqual(
                helpers.read_kodi_proxy_setting("network.httpproxyport"), 1080)

    def test_read_kodi_proxy_setting_sends_get_setting_value(self):
        fake = _FakeXbmc(['{"id":1,"jsonrpc":"2.0","result":{"value":1}}'])
        with _kodi_module("xbmc", fake):
            helpers.read_kodi_proxy_setting("network.usehttpproxy")
        req = json.loads(fake.calls[0])
        self.assertEqual(req["jsonrpc"], "2.0")
        self.assertEqual(req["method"], "Settings.GetSettingValue")
        self.assertEqual(req["params"], {"setting": "network.usehttpproxy"})

    def test_read_kodi_proxy_setting_malformed_json_is_none(self):
        fake = _FakeXbmc(["not json at all"])
        with _kodi_module("xbmc", fake):
            self.assertIsNone(
                helpers.read_kodi_proxy_setting("network.usehttpproxy"))

    def test_read_kodi_proxy_setting_error_response_is_none(self):
        fake = _FakeXbmc(['{"id":1,"jsonrpc":"2.0","error":{"code":-32602,"message":"x"}}'])
        with _kodi_module("xbmc", fake):
            self.assertIsNone(
                helpers.read_kodi_proxy_setting("network.usehttpproxy"))

    def test_read_kodi_proxy_setting_empty_response_is_none(self):
        fake = _FakeXbmc([""])
        with _kodi_module("xbmc", fake):
            self.assertIsNone(
                helpers.read_kodi_proxy_setting("network.usehttpproxy"))

    def test_read_kodi_proxy_setting_without_xbmc_is_none(self):
        self.assertIsNone(helpers.read_kodi_proxy_setting("network.usehttpproxy"))

    def test_read_kodi_proxy_setting_non_dict_result_is_none(self):
        fake = _FakeXbmc(['{"id":1,"jsonrpc":"2.0","result":true}'])
        with _kodi_module("xbmc", fake):
            self.assertIsNone(
                helpers.read_kodi_proxy_setting("network.usehttpproxy"))

    def test_write_kodi_proxy_setting_sends_set_setting_value(self):
        fake = _FakeXbmc(['{"id":1,"jsonrpc":"2.0","result":true}'])
        with _kodi_module("xbmc", fake):
            self.assertTrue(
                helpers.write_kodi_proxy_setting("network.usehttpproxy", True))
        req = json.loads(fake.calls[0])
        self.assertEqual(req["method"], "Settings.SetSettingValue")
        self.assertEqual(req["params"],
                         {"setting": "network.usehttpproxy", "value": True})

    def test_write_kodi_proxy_setting_failures_are_false(self):
        for resp in (["bad json"],
                     ['{"id":1,"jsonrpc":"2.0","error":{"code":1,"message":"e"}}'],
                     ['{"id":1,"jsonrpc":"2.0","result":false}'],
                     [""]):
            fake = _FakeXbmc(resp)
            with _kodi_module("xbmc", fake):
                self.assertFalse(
                    helpers.write_kodi_proxy_setting("network.usehttpproxy", True))

    def test_write_kodi_proxy_setting_without_xbmc_is_false(self):
        self.assertFalse(
            helpers.write_kodi_proxy_setting("network.usehttpproxy", True))


    def test_addon_available_true_for_installed(self):
        fake = _FakeXbmcAddon(addon=_FakeAddon())
        with _kodi_module("xbmcaddon", fake):
            self.assertTrue(helpers.addon_available("plugin.video.youtube"))

    def test_addon_available_false_for_missing(self):
        fake = _FakeXbmcAddon(addon=_FakeAddon(),
                              missing=("plugin.video.youtube",))
        with _kodi_module("xbmcaddon", fake):
            self.assertFalse(helpers.addon_available("plugin.video.youtube"))

    def test_addon_available_false_without_xbmcaddon(self):
        self.assertFalse(helpers.addon_available("plugin.video.youtube"))

    def test_read_addon_setting_youtube_proxy_source_uses_typed_int_accessor(self):
        addon = _FakeAddon({"requests.proxy.source": 1})
        fake = _FakeXbmcAddon(addon=addon)
        with _kodi_module("xbmcaddon", fake):
            value = helpers.read_addon_setting(
                "plugin.video.youtube", "requests.proxy.source")
        self.assertEqual(value, 1)
        self.assertIn(("getSettingInt", "requests.proxy.source"), addon.calls)

    def test_read_addon_setting_generic_string_stays_string(self):
        addon = _FakeAddon({"some.text": "hello"})
        fake = _FakeXbmcAddon(addon=addon)
        with _kodi_module("xbmcaddon", fake):
            value = helpers.read_addon_setting("plugin.video.youtube", "some.text")
        self.assertEqual(value, "hello")

    def test_read_addon_setting_generic_numeric_coerces_to_int(self):
        addon = _FakeAddon({"some.number": "42"})
        fake = _FakeXbmcAddon(addon=addon)
        with _kodi_module("xbmcaddon", fake):
            value = helpers.read_addon_setting("plugin.video.youtube", "some.number")
        self.assertEqual(value, 42)

    def test_read_addon_setting_generic_bool_coerces(self):
        addon = _FakeAddon({"some.flag": "false"})
        fake = _FakeXbmcAddon(addon=addon)
        with _kodi_module("xbmcaddon", fake):
            self.assertFalse(
                helpers.read_addon_setting("plugin.video.youtube", "some.flag"))

    def test_read_addon_setting_missing_addon_is_none(self):
        fake = _FakeXbmcAddon(addon=_FakeAddon(),
                              missing=("plugin.video.youtube",))
        with _kodi_module("xbmcaddon", fake):
            self.assertIsNone(helpers.read_addon_setting(
                "plugin.video.youtube", "requests.proxy.source"))

    def test_write_addon_setting_youtube_proxy_source_uses_typed_int_accessor(self):
        addon = _FakeAddon()
        fake = _FakeXbmcAddon(addon=addon)
        with _kodi_module("xbmcaddon", fake):
            ok = helpers.write_addon_setting(
                "plugin.video.youtube", "requests.proxy.source", 1)
        self.assertTrue(ok)
        self.assertIn(("setSettingInt", "requests.proxy.source", 1), addon.calls)

    def test_write_addon_setting_bool_uses_set_setting_bool(self):
        addon = _FakeAddon()
        fake = _FakeXbmcAddon(addon=addon)
        with _kodi_module("xbmcaddon", fake):
            ok = helpers.write_addon_setting(
                "plugin.video.youtube", "some.flag", False)
        self.assertTrue(ok)
        self.assertIn(("setSettingBool", "some.flag", False), addon.calls)

    def test_write_addon_setting_int_uses_set_setting_int(self):
        addon = _FakeAddon()
        fake = _FakeXbmcAddon(addon=addon)
        with _kodi_module("xbmcaddon", fake):
            ok = helpers.write_addon_setting(
                "plugin.video.youtube", "some.number", 7)
        self.assertTrue(ok)
        self.assertIn(("setSettingInt", "some.number", 7), addon.calls)

    def test_write_addon_setting_string_uses_set_setting(self):
        addon = _FakeAddon()
        fake = _FakeXbmcAddon(addon=addon)
        with _kodi_module("xbmcaddon", fake):
            ok = helpers.write_addon_setting(
                "plugin.video.youtube", "some.text", "hi")
        self.assertTrue(ok)
        self.assertIn(("setSetting", "some.text", "hi"), addon.calls)

    def test_write_addon_setting_missing_addon_is_false(self):
        fake = _FakeXbmcAddon(addon=_FakeAddon(),
                              missing=("plugin.video.youtube",))
        with _kodi_module("xbmcaddon", fake):
            self.assertFalse(helpers.write_addon_setting(
                "plugin.video.youtube", "requests.proxy.source", 1))


    def test_integration_backup_path_under_profile_dir(self):
        path = helpers.integration_backup_path()
        self.assertTrue(path.endswith("integration_backup.json"))
        self.assertEqual(os.path.dirname(path), helpers.profile_dir())


# ----------------------------------------------------------------------
# service lifecycle wiring (main.py)
# ----------------------------------------------------------------------

INTEGRATION_HOST = "127.0.0.1"


class _FakeMonitor(object):
    """xbmc.Monitor stand-in that aborts after `iterations` loop passes.

    When `waitForAbort` decides to abort it records ("loop.abort",) into
    `events`, so the wiring test can prove the in-loop ``begin_shutdown``
    runs before the loop breaks.
    """

    def __init__(self, iterations=1, events=None):
        self._left = iterations
        self.events = events

    def abortRequested(self):
        return self._left <= 0

    def waitForAbort(self, seconds):
        self._left -= 1
        if self._left <= 0:
            if self.events is not None:
                self.events.append(("loop.abort",))
            return True
        return False


class _FakeXbmcModule(object):
    LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = 0, 1, 2, 3

    def __init__(self):
        self.messages = []
        self.Monitor = _FakeMonitor

    def log(self, msg, level=1):
        self.messages.append((msg, level))


class _FakeDialog(object):
    def __init__(self, sink):
        self._sink = sink

    def notification(self, heading, msg, icon, millis):
        self._sink.append((msg, icon))


class _FakeXbmcGui(object):
    NOTIFICATION_INFO = "info"
    NOTIFICATION_ERROR = "error"

    def __init__(self):
        self.notifications = []

    def Dialog(self):
        return _FakeDialog(self.notifications)


class _LogRecorder(object):
    def __init__(self):
        self.entries = []

    def __call__(self, msg, level="info"):
        self.entries.append((level, msg))

    def of_level(self, level):
        return [msg for lvl, msg in self.entries if lvl == level]


class _NotifyRecorder(object):
    def __init__(self):
        self.messages = []

    def __call__(self, msg, error=False):
        self.messages.append((msg, error))


class _FakeIntegrationManager(object):
    """Behavioral IntegrationManager stand-in.

    Emulates the real contract closely enough to assert lifecycle ordering:
    a successful ensure leaves a backup behind, a successful restore consumes
    it, and validate reports whether our values are still in place.
    """

    def __init__(self, calls=None, backup=False, ensure=True, restore=True,
                 validate=None, raises=()):
        self.calls = calls if calls is not None else []
        self.backup = backup
        self.backup_checks = 0
        self._ensure = ensure
        self._restore = restore
        self._validate = validate
        self._raises = set(raises)

    def ensure_configured(self, host, port):
        self.calls.append(("ensure", host, port))
        self._maybe_raise("ensure_configured")
        if self._ensure:
            self.backup = True
        return self._ensure

    def validate(self, host, port):
        self.calls.append(("validate", host, port))
        self._maybe_raise("validate")
        if self._validate is not None:
            return self._validate
        return self.backup

    def restore_previous(self):
        self.calls.append(("restore",))
        self._maybe_raise("restore_previous")
        if self._restore:
            self.backup = False
        return self._restore

    def backup_exists(self):
        self.backup_checks += 1
        self._maybe_raise("backup_exists")
        return self.backup

    def _maybe_raise(self, name):
        if name in self._raises:
            raise RuntimeError("%s exploded" % name)

    @property
    def writes(self):
        return [c for c in self.calls if c[0] in ("ensure", "restore")]


class _FakeEngine(object):
    def __init__(self):
        self.engine = "sing-box"
        self.running = False

    def is_running(self):
        return self.running


class _FakeStore(object):
    def __init__(self, enabled=True):
        self.active_tag = None
        self._enabled = enabled

    def enabled(self):
        return [{"tag": "p"}] if self._enabled else []


class _FakeSupervisor(object):
    """ProxySupervisor stand-in recording into the shared call log.

    `start`/`reconfigure_engine` emulate the busy-port fallback by landing on
    `local_port + 1`, so tests can tell the effective port apart from the
    configured one. `reconfigure_ok` (defaulting to `start_ok`) lets a test
    fail reconfiguration while keeping start healthy.
    """

    def __init__(self, calls, settings, start_ok=True, profiles_enabled=True,
                 should_stop=None, reconfigure_ok=None):
        self.calls = calls
        self.settings = dict(settings)
        self.store = _FakeStore(profiles_enabled)
        self.bin = _FakeEngine()
        self.effective_port = None
        self.last_error = "start failed"
        self.start_ok = start_ok
        self.reconfigure_ok = reconfigure_ok
        self.should_stop = should_stop or (lambda: False)

    def _bring_up(self, ok=None):
        if ok is None:
            ok = self.start_ok
        if not ok:
            self.bin.running = False
            return False
        self.effective_port = int(self.settings.get("local_port", 1080)) + 1
        self.bin.running = True
        return True

    def start(self):
        self.calls.append(("sup.start",))
        return self._bring_up()

    def stop(self):
        self.calls.append(("sup.stop",))
        self.bin.running = False

    def begin_shutdown(self):
        self.calls.append(("sup.begin_shutdown",))

    def reconfigure_engine(self):
        self.calls.append(("sup.reconfigure",))
        return self._bring_up(ok=self.reconfigure_ok)

    def reload_profiles(self):
        self.calls.append(("sup.reload_profiles",))

    def restart(self):
        self.calls.append(("sup.restart",))

    def tick(self):
        pass


@contextlib.contextmanager
def _patched(obj, name, value):
    saved = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, saved)


def _import_main():
    """Load service.advancedproxy/main.py against fake xbmc modules."""
    with _kodi_module("xbmc", _FakeXbmcModule()), \
            _kodi_module("xbmcgui", _FakeXbmcGui()):
        spec = importlib.util.spec_from_file_location(
            "advancedproxy_main", os.path.join(ADDON_DIR, "main.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _settings(**over):
    base = {
        "engine": "sing-box", "mode": "urltest", "local_port": 1080,
        "autostart": True, "notify": True, "auto_configure_integration": True,
    }
    base.update(over)
    return base


def _run_main(settings_seq, manager, start_ok=True, profiles_enabled=True,
              iterations=1, mtimes=None, reconfigure_ok=None):
    """Run main() end to end against fakes; returns (module, supervisor)."""
    module = _import_main()
    tmp = tempfile.mkdtemp()
    seq = [dict(s) for s in settings_seq]
    holder = {}
    wiring = []
    holder["wiring"] = wiring

    def _get_settings(reader=None):
        return dict(seq.pop(0) if len(seq) > 1 else seq[0])

    def _make_monitor():
        monitor = _FakeMonitor(iterations, events=manager.calls)
        wiring.append("monitor.created")
        holder["monitor"] = monitor
        return monitor

    def _make_supervisor(**kwargs):
        wiring.append("supervisor.created")
        sup = _FakeSupervisor(
            manager.calls, kwargs["settings"], start_ok=start_ok,
            profiles_enabled=profiles_enabled,
            should_stop=kwargs.get("should_stop"),
            reconfigure_ok=reconfigure_ok)
        sup.wiring = wiring
        sup.monitor = holder.get("monitor")
        holder["sup"] = sup
        return sup

    with contextlib.ExitStack() as stack:
        patch = stack.enter_context
        patch(_patched(module.helpers, "get_settings", _get_settings))
        patch(_patched(module.helpers, "profile_dir", lambda: tmp))
        patch(_patched(module.helpers, "profiles_path",
                       lambda: os.path.join(tmp, "profiles.json")))
        patch(_patched(module.helpers, "log_path",
                       lambda: os.path.join(tmp, "engine.log")))
        patch(_patched(module.supervisor, "ProxySupervisor", _make_supervisor))
        patch(_patched(module, "build_integration_manager",
                       lambda logger=None, notify=None: manager))
        patch(_patched(module.xbmc, "Monitor", _make_monitor))
        if mtimes is not None:
            stamps = list(mtimes)
            patch(_patched(module, "_profiles_mtime",
                           lambda path: stamps.pop(0) if len(stamps) > 1
                           else stamps[0]))
        module.main()
    return module, holder["sup"]


class TestIntegrationManagerConstruction(unittest.TestCase):
    def setUp(self):
        self.main = _import_main()
        self.built = {}

        def _recorder(**kwargs):
            self.built.update(kwargs)
            return "manager"

        self.recorder = _recorder

    @contextlib.contextmanager
    def _building(self, **over):
        """Build the manager and keep the helper patches alive for the body.

        The adapters must be exercised while patched: ``addon_available`` is
        a closure that resolves ``helpers`` at call time.
        """
        with contextlib.ExitStack() as stack:
            stack.enter_context(_patched(self.main.proxy_integration,
                                         "IntegrationManager", self.recorder))
            for name, value in over.items():
                stack.enter_context(_patched(self.main.helpers, name, value))
            self.main.build_integration_manager()
            yield self.built

    def test_manager_is_built_from_helper_backup_path(self):
        with self._building(integration_backup_path=lambda: "/tmp/backup.json") as built:
            self.assertEqual(built["backup_path"], "/tmp/backup.json")

    def test_kodi_adapters_delegate_to_helpers(self):
        reads, writes = [], []
        with self._building(
                read_kodi_proxy_setting=lambda sid: reads.append(sid) or 7,
                write_kodi_proxy_setting=lambda sid, v: writes.append((sid, v)) or True) as built:
            self.assertEqual(built["read_kodi"]("network.httpproxyport"), 7)
            self.assertTrue(built["write_kodi"]("network.httpproxyport", 1081))
        self.assertEqual(reads, ["network.httpproxyport"])
        self.assertEqual(writes, [("network.httpproxyport", 1081)])

    def test_addon_available_is_zero_arg_and_asks_for_youtube(self):
        asked = []
        with self._building(
                addon_available=lambda addon_id: asked.append(addon_id) or True) as built:
            self.assertTrue(built["addon_available"]())
        self.assertEqual(asked, [self.main.proxy_integration.YOUTUBE_ADDON_ID])

    def test_addon_adapters_delegate_to_helpers(self):
        reads, writes = [], []
        with self._building(
                read_addon_setting=lambda a, s: reads.append((a, s)) or 0,
                write_addon_setting=lambda a, s, v: writes.append((a, s, v)) or True) as built:
            self.assertEqual(built["read_addon"]("plugin.video.youtube",
                                                 "requests.proxy.source"), 0)
            self.assertTrue(built["write_addon"]("plugin.video.youtube",
                                                 "requests.proxy.source", 1))
        self.assertEqual(reads, [("plugin.video.youtube", "requests.proxy.source")])
        self.assertEqual(writes,
                         [("plugin.video.youtube", "requests.proxy.source", 1)])

    def test_logger_and_notify_are_forwarded(self):
        log, notify = _LogRecorder(), _NotifyRecorder()
        with _patched(self.main.proxy_integration, "IntegrationManager",
                      self.recorder):
            self.main.build_integration_manager(log, notify)
        self.assertIs(self.built["logger"], log)
        self.assertIs(self.built["notify"], notify)

    def test_real_manager_is_constructible_with_helper_adapters(self):
        tmp = tempfile.mkdtemp()
        with _patched(self.main.helpers, "integration_backup_path",
                      lambda: os.path.join(tmp, "integration_backup.json")):
            manager = self.main.build_integration_manager()
        self.assertIsInstance(manager,
                              self.main.proxy_integration.IntegrationManager)
        # adapters are unusable outside Kodi, but must degrade instead of raise
        self.assertFalse(manager.ensure_configured(INTEGRATION_HOST, 1081))
        self.assertFalse(manager.backup_exists())


class TestIntegrationLifecycle(unittest.TestCase):
    def setUp(self):
        self.main = _import_main()
        self.log = _LogRecorder()
        self.notify = _NotifyRecorder()

    def _lifecycle(self, manager):
        return self.main.IntegrationLifecycle(manager, self.log, self.notify)

    def test_ensures_localhost_and_effective_port_when_running(self):
        manager = _FakeIntegrationManager()
        self.assertTrue(self._lifecycle(manager).sync(True, True, 1081))
        self.assertEqual(manager.calls, [("ensure", INTEGRATION_HOST, 1081)])

    def test_does_not_ensure_while_disabled(self):
        manager = _FakeIntegrationManager()
        self.assertFalse(self._lifecycle(manager).sync(False, True, 1081))
        self.assertEqual(manager.calls, [])

    def test_does_not_ensure_when_proxy_is_not_running(self):
        manager = _FakeIntegrationManager()
        self.assertFalse(self._lifecycle(manager).sync(True, False, 1081))
        self.assertEqual(manager.calls, [])

    def test_does_not_ensure_without_an_effective_port(self):
        manager = _FakeIntegrationManager()
        self.assertFalse(self._lifecycle(manager).sync(True, True, None))
        self.assertEqual(manager.calls, [])

    def test_disabling_restores_then_validates_read_only(self):
        manager = _FakeIntegrationManager(backup=True)
        self._lifecycle(manager).sync(False, True, 1081)
        self.assertEqual(manager.calls,
                         [("restore",), ("validate", INTEGRATION_HOST, 1081)])

    def test_disabling_warns_when_settings_still_point_at_the_proxy(self):
        manager = _FakeIntegrationManager(backup=True, validate=True)
        self._lifecycle(manager).sync(False, True, 1081)
        self.assertTrue(any("1081" in msg for msg in self.log.of_level("warn")),
                        self.log.entries)
        self.assertEqual([c for c in manager.calls if c[0] == "ensure"], [])

    def test_disabling_without_backup_touches_nothing(self):
        manager = _FakeIntegrationManager(backup=False)
        self._lifecycle(manager).sync(False, True, 1081)
        self.assertEqual(manager.calls, [])

    def test_re_enabling_ensures_again(self):
        manager = _FakeIntegrationManager(backup=True)
        lifecycle = self._lifecycle(manager)
        lifecycle.sync(False, True, 1081)
        manager.calls[:] = []
        self.assertTrue(lifecycle.sync(True, True, 1081))
        self.assertEqual(manager.calls, [("ensure", INTEGRATION_HOST, 1081)])

    def test_stale_backup_is_restored_when_nothing_is_running(self):
        manager = _FakeIntegrationManager(backup=True)
        self._lifecycle(manager).sync(True, False, 1080)
        self.assertEqual(manager.calls, [("restore",)])

    def test_shutdown_restores_previous_values(self):
        manager = _FakeIntegrationManager(backup=True)
        self.assertTrue(self._lifecycle(manager).shutdown())
        self.assertEqual(manager.calls, [("restore",)])

    def test_shutdown_without_backup_does_not_write(self):
        manager = _FakeIntegrationManager(backup=False)
        self.assertFalse(self._lifecycle(manager).shutdown())
        self.assertEqual(manager.calls, [])

    def test_failed_ensure_is_logged_and_notified(self):
        manager = _FakeIntegrationManager(ensure=False)
        self.assertFalse(self._lifecycle(manager).sync(True, True, 1081))
        self.assertTrue(self.log.of_level("warn"))
        self.assertTrue(any(err for _, err in self.notify.messages))

    def test_raising_manager_never_propagates_on_ensure(self):
        manager = _FakeIntegrationManager(raises=("ensure_configured",))
        self.assertFalse(self._lifecycle(manager).sync(True, True, 1081))
        self.assertTrue(self.log.of_level("error"))

    def test_raising_manager_never_propagates_on_restore(self):
        manager = _FakeIntegrationManager(backup=True,
                                          raises=("restore_previous",))
        self.assertFalse(self._lifecycle(manager).shutdown())
        self.assertTrue(self.log.of_level("error"))

    def test_raising_backup_probe_never_propagates(self):
        manager = _FakeIntegrationManager(raises=("backup_exists",))
        self.assertFalse(self._lifecycle(manager).shutdown())
        self.assertTrue(self.log.of_level("error"))

    def test_broken_logger_and_notifier_are_survivable(self):
        def boom(*args, **kwargs):
            raise RuntimeError("logger down")

        manager = _FakeIntegrationManager(ensure=False)
        lifecycle = self.main.IntegrationLifecycle(manager, boom, boom)
        self.assertFalse(lifecycle.sync(True, True, 1081))


class TestMainLifecycleWiring(unittest.TestCase):
    def test_successful_autostart_configures_effective_port(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings()], manager)
        self.assertEqual(manager.calls[:2],
                         [("sup.start",), ("ensure", INTEGRATION_HOST, 1081)])

    def test_configured_port_is_not_used_when_engine_falls_back(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(local_port=9090)], manager)
        self.assertIn(("ensure", INTEGRATION_HOST, 9091), manager.calls)
        self.assertNotIn(("ensure", INTEGRATION_HOST, 9090), manager.calls)

    def test_shutdown_restores_before_stopping_the_engine(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings()], manager)
        self.assertLess(manager.calls.index(("restore",)),
                        manager.calls.index(("sup.stop",)))

    def test_failed_start_restores_stale_backup_and_never_ensures(self):
        manager = _FakeIntegrationManager(backup=True)
        _run_main([_settings()], manager, start_ok=False)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("restore",), ("loop.abort",),
                          ("sup.begin_shutdown",), ("sup.begin_shutdown",),
                          ("sup.stop",)])

    def test_no_profiles_restores_stale_backup(self):
        manager = _FakeIntegrationManager(backup=True)
        _run_main([_settings()], manager, profiles_enabled=False)
        self.assertEqual(manager.calls,
                         [("restore",), ("loop.abort",),
                          ("sup.begin_shutdown",), ("sup.begin_shutdown",),
                          ("sup.stop",)])

    def test_autostart_off_restores_stale_backup(self):
        manager = _FakeIntegrationManager(backup=True)
        _run_main([_settings(autostart=False)], manager)
        self.assertEqual(manager.calls,
                         [("restore",), ("loop.abort",),
                          ("sup.begin_shutdown",), ("sup.begin_shutdown",),
                          ("sup.stop",)])

    def test_idle_service_without_backup_writes_nothing(self):
        manager = _FakeIntegrationManager(backup=False)
        _run_main([_settings(autostart=False)], manager)
        self.assertEqual(manager.writes, [])

    def test_disabling_setting_at_runtime_restores_and_validates(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(), _settings(auto_configure_integration=False)],
                  manager)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("ensure", INTEGRATION_HOST, 1081),
                          ("restore",), ("validate", INTEGRATION_HOST, 1081),
                          ("loop.abort",), ("sup.begin_shutdown",),
                          ("sup.begin_shutdown",), ("sup.stop",)])

    def test_enabling_setting_at_runtime_ensures(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(auto_configure_integration=False), _settings()],
                  manager)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("ensure", INTEGRATION_HOST, 1081),
                          ("loop.abort",), ("sup.begin_shutdown",),
                          ("sup.begin_shutdown",), ("restore",),
                          ("sup.stop",)])

    def test_port_change_reconfigures_then_ensures_new_port(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(), _settings(local_port=9090)], manager)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("ensure", INTEGRATION_HOST, 1081),
                          ("sup.reconfigure",),
                          ("ensure", INTEGRATION_HOST, 9091),
                          ("loop.abort",), ("sup.begin_shutdown",),
                          ("sup.begin_shutdown",), ("restore",),
                          ("sup.stop",)])

    def test_failed_reconfigure_restores_instead_of_ensuring(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(), _settings(local_port=9090)], manager,
                  start_ok=False)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("sup.reconfigure",), ("loop.abort",),
                          ("sup.begin_shutdown",), ("sup.begin_shutdown",),
                          ("sup.stop",)])

    def test_unrelated_setting_change_does_not_touch_integration(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(), _settings(notify=False)], manager)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("ensure", INTEGRATION_HOST, 1081),
                          ("loop.abort",), ("sup.begin_shutdown",),
                          ("sup.begin_shutdown",), ("restore",),
                          ("sup.stop",)])

    def test_start_after_profile_change_ensures_effective_port(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(autostart=False), _settings()], manager,
                  mtimes=[0, 5])
        self.assertEqual(manager.calls,
                         [("sup.reload_profiles",), ("sup.start",),
                          ("ensure", INTEGRATION_HOST, 1081),
                          ("loop.abort",), ("sup.begin_shutdown",),
                          ("sup.begin_shutdown",), ("restore",),
                          ("sup.stop",)])

    def test_monitor_created_before_supervisor_and_should_stop_injected(self):
        manager = _FakeIntegrationManager()
        _, sup = _run_main([_settings()], manager)
        self.assertLess(sup.wiring.index("monitor.created"),
                        sup.wiring.index("supervisor.created"))
        self.assertIs(sup.should_stop.__self__, sup.monitor)

    def test_aborting_monitor_orders_shutdown_events(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings()], manager)
        self.assertEqual(manager.calls[-5:],
                         [("loop.abort",), ("sup.begin_shutdown",),
                          ("sup.begin_shutdown",), ("restore",),
                          ("sup.stop",)])

    def test_reconfigure_failure_restores_without_new_ensure(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(), _settings(local_port=9090)], manager,
                  reconfigure_ok=False)
        self.assertEqual([c for c in manager.calls if c[0] == "ensure"],
                         [("ensure", INTEGRATION_HOST, 1081)])
        self.assertLess(manager.calls.index(("restore",)),
                        manager.calls.index(("sup.stop",)))

    def test_integration_failure_never_stops_the_engine(self):
        manager = _FakeIntegrationManager(
            raises=("ensure_configured", "restore_previous"))
        module, sup = _run_main([_settings()], manager)
        self.assertIn(("sup.start",), manager.calls)
        self.assertIn(("sup.stop",), manager.calls)
        self.assertFalse(sup.bin.is_running())
        self.assertTrue([m for m, err in module.xbmcgui.notifications if err])


class _FakeProcess(object):
    """subprocess.Popen stand-in: alive until it exits with `returncode`."""

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def exit(self, code=1):
        self.returncode = code


class _FakeBinaryManager(object):
    """BinaryManager stand-in honouring the real proc/is_running contract."""

    def __init__(self, calls, engine="sing-box", platform="linux_x64"):
        self.calls = calls
        self.engine = engine
        self.platform = platform
        self.proc = None

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, config_path, port=None):
        self.calls.append(("start", config_path, port))
        self.proc = _FakeProcess()
        return self.proc

    def stop(self, port=None):
        self.calls.append(("stop", port))
        self.proc = None

    def restart(self, config_path, port=None):
        self.calls.append(("restart", config_path, port))
        self.proc = _FakeProcess()
        return self.proc

    def check(self, config_path):
        return True, ""

    def crash(self, code=1):
        self.proc.exit(code)


class _FakeClock(object):
    """`time` module stand-in: the supervisor's only wall-clock boundary."""

    def __init__(self, now=1000.0):
        self.now = now

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestSupervisorTick(unittest.TestCase):
    """tick() against a real ProxySupervisor with faked engine and clock."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        settings = {
            "engine": "sing-box", "mode": "manual", "local_port": 1080,
            "autostart": True, "urltest_interval": "3m", "urltest_tolerance": 50,
            "test_url": "https://www.gstatic.com/generate_204",
            "interrupt_connections": True, "log_level": "info",
            "binary_platform_override": "auto", "binary_custom_path": "",
        }
        self.log = _LogRecorder()
        self.notify = _NotifyRecorder()
        self.sup = supervisor.ProxySupervisor(
            settings=settings, addon_dir=self.tmp, work_dir=self.tmp,
            logger=self.log, notify=self.notify)
        self.sup.store.add_uri(VLESS)
        self.sup.store.add_uri(HY2)
        self.calls = []
        self.sup.bin = _FakeBinaryManager(self.calls)
        self.clock = _FakeClock()
        saved = supervisor.time
        supervisor.time = self.clock
        self.addCleanup(setattr, supervisor, "time", saved)

    def _kinds(self):
        return [c[0] for c in self.calls]

    def _started(self):
        self.assertTrue(self.sup.start(), self.sup.last_error)
        self.calls[:] = []
        self.notify.messages[:] = []

    # ----- the regression --------------------------------------------
    def test_healthy_engine_is_not_restarted_when_180s_elapse(self):
        self._started()
        self.clock.advance(181)
        self.sup.tick()
        self.assertEqual(self.calls, [],
                         "healthy engine was torn down by the periodic timer")

    def test_healthy_engine_process_survives_an_hour_of_ticks(self):
        self._started()
        proc = self.sup.bin.proc
        for _ in range(20):
            self.clock.advance(180)
            self.sup.tick()
        self.assertEqual(self.calls, [])
        self.assertIs(self.sup.bin.proc, proc, "engine process was replaced")
        self.assertTrue(self.sup.bin.is_running())

    # ----- behaviour that must be preserved ---------------------------
    def test_tick_notifies_once_when_the_engine_comes_up(self):
        self.sup._resolve_effective_port()
        self.assertTrue(self.sup.build_and_write_config())
        self.sup.bin.start(self.sup.config_path)
        self.notify.messages[:] = []
        self.sup.tick()
        self.assertTrue([m for m, err in self.notify.messages if "proxy up" in m],
                        self.notify.messages)
        self.notify.messages[:] = []
        self.clock.advance(300)
        self.sup.tick()
        self.assertEqual(self.notify.messages, [])

    def test_tick_notifies_active_profile_change_without_restarting(self):
        self._started()
        self.sup.store.set_active("AUTO:Hysteria2")
        self.clock.advance(300)
        self.sup.tick()
        self.assertIn(("Active profile: AUTO:Hysteria2", False),
                      self.notify.messages)
        self.assertEqual(self.calls, [])

    def test_crashed_engine_is_restarted_after_backoff(self):
        self._started()
        self.sup.bin.crash(2)
        self.sup.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sup.consecutive_failures, 1)
        self.assertTrue([m for m, err in self.notify.messages if err],
                        self.notify.messages)
        self.clock.advance(1)
        self.sup.tick()
        self.assertEqual(self.calls, [], "restarted before the backoff elapsed")
        self.clock.advance(1)
        self.sup.tick()
        self.assertEqual(self._kinds(), ["start"])
        self.assertTrue(self.sup.bin.is_running())

    def test_backoff_grows_with_consecutive_failures(self):
        self._started()
        delays = []
        for _ in range(4):
            self.sup.bin.crash()
            self.sup.tick()
            delays.append(self.sup._restart_at - self.clock.now)
            self.clock.advance(delays[-1])
            self.sup.tick()
        self.assertEqual(delays, [2, 4, 8, 16])

    def test_recovered_engine_resets_the_failure_counter(self):
        self._started()
        self.sup.bin.crash()
        self.sup.tick()
        self.clock.advance(2)
        self.sup.tick()
        self.clock.advance(1)
        self.sup.tick()
        self.assertEqual(self.sup.consecutive_failures, 0)
        self.assertIsNone(self.sup._restart_at)

    def test_gives_up_after_too_many_failures(self):
        self._started()
        self.sup.bin.stop()
        self.calls[:] = []
        self.sup.consecutive_failures = 11
        self.sup._restart_at = self.clock.now
        self.sup.tick()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.sup._restart_at)

    def test_backoff_is_capped_at_60_seconds(self):
        self._started()
        self.sup.consecutive_failures = 6
        delays = []
        for _ in range(3):
            self.sup.bin.crash()
            self.sup.tick()
            delays.append(self.sup._restart_at - self.clock.now)
            self.clock.advance(delays[-1])
            self.sup.tick()
        self.assertEqual(delays, [60, 60, 60])

    # ----- shutdown-aware watchdog -----------------------------------
    def test_clean_exit_after_begin_shutdown_does_not_arm_restart(self):
        self._started()
        self.sup.begin_shutdown()
        self.sup.bin.crash(0)
        self.sup.tick()
        self.assertIsNone(self.sup._restart_at)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sup.consecutive_failures, 0)
        self.assertEqual([m for m, err in self.notify.messages if err], [])

    def test_begin_shutdown_cancels_a_pending_restart(self):
        self._started()
        self.sup.bin.crash(1)
        self.sup.tick()
        self.assertIsNotNone(self.sup._restart_at)
        self.sup.begin_shutdown()
        self.assertIsNone(self.sup._restart_at)

    def test_begin_shutdown_is_idempotent(self):
        self._started()
        self.sup.begin_shutdown()
        self.sup.begin_shutdown()
        self.assertTrue(self.sup._shutting_down)
        self.assertIsNone(self.sup._restart_at)

    def test_stop_during_shutdown_keeps_watchdog_cancelled_and_state_false(self):
        self._started()
        self.sup.bin.crash(1)
        self.sup.tick()
        self.sup.begin_shutdown()
        self.assertIsNone(self.sup._restart_at)
        self.sup.stop()
        self.assertIsNone(self.sup._restart_at)
        with open(self.sup.state_path) as f:
            st = json.load(f)
        self.assertFalse(st["running"])

    def test_stop_is_idempotent_and_safe_without_a_process(self):
        self.sup.stop()
        self.sup.stop()
        with open(self.sup.state_path) as f:
            st = json.load(f)
        self.assertFalse(st["running"])

    def test_stop_persists_state_and_forwards_the_effective_port(self):
        self._started()
        self.sup.stop()
        self.assertIn(("stop", self.sup.effective_port), self.calls)
        with open(self.sup.state_path) as f:
            st = json.load(f)
        self.assertFalse(st["running"])

    # ----- should_stop injection -------------------------------------
    def test_exit_observed_while_should_stop_is_classified_as_shutdown(self):
        self._started()
        self.sup.should_stop = lambda: True
        self.sup.bin.crash(1)
        self.sup.tick()
        self.assertIsNone(self.sup._restart_at)
        self.assertEqual(self.sup.consecutive_failures, 0)
        self.assertEqual([m for m, err in self.notify.messages if err], [])
        self.assertEqual(self.calls, [])

    def test_restart_about_to_fire_is_cancelled_when_should_stop_turns_true(self):
        self._started()
        self.sup.bin.crash(1)
        self.sup.tick()
        self.assertIsNotNone(self.sup._restart_at)
        self.sup.should_stop = lambda: True
        self.clock.advance(3)
        self.sup.tick()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.sup._restart_at)
        self.assertFalse(self.sup.bin.is_running())

    # ----- no restart after shutdown ---------------------------------
    def test_start_after_shutdown_does_not_start_an_engine(self):
        self._started()
        self.sup.begin_shutdown()
        self.calls[:] = []
        self.assertFalse(self.sup.start())
        self.assertEqual(self.calls, [])

    def test_restart_after_shutdown_does_not_restart_the_engine(self):
        self._started()
        proc = self.sup.bin.proc
        self.sup.begin_shutdown()
        self.calls[:] = []
        self.sup.restart()
        self.assertEqual(self.calls, [])
        self.assertIs(self.sup.bin.proc, proc)
        self.assertTrue(self.sup.bin.is_running())

    def test_idle_supervisor_that_never_started_does_nothing(self):
        self.clock.advance(3600)
        self.sup.tick()
        self.assertEqual(self.calls, [])

    # ----- explicit reconfiguration still restarts --------------------
    def test_profile_activation_change_rebuilds_config_and_restarts(self):
        self._started()
        self.assertTrue(self.sup.store.set_active("AUTO:Hysteria2"))
        self.sup.restart()
        self.assertIn("restart", self._kinds())
        with open(self.sup.config_path) as f:
            cfg = json.load(f)
        sel = [o for o in cfg["outbounds"] if o["type"] == "selector"][0]
        self.assertEqual(sel["default"], "AUTO:Hysteria2")
        self.assertTrue(self.sup.bin.is_running())

    def test_settings_change_is_applied_by_an_explicit_restart(self):
        self._started()
        self.sup.settings["mode"] = "urltest"
        self.sup.restart()
        self.assertIn("restart", self._kinds())
        with open(self.sup.config_path) as f:
            cfg = json.load(f)
        self.assertTrue([o for o in cfg["outbounds"] if o["type"] == "urltest"])

    def test_restart_keeps_the_process_when_the_new_config_is_invalid(self):
        self._started()
        self.sup.bin.check = lambda cfg: (False, "bad config")
        self.sup.restart()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.sup.bin.is_running())

    # ----- subscription refresh scheduling --------------------------

    def test_tick_refreshes_due_subscriptions_once(self):
        self.sup.settings["subscription_interval_hours"] = 24
        refresh_calls = []
        self.sup.refresh_subscriptions = lambda now, interval: (
            refresh_calls.append((now, interval)) or True)
        self.clock.advance(100)
        self.sup.tick()
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(refresh_calls[0][1], 24)

    def test_refresh_in_flight_is_not_reentered(self):
        self.sup.settings["subscription_interval_hours"] = 24
        refresh_calls = []

        def refresher(now, interval):
            refresh_calls.append(1)
            self.sup.tick()  # re-entrant tick must not refresh again
            return False

        self.sup.refresh_subscriptions = refresher
        self.sup.tick()
        self.assertEqual(len(refresh_calls), 1)

    def test_refresh_during_watchdog_backoff_does_not_delay_restart(self):
        self.sup.settings["subscription_interval_hours"] = 24
        refresh_calls = []
        self.sup.refresh_subscriptions = lambda now, interval: (
            refresh_calls.append(1) or False)
        self._started()
        self.sup.bin.crash()
        self.sup.tick()  # arms the restart at now + 2s
        self.clock.advance(3)
        self.sup.tick()  # refresh due AND the pending restart fires
        self.assertGreaterEqual(len(refresh_calls), 1)
        self.assertIn("start", self._kinds())

    def test_refresh_removing_active_reconfigures_in_manual_mode(self):
        self.sup.settings["subscription_interval_hours"] = 24
        self._started()
        parsed, _ = parsers.parse_lines([TROJAN])
        self.sup.store.add_subscription_profiles(parsed, "sub-x")
        self.sup.store.set_active("AUTO:Trojan")
        self.sup._make_binary_manager = lambda: _FakeBin("new", self.calls)
        self.sup.refresh_subscriptions = lambda now, interval: (
            self.sup.store.remove_by_subscription("sub-x") or True)
        self.sup.tick()
        self.assertEqual(self.sup.store.active_tag, "AUTO:VLESS",
                         "active profile must be re-picked after removal")
        self.assertIn(("start", "new", self.sup.effective_port), self.calls,
                      "engine must restart through the config-write path")
        with open(self.sup.config_path) as f:
            cfg = json.load(f)
        sel = [o for o in cfg["outbounds"] if o["type"] == "selector"][0]
        self.assertEqual(sel["default"], "AUTO:VLESS",
                         "rebuilt config must reference the re-picked profile")

    def test_refresh_changing_profiles_during_backoff_does_not_start_early(self):
        self.sup.settings["subscription_interval_hours"] = 24
        self._started()
        self.sup.bin.crash()
        self.sup.tick()  # arms the restart at now + 2s
        self.calls[:] = []
        self.clock.advance(1)
        self.sup.refresh_subscriptions = lambda now, interval: True
        self.sup.tick()
        self.assertNotIn("start", self._kinds(),
                         "refresh must not preempt the pending backoff restart")
        self.assertIsNotNone(self.sup._restart_at)
        self.clock.advance(2)
        self.sup.tick()
        self.assertIn("start", self._kinds(),
                      "the watchdog restart still fires on schedule")

    def test_refresh_in_urltest_mode_reconfigures(self):
        self.sup.settings["mode"] = "urltest"
        self.sup.settings["subscription_interval_hours"] = 24
        self._started()
        reconfigured = []
        self.sup.reconfigure_engine = lambda: reconfigured.append(1) or True
        self.sup.refresh_subscriptions = lambda now, interval: True
        self.sup.tick()
        self.assertEqual(reconfigured, [1],
                         "profile-set change in urltest must reconfigure")


class _FakeProfileStore(object):
    """ProfileStore stand-in for SubscriptionStore cascade tests."""

    def __init__(self):
        self.profiles = []
        self.active_tag = None
        self.added = []
        self.removed = []

    @staticmethod
    def _identity(p):
        uri = p.get("uri")
        if uri:
            return uri
        return (p.get("protocol"), p.get("server"), p.get("port"))

    @staticmethod
    def _endpoint(p):
        return (p.get("protocol"), p.get("server"), p.get("port"))

    def tags(self):
        return [p["tag"] for p in self.profiles]

    def add_subscription_profiles(self, parsed, group_id):
        manual = [p for p in self.profiles if p.get("subscription") is None]
        manual_ids = {self._identity(p) for p in manual}
        manual_endpoints = {self._endpoint(p) for p in manual}
        group_ids = {self._identity(p) for p in self.profiles
                     if p.get("subscription") == group_id}
        added = 0
        for p in parsed:
            identity = self._identity(p)
            if identity in manual_ids or identity in group_ids or \
                    self._endpoint(p) in manual_endpoints:
                continue
            p["subscription"] = group_id
            self.profiles.append(p)
            self.added.append(p["tag"])
            group_ids.add(identity)
            added += 1
        return added

    def sync_subscription(self, parsed, group_id):
        """Mirror sync: add new profiles, remove disappeared ones."""
        current = [p for p in self.profiles
                   if p.get("subscription") == group_id]
        current_by_id = {self._identity(p): p for p in current}
        new_ids = {self._identity(p) for p in parsed}
        removed = [p["tag"] for identity, p in current_by_id.items()
                   if identity not in new_ids]
        added = []
        manual_endpoints = {self._endpoint(p) for p in self.profiles
                            if p.get("subscription") is None}
        for p in parsed:
            if self._identity(p) not in current_by_id:
                if self._endpoint(p) in manual_endpoints:
                    continue
                p["subscription"] = group_id
                self.profiles.append(p)
                added.append(p["tag"])
        self.profiles = [p for p in self.profiles
                         if not (p.get("subscription") == group_id
                                 and p["tag"] in removed)]
        self.removed = removed
        self.added = added
        if self.active_tag not in [p["tag"] for p in self.profiles]:
            self.active_tag = (self.profiles[0]["tag"] if self.profiles
                               else None)
        return added, removed

    def remove_by_subscription(self, group_id):
        kept = [p for p in self.profiles if p.get("subscription") != group_id]
        self.removed = [p["tag"] for p in self.profiles
                        if p.get("subscription") == group_id]
        self.profiles = kept
        if self.active_tag not in [p["tag"] for p in kept]:
            self.active_tag = kept[0]["tag"] if kept else None
        return self.removed


class TestSubscriptionDecode(unittest.TestCase):
    """decode_subscription: plain text, base64, JSON, fallback order."""

    def setUp(self):
        import subscriptions  # noqa: E402
        self.subscriptions = subscriptions

    def test_plain_text_with_one_vless_line_decodes(self):
        profs, skipped = self.subscriptions.decode_subscription(VLESS.encode())
        self.assertEqual(skipped, [])
        self.assertEqual([p["protocol"] for p in profs], ["vless"])
        self.assertEqual(profs[0]["tag"], "AUTO:VLESS")
        self.assertEqual(profs[0]["uri"], VLESS)

    def test_standard_base64_decodes(self):
        import base64
        body = base64.b64encode((VLESS + "\n" + HY2).encode())
        profs, skipped = self.subscriptions.decode_subscription(body)
        self.assertEqual(skipped, [])
        self.assertEqual([p["protocol"] for p in profs],
                         ["vless", "hysteria2"])

    def test_urlsafe_base64_with_newlines_and_no_padding_decodes(self):
        import base64
        body = base64.urlsafe_b64encode((VLESS + "\n" + TROJAN).encode())
        body = body.rstrip(b"=")  # strip padding, URL-safe style
        profs, skipped = self.subscriptions.decode_subscription(body)
        self.assertEqual(skipped, [])
        self.assertEqual([p["protocol"] for p in profs],
                         ["vless", "trojan"])

    def test_text_body_with_profile_lines_is_used_as_is(self):
        profs, skipped = self.subscriptions.decode_subscription(
            (VLESS + "\n").encode())
        self.assertEqual([p["protocol"] for p in profs], ["vless"])

    def test_text_and_base64_without_links_is_an_error(self):
        import base64
        inner = base64.b64encode(b"not-a-profile-line").decode()
        profs, skipped = self.subscriptions.decode_subscription(inner.encode())
        self.assertEqual(profs, [])
        self.assertEqual(skipped, [])

    def test_garbage_does_not_decode(self):
        profs, skipped = self.subscriptions.decode_subscription(
            b"\x00\xff\xfe not a sub")
        self.assertEqual(profs, [])
        self.assertEqual(skipped, [])

    def test_zero_profile_lines_is_empty(self):
        profs, skipped = self.subscriptions.decode_subscription(
            b"just some text, no links")
        self.assertEqual(profs, [])
        self.assertEqual(skipped, [])


class TestSubscriptionStore(unittest.TestCase):
    """SubscriptionStore: add/remove/refresh/due with injectable deps."""

    def setUp(self):
        import subscriptions  # noqa: E402
        self.subscriptions = subscriptions
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "subscriptions.json")
        self.store = self.subscriptions.SubscriptionStore(
            self.path, now=lambda: 1000.0)
        self.pstore = _FakeProfileStore()

    def test_add_records_group_and_adds_profiles(self):
        group, err = self.store.add(
            "https://example.com/sub", fetcher=lambda url: (VLESS + "\n" + HY2).encode(),
            profile_store=self.pstore)
        self.assertIsNone(err)
        self.assertIsNotNone(group["id"])
        self.assertEqual(group["url"], "https://example.com/sub")
        self.assertEqual(self.pstore.added,
                         [parsers.parse_uri(VLESS)["tag"],
                          parsers.parse_uri(HY2)["tag"]])
        groups = self.store.groups()
        self.assertEqual(len(groups), 1)

    def test_remove_cascades_to_profile_store(self):
        self.store.add("https://example.com/sub",
                       fetcher=lambda url: VLESS.encode(),
                       profile_store=self.pstore)
        gid = self.store.groups()[0]["id"]
        self.store.remove(gid, self.pstore)
        self.assertEqual(self.pstore.removed,
                         [parsers.parse_uri(VLESS)["tag"]])
        self.assertEqual(self.store.groups(), [])

    def test_refresh_mirror_sync_adds_and_removes(self):
        group, _ = self.store.add("https://example.com/sub",
                                  fetcher=lambda url: (VLESS + "\n" + HY2).encode(),
                                  profile_store=self.pstore)
        gid = group["id"]
        # Second fetch drops HY2, adds TROJAN
        added, removed, err = self.store.refresh(
            gid,
            fetch=lambda url: (VLESS + "\n" + TROJAN).encode(),
            profile_store=self.pstore)
        self.assertIsNone(err)
        self.assertEqual(removed,
                         [parsers.parse_uri(HY2)["tag"]])
        self.assertIn(parsers.parse_uri(TROJAN)["tag"], added)

    def test_refresh_failure_leaves_profiles_untouched(self):
        group, _ = self.store.add("https://example.com/sub",
                                  fetcher=lambda url: VLESS.encode(),
                                  profile_store=self.pstore)
        gid = group["id"]
        def boom(url):
            raise IOError("network down")
        added, removed, err = self.store.refresh(gid, fetch=boom,
                                                 profile_store=self.pstore)
        self.assertIsNotNone(err)
        self.assertEqual(added, [])
        self.assertEqual(removed, [])
        self.assertIsNotNone(self.store.get(gid)["last_error"])

    def test_due_respects_interval_and_never(self):
        now = 1000.0
        self.store.add("https://a.example/sub",
                       fetcher=lambda url: VLESS.encode(),
                       profile_store=self.pstore)
        gid = self.store.groups()[0]["id"]
        # last_updated = 1000 (injected clock), not due until 24h pass
        self.assertEqual(self.store.due(now, 24), [])
        # interval 0 = never
        self.assertEqual(self.store.due(now, 0), [])
        # after advancing past N hours, due
        self.assertEqual(self.store.due(now + 24 * 3600 + 1, 24),
                         [self.store.get(gid)])

    def _json_body(self, outbounds, remarks=None):
        cfg = {"outbounds": outbounds}
        if remarks:
            cfg["remarks"] = remarks
        return json.dumps(cfg).encode()

    def test_add_json_config_body_works_through_store(self):
        body = self._json_body([
            {"type": "vless", "tag": "j-vless", "server": "h1",
             "server_port": 443, "uuid": "u-1"},
            {"type": "hysteria2", "tag": "j-hy2", "server": "h2",
             "server_port": 8443, "password": "pw"},
        ])
        group, err = self.store.add("https://example.com/json",
                                    fetcher=lambda url: body,
                                    profile_store=self.pstore)
        self.assertIsNone(err)
        self.assertEqual(self.pstore.added,
                         ["j-vless", "j-hy2"])
        self.assertTrue(all(p.get("uri") is None
                            for p in self.pstore.profiles))

    def test_refresh_json_mirror_sync_and_empty_guard(self):
        body1 = self._json_body([
            {"type": "vless", "tag": "j1", "server": "h1",
             "server_port": 443, "uuid": "u-1"},
            {"type": "hysteria2", "tag": "j2", "server": "h2",
             "server_port": 8443, "password": "pw"},
        ])
        group, err = self.store.add("https://example.com/json",
                                    fetcher=lambda url: body1,
                                    profile_store=self.pstore)
        self.assertIsNone(err)
        gid = group["id"]
        # refresh with a changed body: drop j2, add j3
        body2 = self._json_body([
            {"type": "vless", "tag": "j1", "server": "h1",
             "server_port": 443, "uuid": "u-1"},
            {"type": "trojan", "tag": "j3", "server": "h3",
             "server_port": 443, "password": "pw"},
        ])
        added, removed, err = self.store.refresh(gid, fetch=lambda url: body2,
                                                 profile_store=self.pstore)
        self.assertIsNone(err)
        self.assertEqual(removed, ["j2"])
        self.assertIn("j3", added)
        self.assertEqual(sorted(p["tag"] for p in self.pstore.profiles),
                         ["j1", "j3"])
        # empty body must NOT wipe the group
        added, removed, err = self.store.refresh(
            gid, fetch=lambda url: b"no usable profiles here",
            profile_store=self.pstore)
        self.assertIsNotNone(err)
        self.assertEqual(removed, [])
        self.assertEqual(sorted(p["tag"] for p in self.pstore.profiles),
                         ["j1", "j3"])

    def test_cascade_delete_json_group(self):
        body = self._json_body([
            {"type": "vless", "tag": "j1", "server": "h1",
             "server_port": 443, "uuid": "u-1"},
        ])
        group, err = self.store.add("https://example.com/json",
                                    fetcher=lambda url: body,
                                    profile_store=self.pstore)
        self.assertIsNone(err)
        self.store.remove(group["id"], self.pstore)
        self.assertEqual(self.pstore.removed, ["j1"])
        self.assertEqual(self.store.groups(), [])


class TestEngineVersionContract(unittest.TestCase):
    """build.sh and binary_manager.py must pin the same latest engine
    versions; check_versions.sh treats drift between them as fatal."""

    EXPECTED_SINGBOX = "1.13.15"
    EXPECTED_XRAY = "26.7.28"

    def test_build_sh_pins_latest_singbox(self):
        with open(os.path.join(HERE, "..", "build.sh")) as f:
            src = f.read()
        self.assertIn('SINGBOX_VERSION="%s"' % self.EXPECTED_SINGBOX, src)

    def test_build_sh_pins_latest_xray(self):
        with open(os.path.join(HERE, "..", "build.sh")) as f:
            src = f.read()
        self.assertIn('XRAY_VERSION="%s"' % self.EXPECTED_XRAY, src)

    def test_binary_manager_pins_latest_singbox(self):
        with open(os.path.join(SRC, "binary_manager.py")) as f:
            src = f.read()
        self.assertIn('SINGBOX_VERSION = "%s"' % self.EXPECTED_SINGBOX, src)

    def test_binary_manager_pins_latest_xray(self):
        with open(os.path.join(SRC, "binary_manager.py")) as f:
            src = f.read()
        self.assertIn('XRAY_VERSION = "%s"' % self.EXPECTED_XRAY, src)


class TestParseConfig(unittest.TestCase):
    """parse_config: JSON sing-box/Xray configs -> neutral profiles."""

    def _singbox(self, outbounds, remarks=None):
        cfg = {"outbounds": outbounds}
        if remarks:
            cfg["remarks"] = remarks
        return cfg

    def _sb_vless(self, tag="sb-vless"):
        return {"type": "vless", "tag": tag, "server": "h1.example",
                "server_port": 443, "uuid": "u-1", "network": "tcp",
                "tls": {"enabled": True, "server_name": "sni1"}}

    def _sb_h2(self, tag="sb-hy2"):
        return {"type": "hysteria2", "tag": tag, "server": "h2.example",
                "server_port": 8443, "password": "pw",
                "tls": {"enabled": True, "server_name": "h2.example"}}

    def _sb_trojan(self, tag="sb-trojan"):
        return {"type": "trojan", "tag": tag, "server": "h3.example",
                "server_port": 443, "password": "pw"}

    def test_singbox_config_extracts_proxies_and_skips_non_proxy(self):
        profs, skipped = parsers.parse_config(self._singbox([
            self._sb_vless(), self._sb_h2(), self._sb_trojan(),
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
            {"type": "selector", "tag": "proxy", "outbounds": ["sb-vless"]},
        ]))
        self.assertEqual([p["protocol"] for p in profs],
                         ["vless", "hysteria2", "trojan"])
        self.assertEqual([p["tag"] for p in profs],
                         ["sb-vless", "sb-hy2", "sb-trojan"])
        self.assertEqual(len(skipped), 3)

    def test_singbox_extra_protocols(self):
        profs, _ = parsers.parse_config(self._singbox([
            {"type": "vmess", "tag": "v", "server": "h", "server_port": 80,
             "uuid": "u"},
            {"type": "shadowsocks", "tag": "s", "server": "h", "server_port": 1,
             "method": "aes-256-gcm", "password": "p"},
            {"type": "wireguard", "tag": "w", "server": "h", "server_port": 2,
             "local_address": "10.0.0.2/32", "private_key": "k"},
            {"type": "tuic", "tag": "t", "server": "h", "server_port": 3,
             "uuid": "u", "password": "p"},
            {"type": "socks", "tag": "x", "server": "h", "server_port": 4},
            {"type": "http", "tag": "y", "server": "h", "server_port": 5},
        ]))
        self.assertEqual([p["protocol"] for p in profs],
                         ["vmess", "shadowsocks", "wireguard", "tuic",
                          "socks", "http"])

    def test_xray_config_extracts_proxies(self):
        cfg = {"outbounds": [
            {"tag": "x-vless", "protocol": "vless",
             "settings": {"vnext": [{"address": "h1", "port": 443,
                                     "users": [{"id": "u-1"}]}]}},
            {"tag": "x-hy2", "protocol": "hysteria",
             "settings": {"address": "h2", "port": 8443},
             "streamSettings": {"network": "hysteria",
                                "security": "tls",
                                "tlsSettings": {"serverName": "h2"},
                                "hysteriaSettings": {"auth": "x-hy2-auth"}}},
            {"tag": "direct", "protocol": "freedom"},
        ]}
        profs, skipped = parsers.parse_config(cfg)
        self.assertEqual([p["protocol"] for p in profs],
                         ["vless", "hysteria2"])
        self.assertEqual(profs[1]["password"], "x-hy2-auth")
        self.assertEqual(len(skipped), 1)

    def test_xray_skips_tuic(self):
        cfg = {"outbounds": [
            {"tag": "t", "protocol": "tuic",
             "settings": {"address": "h", "port": 443}},
        ]}
        profs, skipped = parsers.parse_config(cfg)
        self.assertEqual(profs, [])
        self.assertTrue(any("tuic" in reason for _, reason in skipped))

    def test_array_of_configs_uses_remarks_fallback(self):
        docs = [self._singbox([self._sb_vless(tag="o1")], remarks="Location A"),
                self._singbox([self._sb_h2(tag="o2")], remarks="Location B")]
        profs, _ = parsers.parse_config(docs)
        self.assertEqual([p["tag"] for p in profs], ["o1", "o2"])

    def test_array_element_without_tag_uses_remarks(self):
        docs = [self._singbox([self._sb_vless(tag="")], remarks="Loc A")]
        profs, _ = parsers.parse_config(docs)
        self.assertEqual(profs[0]["tag"], "Loc A")

    def test_bigping_shape_extracts_three_proxies(self):
        docs = [
            {"remarks": "🇷🇺 Россия",
             "outbounds": [self._sb_vless(), self._sb_h2(), self._sb_trojan(),
                           {"type": "block", "tag": "block"}]},
            {"remarks": "🇫🇮 Финляндия",
             "outbounds": [self._sb_vless(tag="fi-vless"),
                           {"type": "block", "tag": "block"}]},
        ]
        profs, skipped = parsers.parse_config(docs)
        self.assertEqual([p["protocol"] for p in profs],
                         ["vless", "hysteria2", "trojan", "vless"])
        self.assertEqual(len(skipped), 2)

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            parsers.parse_config("{not json")

    def test_config_without_proxy_outbounds(self):
        profs, skipped = parsers.parse_config(self._singbox([
            {"type": "direct", "tag": "d"},
            {"type": "block", "tag": "b"},
        ]))
        self.assertEqual(profs, [])
        self.assertEqual(len(skipped), 2)

    def test_decode_subscription_json_branch(self):
        import subscriptions
        body = json.dumps([self._singbox([self._sb_vless(), self._sb_h2()])])
        profs, skipped = subscriptions.decode_subscription(body.encode())
        self.assertEqual([p["protocol"] for p in profs], ["vless", "hysteria2"])
        self.assertEqual(skipped, [])

    def test_decode_subscription_json_base64(self):
        import base64
        import subscriptions
        body = json.dumps(self._singbox([self._sb_vless()]))
        wrapped = base64.b64encode(body.encode())
        profs, skipped = subscriptions.decode_subscription(wrapped)
        self.assertEqual([p["protocol"] for p in profs], ["vless"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
