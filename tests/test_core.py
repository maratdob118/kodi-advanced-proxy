# -*- coding: utf-8 -*-
"""Unit tests for the Kodi-free core modules (dual-engine redesign).

Run:  python3 tests/test_core.py
No Kodi required; xbmc modules are never imported by the tested code.
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "service.advancedproxy", "src")
sys.path.insert(0, os.path.abspath(SRC))

import binary_manager  # noqa: E402
import build_singbox  # noqa: E402
import build_xray  # noqa: E402
import helpers  # noqa: E402
import osarch  # noqa: E402
import parsers  # noqa: E402
import profiles  # noqa: E402

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

    def test_skip_xhttp(self):
        profs, _ = parsers.parse_lines([VLESS, XHTTP])
        cfg, skipped = build_singbox.build_config(profs, self._settings())
        self.assertEqual(len(skipped), 1)
        tags = [o["tag"] for o in cfg["outbounds"]
                if o["type"] not in ("urltest", "direct")]
        self.assertNotIn("UAE:xHTTP", tags)


class TestBuildXray(unittest.TestCase):
    def _settings(self, **kw):
        s = {"local_port": 1080, "mode": "urltest", "urltest_interval": "3m",
             "test_url": "https://x/204", "log_level": "info"}
        s.update(kw)
        return s

    def test_skip_hysteria2(self):
        profs, _ = parsers.parse_lines([VLESS, HY2, TROJAN])
        cfg, skipped = build_xray.build_config(profs, self._settings())
        self.assertEqual(len(skipped), 1)
        self.assertIn("hysteria2", skipped[0][1])
        self.assertEqual(len(cfg["routing"]["balancers"][0]["selector"]), 2)

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

    def test_socks_inbound(self):
        profs, _ = parsers.parse_lines([VLESS])
        cfg, _ = build_xray.build_config(profs, self._settings())
        socks = [i for i in cfg["inbounds"] if i["protocol"] == "socks"][0]
        self.assertEqual(socks["port"], 1080)


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


class TestOsarch(unittest.TestCase):
    def test_supported(self):
        self.assertTrue(osarch.is_supported(osarch.get_platform()))

    def test_override(self):
        self.assertEqual(osarch.get_platform("linux_armv7"), "linux_armv7")


if __name__ == "__main__":
    unittest.main(verbosity=2)
