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
import shutil
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
import dns_utils  # noqa: E402
import health  # noqa: E402
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

    def test_parse_lines(self):
        profs, skipped = parsers.parse_lines([VLESS, HY2, "garbage", TROJAN])
        self.assertEqual(len(profs), 3)
        self.assertEqual(len(skipped), 1)

    def test_is_subscription_url_https(self):
        self.assertTrue(parsers.is_subscription_url("https://example.com/sub"))

    def test_is_subscription_url_false_for_profile(self):
        self.assertFalse(parsers.is_subscription_url(VLESS))

    def test_vmess_modern_form(self):
        p = parsers.parse_uri(
            "vmess://uuid-1111@h.example:443?security=auto&type=tcp#VM:T1")
        self.assertEqual(p["protocol"], "vmess")
        self.assertEqual(p["tag"], "VM:T1")
        self.assertEqual(p["server"], "h.example")
        self.assertEqual(p["port"], 443)
        self.assertEqual(p["uuid"], "uuid-1111")

    def test_shadowsocks_plain_form(self):
        p = parsers.parse_uri("ss://chacha20-ietf-poly1305:pass@h.example:8388#SS:1")
        self.assertEqual(p["protocol"], "shadowsocks")
        self.assertEqual(p["tag"], "SS:1")
        self.assertEqual(p["method"], "chacha20-ietf-poly1305")
        self.assertEqual(p["password"], "pass")
        self.assertEqual(p["port"], 8388)

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

    def test_manual_interrupt_connections_false(self):
        profs, _ = parsers.parse_lines([VLESS, HY2])
        cfg, _ = build_singbox.build_config(profs, self._settings(mode="manual", interrupt_connections=False),
                                            active_tag="AUTO:Hysteria2")
        sel = [o for o in cfg["outbounds"] if o["type"] == "selector"][0]
        self.assertEqual(sel["interrupt_exist_connections"], False)

    def test_direct_mode_forces_direct_even_with_profiles(self):
        profs, _ = parsers.parse_lines([VLESS, HY2])
        cfg, _ = build_singbox.build_config(profs, self._settings(mode="direct"))
        self.assertEqual(cfg["route"]["final"], "direct")
        types = [o["type"] for o in cfg["outbounds"]]
        self.assertNotIn("urltest", types)
        self.assertNotIn("selector", types)
        self.assertEqual(types, ["direct"])


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

    def test_direct_mode_forces_direct_even_with_profiles(self):
        profs, _ = parsers.parse_lines([VLESS, TROJAN])
        cfg, _ = build_xray.build_config(profs, self._settings(mode="direct"),
                                         active_tag="AUTO:Trojan")
        self.assertEqual(cfg["routing"]["final"], "direct")
        self.assertNotIn("balancers", cfg["routing"])
        self.assertNotIn("burstObservatory", cfg)
        self.assertFalse([r for r in cfg["routing"]["rules"]
                          if r.get("balancerTag") or
                          r.get("outboundTag") == "AUTO:Trojan"])

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
        # Xray cannot multiplex SOCKS and HTTP on one port: SOCKS moves to
        # local_port + 1, HTTP keeps the configured port.
        self.assertEqual(socks["port"], 1081)

    def test_http_inbound_serves_kodi_http_proxy(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(profs, self._settings())
        http = [i for i in cfg["inbounds"] if i["protocol"] == "http"]
        self.assertEqual(len(http), 1)
        self.assertEqual(http[0]["port"], 1080)
        self.assertEqual(http[0]["listen"], "127.0.0.1")

    def _xray_outbound(self, prof, protocol):
        cfg, skipped = build_xray.build_config([prof], self._settings())
        self.assertEqual(skipped, [])
        return [o for o in cfg["outbounds"] if o["protocol"] == protocol][0]

    def test_skips_tuic(self):
        prof = parsers.parse_uri("tuic://uuid-3@h:443?password=pw#TU:1")
        outbounds, tags, skipped = build_xray.build_outbounds([prof])
        self.assertEqual(outbounds, [])
        self.assertEqual(tags, [])
        self.assertTrue(any("tuic" in reason for _, reason in skipped))
        cfg, _ = build_xray.build_config([prof], self._settings())
        self.assertEqual(cfg["routing"]["final"], "direct",
                         "with no usable profiles the config must route direct")

    def test_empty_profiles_route_direct(self):
        for builder in (build_xray.build_config, build_singbox.build_config):
            cfg, skipped = builder([], self._settings())
            if builder is build_xray.build_config:
                self.assertEqual(cfg["routing"]["final"], "direct")
            else:
                self.assertEqual(cfg["route"]["final"], "direct")
                self.assertEqual([o["type"] for o in cfg["outbounds"]],
                                 ["direct"])

    # ----- DNS -------------------------------------------------------

    def test_dns_udp_server(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(
            profs, self._settings(dns_server="8.8.8.8",
                                  dns_bootstrap=["192.168.1.1"]))
        addresses = [s["address"] for s in cfg["dns"]["servers"]]
        self.assertEqual(addresses[0], "8.8.8.8")
        self.assertIn("192.168.1.1", addresses)

    def test_dns_doh_via_proxy_routing(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(
            profs, self._settings(dns_server="https://1.1.1.1/dns-query",
                                  dns_bootstrap=["192.168.1.1"]))
        self.assertEqual(cfg["dns"]["servers"][0]["address"],
                         "https://1.1.1.1/dns-query")
        self.assertNotIn("hosts", cfg["dns"],
                         "IP-literal DoH needs no hosts pinning")

    def test_dns_doh_hostname_pinned_via_hosts(self):
        profs, _ = parsers.parse_lines([VLESS])
        with patch.object(dns_utils, "resolve_hostname",
                          return_value=["94.140.14.14"]) as resolve:
            cfg, _ = build_xray.build_config(
                profs, self._settings(
                    dns_server="https://dns.adguard-dns.com/dns-query",
                    dns_bootstrap=["192.168.1.1"]))
        resolve.assert_called_once_with("dns.adguard-dns.com")
        self.assertEqual(cfg["dns"]["hosts"]["dns.adguard-dns.com"],
                         ["94.140.14.14"])

    def test_dns_dot(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(
            profs, self._settings(dns_server="tls://1.1.1.1",
                                  dns_bootstrap=["192.168.1.1"]))
        self.assertEqual(cfg["dns"]["servers"][0]["address"], "tls://1.1.1.1")

    def test_dns_default_is_doh_with_router_fallback(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(
            profs, self._settings(dns_bootstrap=["192.168.1.1"]))
        addresses = [s["address"] for s in cfg["dns"]["servers"]]
        self.assertEqual(addresses,
                         ["https://1.1.1.1/dns-query", "192.168.1.1"])

    def test_geoip_rule_absent_when_db_missing(self):
        # A rule referencing a missing geoip.dat makes Xray refuse the whole
        # config; the rule must not be emitted until the DB is downloaded.
        profs, _ = parsers.parse_lines([VLESS])
        with tempfile.TemporaryDirectory() as td:
            cfg, _ = build_xray.build_config(
                profs, self._settings(
                    geoip_url="https://a/geoip.dat",
                    geo_paths={"geoip": os.path.join(td, "geoip.dat"),
                               "geosite": os.path.join(td, "geosite.dat")}))
        self.assertFalse(any(r.get("ip") == ["geoip:ru-blocked"]
                             for r in cfg["routing"]["rules"]))

    def test_supervisor_geo_paths_injected_into_build_settings(self):
        with tempfile.TemporaryDirectory() as work:
            sup = supervisor.ProxySupervisor(
                settings={"local_port": 1080}, addon_dir=work, work_dir=work)
            sup.effective_port = 1080
            s = sup._build_settings()
            self.assertEqual(s["geo_paths"]["geoip"],
                             os.path.join(work, "geoip.dat"))
            self.assertEqual(s["geo_paths"]["geosite"],
                             os.path.join(work, "geosite.dat"))


class TestDnsUtils(unittest.TestCase):
    def test_parse_udp(self):
        self.assertEqual(dns_utils.parse_dns_server("8.8.8.8"),
                         {"kind": "udp", "host": "8.8.8.8", "port": 53})
        self.assertEqual(dns_utils.parse_dns_server("udp://1.1.1.1:5353"),
                         {"kind": "udp", "host": "1.1.1.1", "port": 5353})

    def test_parse_doh_keeps_path(self):
        parsed = dns_utils.parse_dns_server("https://dns.google/dns-query")
        self.assertEqual(parsed, {"kind": "doh", "host": "dns.google",
                                  "port": 443, "path": "/dns-query"})
        parsed = dns_utils.parse_dns_server("https://1.1.1.1")
        self.assertEqual(parsed["path"], "/dns-query")

    def test_parse_dot(self):
        self.assertEqual(dns_utils.parse_dns_server("tls://1.1.1.1"),
                         {"kind": "dot", "host": "1.1.1.1", "port": 853})
        self.assertEqual(dns_utils.parse_dns_server("tls://dns.quad9.net:8853"),
                         {"kind": "dot", "host": "dns.quad9.net", "port": 8853})

    def test_parse_rejects_garbage(self):
        for bad in ("", "  ", "example.com", "https://", "tls://",
                    "1.1.1.1:abc", "http://1.1.1.1/dns-query"):
            with self.subTest(bad=bad):
                self.assertIsNone(dns_utils.parse_dns_server(bad))

    def test_system_dns_servers_skips_loopback(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as f:
            f.write("# comment\nnameserver 127.0.0.53\n"
                    "nameserver 192.168.1.1\nnameserver fe80::1%eth0\n"
                    "nameserver 192.168.1.1\n; another comment\n"
                    "nameserver 10.0.0.1 ; trailing\n")
            path = f.name
        try:
            self.assertEqual(dns_utils.system_dns_servers(path),
                             ["192.168.1.1", "10.0.0.1"])
        finally:
            os.unlink(path)

    def test_system_dns_servers_missing_file(self):
        self.assertEqual(dns_utils.system_dns_servers("/nonexistent/x"), [])

    def test_presets(self):
        self.assertEqual(dns_utils.preset_server("cloudflare-doh"),
                         "https://1.1.1.1/dns-query")
        self.assertEqual(dns_utils.preset_server("auto"), "")
        self.assertEqual(dns_utils.preset_server("custom", "tls://x"),
                         "tls://x")
        self.assertEqual(dns_utils.preset_server("unknown", ""), "")
        self.assertEqual(dns_utils.preset_id_by_index(0), "auto")
        self.assertEqual(
            dns_utils.preset_id_by_index(len(dns_utils.DNS_PRESETS) - 1),
            "custom")
        self.assertEqual(dns_utils.preset_id_by_index("bogus"), "auto")
        for i, (name, _) in enumerate(dns_utils.DNS_PRESETS):
            self.assertEqual(dns_utils.preset_index_by_id(name), i)
            self.assertEqual(dns_utils.preset_id_by_index(i), name)

    def test_resolve_hostname_ipv4_passthrough(self):
        self.assertEqual(dns_utils.resolve_hostname("1.2.3.4"), ["1.2.3.4"])


class TestSingboxDns(unittest.TestCase):
    def _settings(self, **kw):
        s = {"local_port": 1080, "mode": "urltest", "urltest_interval": "3m",
             "urltest_tolerance": 50, "interrupt_connections": True,
             "test_url": "https://x/204", "log_level": "info",
             "dns_bootstrap": ["192.168.1.1"]}
        s.update(kw)
        return s

    def _dns(self, settings, profiles=None):
        if profiles is None:
            profiles, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_singbox.build_config(profiles, settings)
        return cfg

    def _by_tag(self, cfg, tag):
        return [s for s in cfg["dns"]["servers"] if s.get("tag") == tag][0]

    def test_bootstrap_and_local_use_router_dns(self):
        cfg = self._dns(self._settings())
        self.assertEqual(self._by_tag(cfg, "bootstrap")["server"],
                         "192.168.1.1")
        self.assertEqual(self._by_tag(cfg, "local")["server"], "192.168.1.1")
        self.assertEqual(cfg["route"]["default_domain_resolver"], "bootstrap")

    def test_bootstrap_fallback_without_resolv_conf(self):
        cfg = self._dns(self._settings(dns_bootstrap=[]))
        self.assertEqual(self._by_tag(cfg, "bootstrap")["server"],
                         "77.88.8.8")

    def test_default_remote_is_doh(self):
        cfg = self._dns(self._settings())
        remote = self._by_tag(cfg, "remote")
        self.assertEqual(remote["type"], "https")
        self.assertEqual(remote["server"], "1.1.1.1")
        self.assertEqual(remote["detour"], "proxy")
        self.assertEqual(cfg["dns"]["final"], "remote")

    def test_doh_detour_direct_in_direct_mode(self):
        cfg = self._dns(self._settings(mode="direct"))
        self.assertNotIn("detour", self._by_tag(cfg, "remote"),
                         "no proxy outbound -> implicit direct detour")

    def test_custom_doh_hostname_gets_domain_resolver(self):
        cfg = self._dns(self._settings(
            dns_server="https://dns.adguard-dns.com/dns-query"))
        remote = self._by_tag(cfg, "remote")
        self.assertEqual(remote["type"], "https")
        self.assertEqual(remote["server"], "dns.adguard-dns.com")
        self.assertEqual(remote["path"], "/dns-query")
        self.assertEqual(remote["domain_resolver"], "bootstrap")
        self.assertEqual(remote["detour"], "proxy")

    def test_custom_dot(self):
        cfg = self._dns(self._settings(dns_server="tls://dns.quad9.net"))
        remote = self._by_tag(cfg, "remote")
        self.assertEqual(remote["type"], "tls")
        self.assertEqual(remote["server"], "dns.quad9.net")
        self.assertEqual(remote["domain_resolver"], "bootstrap")

    def test_custom_udp(self):
        cfg = self._dns(self._settings(dns_server="9.9.9.9"))
        remote = self._by_tag(cfg, "remote")
        self.assertEqual(remote["type"], "udp")
        self.assertEqual(remote["server"], "9.9.9.9")
        self.assertNotIn("detour", remote)

    def test_duckdns_rule_stays_local(self):
        cfg = self._dns(self._settings())
        self.assertEqual(cfg["dns"]["rules"],
                         [{"domain_suffix": [".duckdns.org"],
                           "server": "local"}])


class TestHelpersDns(unittest.TestCase):
    def test_preset_resolves_server(self):
        raw = {"dns_preset": "1"}  # cloudflare-doh
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["dns_server"], "https://1.1.1.1/dns-query")
        self.assertEqual(s["dns_preset"], "cloudflare-doh")

    def test_auto_preset_clears_server(self):
        raw = {"dns_preset": "0", "dns_server": "8.8.8.8"}
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["dns_server"], "")

    def test_custom_preset_keeps_freeform_server(self):
        raw = {"dns_preset": str(len(dns_utils.DNS_PRESETS) - 1),
               "dns_server": "tls://my-dns.example"}
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["dns_server"], "tls://my-dns.example")

    def test_legacy_dns_server_without_preset_is_custom(self):
        raw = {"dns_server": "8.8.8.8"}  # predates dns_preset
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["dns_preset"], "custom")
        self.assertEqual(s["dns_server"], "8.8.8.8")

    def test_untouched_preset_with_custom_server_stays_custom(self):
        # Kodi returns "" or the default "0" for an unset setting depending
        # on the reader; _read_kodi_settings marks untouched as "". Either
        # way a hand-entered server must not be discarded.
        raw = {"dns_preset": "", "dns_server": "8.8.8.8"}
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["dns_preset"], "custom")
        self.assertEqual(s["dns_server"], "8.8.8.8")

    def test_explicit_auto_preset_wins_over_stale_server(self):
        raw = {"dns_preset": "0", "dns_server": "8.8.8.8"}
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["dns_preset"], "auto")
        self.assertEqual(s["dns_server"], "")

    def test_parse_dns_server_delegates(self):
        self.assertEqual(helpers.parse_dns_server("tls://1.1.1.1"),
                         {"kind": "dot", "host": "1.1.1.1", "port": 853})


class TestDirectoryGrouping(unittest.TestCase):
    """Subscription groups render as header + their profiles."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = profiles.ProfileStore(os.path.join(self.tmp, "p.json"))

    def _build(self, subs):
        return helpers.build_directory_entries(
            self.store, "urltest", "plugin://service.advancedproxy/",
            subscriptions=subs)

    def _kinds(self, entries):
        return [e["kind"] for e in entries]

    def test_headers_precede_their_profiles(self):
        self.store.add_subscription_profiles(
            parsers.parse_lines([VLESS, HY2])[0], "sub-aaa")
        self.store.add_subscription_profiles(
            parsers.parse_lines([TROJAN])[0], "sub-bbb")
        subs = [{"id": "sub-aaa", "url": "https://a/sub", "last_updated": 1},
                {"id": "sub-bbb", "url": "https://b/sub", "last_updated": 1}]
        entries = self._build(subs)
        tags = [e.get("tag", e.get("id")) for e in entries
                if e["kind"] in ("profile", "subscription")]
        self.assertEqual(tags, ["sub-aaa", "AUTO:VLESS", "AUTO:Hysteria2",
                                "sub-bbb", "AUTO:Trojan"])

    def test_ungrouped_profiles_come_after_groups(self):
        self.store.add_uri(VLESS)  # manual, no subscription
        self.store.add_subscription_profiles(
            parsers.parse_lines([TROJAN])[0], "sub-bbb")
        subs = [{"id": "sub-bbb", "url": "https://b/sub", "last_updated": 1}]
        entries = self._build(subs)
        tags = [e.get("tag", e.get("id")) for e in entries
                if e["kind"] in ("profile", "subscription")]
        self.assertEqual(tags, ["sub-bbb", "AUTO:Trojan", "AUTO:VLESS"])
        grouped = {e["tag"]: e["grouped"] for e in entries
                   if e["kind"] == "profile"}
        self.assertEqual(grouped, {"AUTO:Trojan": True, "AUTO:VLESS": False})

    def test_header_click_refreshes_and_counts_profiles(self):
        self.store.add_subscription_profiles(
            parsers.parse_lines([VLESS, HY2])[0], "sub-aaa")
        subs = [{"id": "sub-aaa", "url": "https://a/sub", "last_updated": 1}]
        entries = self._build(subs)
        header = [e for e in entries if e["kind"] == "subscription"][0]
        self.assertEqual(header["count"], 2)
        self.assertIn("action=sub_refresh", header["click_url"])
        self.assertIn("id=sub-aaa", header["click_url"])

    def test_empty_subscription_still_shows_header(self):
        subs = [{"id": "sub-aaa", "url": "https://a/sub",
                 "last_error": "boom"}]
        entries = self._build(subs)
        header = [e for e in entries if e["kind"] == "subscription"][0]
        self.assertEqual(header["status"], "error: boom")
        self.assertEqual(header["count"], 0)


class _FakeGroupControl(object):
    """In-memory urltest group: `working` holds outbound tags that pass."""

    def __init__(self, members, working=()):
        self._members = list(members)
        self.working = set(working)
        self.now = self._members[0] if self._members else None
        self.selections = []

    def current(self):
        return self.now

    def effective(self):
        return self.now

    def members(self):
        return list(self._members)

    def select(self, tag):
        self.now = tag
        self.selections.append(tag)
        return True


class TestHealthMonitor(unittest.TestCase):
    def _monitor(self, control=None, working=True, **kw):
        notes = []
        logs = []
        args = dict(
            port=1080, test_url="https://x/204", control=control,
            notify=lambda msg, error=False: notes.append((msg, error)),
            logger=lambda msg, level="info": logs.append((level, msg)),
            interval=30, fail_threshold=2, sleeper=lambda s: None)
        args.update(kw)
        mon = health.HealthMonitor(**args)
        ctl = control

        def fetch(url, port):
            if ctl is not None and hasattr(ctl, "now"):
                return ctl.now in ctl.working
            return working

        mon.fetch = fetch
        return mon, notes

    def test_healthy_no_notifications(self):
        mon, notes = self._monitor()
        self.assertTrue(mon.check())
        self.assertEqual(notes, [])
        self.assertFalse(mon._down)

    def test_single_failure_below_threshold_stays_quiet(self):
        mon, notes = self._monitor(working=False)
        self.assertFalse(mon.check())
        self.assertEqual(notes, [])
        self.assertFalse(mon._down)

    def test_sustained_failure_notifies_outage_and_failover(self):
        ctl = _FakeGroupControl(["A", "B", "C"], working={"B"})
        mon, notes = self._monitor(control=ctl)
        mon.check()
        mon.check()  # threshold reached -> failover walks A(dead) B(alive)
        self.assertEqual(ctl.now, "B")
        self.assertIn(("No internet via proxy, switching...", True), notes)
        self.assertIn(("Switched: A -> B", False), notes)
        self.assertFalse(mon._down)

    def test_all_dead_restores_original_and_reports(self):
        ctl = _FakeGroupControl(["A", "B"], working=set())
        mon, notes = self._monitor(control=ctl)
        mon.check()
        mon.check()
        self.assertEqual(ctl.now, "A", "original selection must be restored")
        self.assertIn(("All proxy servers unreachable", True), notes)
        self.assertTrue(mon._down)

    def test_recovery_notifies(self):
        ctl = _FakeGroupControl(["A"], working=set())
        mon, notes = self._monitor(control=ctl)
        mon.check()
        mon.check()
        self.assertTrue(mon._down)
        ctl.working.add("A")
        mon.check()
        self.assertIn(("Proxy connectivity restored", False), notes)
        self.assertFalse(mon._down)

    def test_engine_side_switch_is_reported(self):
        ctl = _FakeGroupControl(["A", "B"], working={"A", "B"})
        mon, notes = self._monitor(control=ctl)
        mon.check()  # learns current = A
        ctl.now = "B"  # engine urltest switched on its own
        mon.check()
        self.assertIn(("Auto-switch: A -> B", False), notes)

    def test_restart_control_used_when_no_group(self):
        calls = []
        ctl = health.RestartControl(lambda: calls.append("restart"))
        mon, notes = self._monitor(control=ctl, working=False)
        mon.check()
        mon.check()
        self.assertEqual(calls, ["restart"])
        self.assertIn(("All proxy servers unreachable", True), notes)

    def test_tick_respects_interval(self):
        mon, _ = self._monitor()
        self.assertTrue(mon.tick(now=1000))
        self.assertIsNone(mon.tick(now=1010))
        self.assertTrue(mon.tick(now=1031))

    def test_fallback_urls_tried(self):
        seen = []
        mon, _ = self._monitor()
        mon.fetch = lambda url, port: seen.append(url) or \
            url == health.FALLBACK_URLS[0]
        self.assertTrue(mon.check())
        self.assertEqual(seen[0], "https://x/204")
        self.assertEqual(seen[1], health.FALLBACK_URLS[0])

    def test_auto_failover_disabled_keeps_selection(self):
        ctl = _FakeGroupControl(["A", "B"], working={"B"})
        mon, notes = self._monitor(control=ctl, auto_failover=False)
        mon.check()
        mon.check()
        self.assertEqual(ctl.now, "A")
        self.assertIn(("No internet via proxy, switching...", True), notes)


class TestClashGroupControl(unittest.TestCase):
    def _opener(self, payload, put_status=204):
        class _Resp(object):
            status = put_status

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        class _Opener(object):
            def __init__(self):
                self.requests = []

            def open(self, request, timeout=5):
                url = getattr(request, "full_url", request)
                self.requests.append(url)
                return _Resp()

        return _Opener()

    def test_current_and_members(self):
        opener = self._opener({"now": "B", "all": ["A", "B"]})
        ctl = health.ClashGroupControl(9091, opener=opener)
        self.assertEqual(ctl.current(), "B")
        self.assertEqual(ctl.members(), ["A", "B"])

    def test_select_puts_name(self):
        opener = self._opener({})
        ctl = health.ClashGroupControl(9091, opener=opener)
        self.assertTrue(ctl.select("B"))
        self.assertIn("/proxies/proxy", opener.requests[0])

    def test_api_down_returns_none_not_exception(self):
        class _Broken(object):
            def open(self, request, timeout=5):
                raise OSError("connection refused")

        ctl = health.ClashGroupControl(9091, opener=_Broken())
        self.assertIsNone(ctl.current())
        self.assertEqual(ctl.members(), [])
        self.assertFalse(ctl.select("A"))


class TestSupervisorHealthWiring(unittest.TestCase):
    def _supervisor(self, tmp, **settings):
        base = {"local_port": 1080, "mode": "urltest", "engine": "sing-box",
                "test_url": "https://x/204"}
        base.update(settings)
        return supervisor.ProxySupervisor(settings=base, addon_dir=tmp,
                                          work_dir=tmp)

    def test_monitor_created_for_urltest(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._supervisor(tmp)
            sup.effective_port = 1080
            sup.clash_port = 1180
            mon = sup._make_health_monitor()
            self.assertIsNotNone(mon)
            self.assertIsInstance(mon.control, health.ClashGroupControl)
            self.assertTrue(mon.auto_failover)

    def test_monitor_none_in_direct_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._supervisor(tmp, mode="direct")
            self.assertIsNone(sup._make_health_monitor())

    def test_monitor_none_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._supervisor(tmp, health_check=False)
            self.assertIsNone(sup._make_health_monitor())

    def test_xray_gets_restart_control_without_group_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._supervisor(tmp, engine="xray")
            sup.effective_port = 1080
            mon = sup._make_health_monitor()
            self.assertIsInstance(mon.control, health.RestartControl)
            self.assertTrue(mon.auto_failover)

    def test_manual_mode_no_auto_failover(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._supervisor(tmp, mode="manual")
            sup.effective_port = 1080
            sup.clash_port = 1180
            mon = sup._make_health_monitor()
            self.assertFalse(mon.auto_failover)

    def test_clash_port_reserved_for_singbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._supervisor(tmp)
            sup._resolve_effective_port()
            self.assertIsNotNone(sup.clash_port)
            self.assertGreater(sup.clash_port, sup.effective_port)
            s = sup._build_settings()
            self.assertEqual(s["clash_api_port"], sup.clash_port)

    def test_clash_api_block_in_config(self):
        profs, _ = parsers.parse_lines([VLESS])
        s = {"local_port": 1080, "mode": "urltest", "urltest_interval": "3m",
             "urltest_tolerance": 50, "test_url": "https://x/204",
             "log_level": "info", "clash_api_port": 1180,
             "dns_bootstrap": ["192.168.1.1"]}
        cfg, _ = build_singbox.build_config(profs, s)
        api = cfg["experimental"]["clash_api"]
        self.assertEqual(api["external_controller"], "127.0.0.1:1180")

    def test_no_clash_api_in_direct_mode(self):
        profs, _ = parsers.parse_lines([VLESS])
        s = {"local_port": 1080, "mode": "direct", "log_level": "info",
             "clash_api_port": 1180, "dns_bootstrap": ["192.168.1.1"]}
        cfg, _ = build_singbox.build_config(profs, s)
        self.assertNotIn("experimental", cfg)


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

    def test_direct_mode_normalization(self):
        s = helpers.get_settings(reader=lambda: {"mode": "2"})
        self.assertEqual(s["mode"], "direct")

    def test_probe_with_backoff_retries_then_succeeds(self):
        calls = {"n": 0}
        slept = []

        def prober(host, port, timeout):
            calls["n"] += 1
            return 42 if calls["n"] >= 3 else None

        ms = helpers.probe_with_backoff("h", 1, prober=prober,
                                        sleeper=slept.append)
        self.assertEqual(ms, 42)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(slept, [3, 6])

    def test_probe_with_backoff_gives_up_after_full_schedule(self):
        slept = []
        ms = helpers.probe_with_backoff(
            "h", 1, prober=lambda h, p, t: None, sleeper=slept.append)
        self.assertIsNone(ms)
        self.assertEqual(slept, [3, 6, 12, 24, 60])

    def test_sync_geo_databases_downloads_both(self):
        fetches = {}

        def fake_fetch(url):
            fetches[url] = True
            return b"geo-data"

        status = helpers.sync_geo_databases(
            {"geoip_url": "https://a/geoip.dat",
             "geosite_url": "https://b/geosite.dat"},
            fetch=fake_fetch)
        self.assertEqual(status["geoip"], "ok")
        self.assertEqual(status["geosite"], "ok")
        self.assertEqual(len(fetches), 2)

    def test_sync_geo_databases_empty_urls_skip(self):
        status = helpers.sync_geo_databases(
            {"geoip_url": "", "geosite_url": ""},
            fetch=lambda url: (_ for _ in ()).throw(
                AssertionError("must not fetch")))
        self.assertEqual(status["geoip"], "skipped")
        self.assertEqual(status["geosite"], "skipped")

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

    def test_xray_downloaded_geo_db_overrides_bundled(self):
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bin_dir = os.path.join(addon, "resources", "bin", "linux_x64")
            os.makedirs(bin_dir)
            with open(os.path.join(bin_dir, "xray"), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(os.path.join(bin_dir, "xray"), 0o755)
            with open(os.path.join(bin_dir, "geoip.dat"), "w") as f:
                f.write("bundled")
            with open(os.path.join(work, "geoip.dat"), "w") as f:
                f.write("downloaded-with-ru-blocked")
            bm = binary_manager.BinaryManager(
                addon, work, engine="xray", platform_override="linux_x64")
            bm.ensure_binary()
            with open(os.path.join(bm.work_dir_bin, "geoip.dat")) as f:
                self.assertEqual(f.read(), "downloaded-with-ru-blocked",
                                 "downloaded DB must win over bundled")

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

    def test_auto_activation_does_not_switch_to_manual(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        start = src.index("def _action_activate_reachable(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertNotIn('setSetting("mode", "1")', body,
                         "auto-mode activation must stay in auto, not "
                         "switch to manual")

    def test_subscription_click_defaults_to_refresh(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        start = src.index('elif kind == "subscription":')
        end = src.index("elif kind ==", start + 1)
        body = src[start:end]
        self.assertIn('e["click_url"]', body,
                      "subscription row must be clickable")
        self.assertIn("sub_refresh", src)

    def test_mode_toggle_cycles_three_modes(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        start = src.index("def _action_toggle_mode(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn('["0", "1", "2"]', body,
                      "mode toggle must cycle urltest/manual/direct")

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

    def test_default_add_accepts_subscription_urls(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        start = src.index("def _action_add(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn("_action_sub_add(handle, kb)", body,
                      "the Add dialog must route through sub_add, which "
                      "detects subscription URLs and profiles")
        sub_add_start = src.index("def _action_sub_add(")
        sub_add_end = src.index("\ndef ", sub_add_start + 1)
        sub_add_body = src[sub_add_start:sub_add_end]
        self.assertIn("is_subscription_url", sub_add_body,
                      "sub_add must detect subscription URLs")
        self.assertIn("sub_store.add", sub_add_body)

    def test_default_sub_add_reads_settings_url_when_param_missing(self):
        path = os.path.join(HERE, "..", "service.advancedproxy", "default.py")
        with open(path) as f:
            src = f.read()
        start = src.index("def _action_sub_add(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn('getSetting("subscription_url")', body,
                      "sub_add must fall back to the settings URL field")

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

    def test_failed_start_restores_stale_backup_and_never_ensures(self):
        manager = _FakeIntegrationManager(backup=True)
        _run_main([_settings()], manager, start_ok=False)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("restore",), ("loop.abort",),
                          ("sup.begin_shutdown",), ("sup.begin_shutdown",),
                          ("sup.stop",)])

    def test_no_profiles_starts_transparent_direct(self):
        manager = _FakeIntegrationManager(backup=True)
        _run_main([_settings()], manager, profiles_enabled=False)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("ensure", INTEGRATION_HOST, 1081),
                          ("loop.abort",),
                          ("sup.begin_shutdown",), ("sup.begin_shutdown",),
                          ("restore",), ("sup.stop",)])

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


class _FakeResponse(object):
    def __init__(self, body):
        self._body = body
        self._pos = 0

    def read(self, size=-1):
        if size < 0:
            size = len(self._body) - self._pos
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener(object):
    def __init__(self, open_impl):
        self.open_impl = open_impl
        self.handlers = [_FakeProxyHandler()]

    def open(self, request, timeout=10):
        return self.open_impl(self, request, timeout)


class _FakeProxyHandler(object):
    pass


class TestSubscriptionDecode(unittest.TestCase):
    """decode_subscription: plain text, base64, JSON, fallback order."""

    def setUp(self):
        import subscriptions  # noqa: E402
        self.subscriptions = subscriptions

    def test_fetch_ignores_kodi_proxy_environment(self):
        # Kodi exports its proxy settings as http_proxy/https_proxy for
        # add-ons; the local proxy may be on a fallback port or not up yet,
        # so subscription fetches must go out directly, not through it.
        import urllib.request
        captured = {}

        def fake_opener_open(opener, request, timeout=10):
            captured["handler"] = type(opener.handlers[0]).__name__
            captured["timeout"] = timeout
            return _FakeResponse(b"direct-bytes")

        with patch.object(urllib.request, "build_opener",
                          side_effect=lambda *a, **k: _FakeOpener(
                              fake_opener_open)):
            body = self.subscriptions.fetch("https://example.com/sub", timeout=7)
        self.assertEqual(body, b"direct-bytes")
        self.assertEqual(captured["handler"], "_FakeProxyHandler")
        self.assertEqual(captured["timeout"], 7)

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

    def test_add_same_url_twice_replaces_profiles_not_duplicates(self):
        body = (VLESS + "\n" + HY2).encode()
        self.store.add("https://example.com/sub", fetcher=lambda url: body,
                       profile_store=self.pstore)
        self.assertEqual(len(self.pstore.profiles), 2)
        self.assertEqual(len(self.pstore.added), 2)
        group, err = self.store.add(
            "https://example.com/sub", fetcher=lambda url: body,
            profile_store=self.pstore)
        self.assertIsNone(err)
        self.assertEqual(len(self.pstore.profiles), 2,
                         "re-adding the same URL must not duplicate profiles")
        self.assertEqual(self.pstore.removed,
                         [parsers.parse_uri(VLESS)["tag"],
                          parsers.parse_uri(HY2)["tag"]],
                         "the old group's profiles must be cascade-removed")
        self.assertEqual(len(self.store.groups()), 1)

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

    def _json_body(self, outbounds, remarks=None):
        cfg = {"outbounds": outbounds}
        if remarks:
            cfg["remarks"] = remarks
        return json.dumps(cfg).encode()

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

if __name__ == "__main__":
    unittest.main(verbosity=2)
