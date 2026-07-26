# -*- coding: utf-8 -*-
"""Unit tests for the Kodi-free core modules.

Run:  python3 -m pytest tests/ -v   (or: python3 tests/test_core.py)
No Kodi required. xbmc modules are never imported by the tested code paths.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "service.advancedproxy", "src")
sys.path.insert(0, os.path.abspath(SRC))

import binary_manager  # noqa: E402
import config_gen  # noqa: E402
import helpers  # noqa: E402
import osarch  # noqa: E402

SUB_URL = "https://bigping.duckdns.org/sub/__REDACTED__/urls"

VLESS_REALITY = ("vless://701b248a-248c-4457-b74e-7a376812a355@bigping.duckdns.org:443"
                 "?encryption=none&security=reality&flow=xtls-rprx-vision&sni=vkvideo.ru"
                 "&fp=chrome&pbk=PBK&sid=SID&type=tcp#AUTO:VLESS")
HY2 = "hy2://pass123@bigping.duckdns.org:443/?sni=bigping.duckdns.org#AUTO:Hysteria2"
TROJAN = "trojan://pass@bigping.duckdns.org:443?security=reality&sni=security.ubuntu.com#AUTO:Trojan"
VLESS_XHTTP = ("vless://uuid@bigping-uae.duckdns.org:443?security=tls&type=xhttp"
               "&path=/xhttp&sni=bigping-uae.duckdns.org#UAE:xHTTP")


class TestOsarch(unittest.TestCase):
    def test_current_platform_detected(self):
        p = osarch.get_platform()
        self.assertTrue(osarch.is_supported(p), "platform %s unsupported" % p)

    def test_override(self):
        self.assertEqual(osarch.get_platform("linux_armv7"), "linux_armv7")

    def test_asset_name(self):
        self.assertEqual(osarch.asset_name("linux_x64", "1.13.14"),
                         "sing-box-1.13.14-linux-amd64")

    def test_asset_url(self):
        url = osarch.asset_url("linux_armv7", "1.13.14")
        self.assertIn("sing-box-1.13.14-linux-armv7", url)
        self.assertTrue(url.endswith(".tar.gz"))

    def test_windows_binary_name(self):
        self.assertEqual(osarch.binary_filename("windows_x64"), "sing-box.exe")
        self.assertEqual(osarch.binary_filename("linux_x64"), "sing-box")


class TestConfigGenParse(unittest.TestCase):
    def test_vless_reality(self):
        obs, tags, skipped = config_gen.parse_lines([VLESS_REALITY], "")
        self.assertEqual(len(obs), 1)
        ob = obs[0]
        self.assertEqual(ob["type"], "vless")
        self.assertEqual(ob["tag"], "AUTO:VLESS")
        self.assertEqual(ob["uuid"], "701b248a-248c-4457-b74e-7a376812a355")
        self.assertEqual(ob["flow"], "xtls-rprx-vision")
        self.assertTrue(ob["tls"]["reality"]["enabled"])
        self.assertEqual(ob["tls"]["reality"]["public_key"], "PBK")
        self.assertEqual(ob["tls"]["server_name"], "vkvideo.ru")

    def test_hysteria2(self):
        obs, _, _ = config_gen.parse_lines([HY2], "")
        self.assertEqual(obs[0]["type"], "hysteria2")
        self.assertEqual(obs[0]["password"], "pass123")

    def test_skip_trojan(self):
        obs, tags, skipped = config_gen.parse_lines([TROJAN], "trojan")
        self.assertEqual(len(obs), 0)
        self.assertEqual(skipped[0][1], "protocol:trojan")

    def test_skip_xhttp_transport(self):
        obs, _, skipped = config_gen.parse_lines([VLESS_XHTTP], "xhttp")
        self.assertEqual(len(obs), 0)
        self.assertEqual(skipped[0][1], "transport:xhttp")

    def test_comma_separated_skip(self):
        obs, _, skipped = config_gen.parse_lines(
            [VLESS_REALITY, HY2, TROJAN, VLESS_XHTTP], "trojan,xhttp")
        types = sorted(o["type"] for o in obs)
        self.assertEqual(types, ["hysteria2", "vless"])
        self.assertEqual(len(skipped), 2)

    def test_urlencoded_hy2_password(self):
        line = "hy2://abc%2Bdef%2F@host:443/?sni=host#T:Hy2"
        obs, _, _ = config_gen.parse_lines([line], "")
        self.assertEqual(obs[0]["password"], "abc+def/")


class TestConfigGenBuild(unittest.TestCase):
    def _settings(self, **kw):
        s = {
            "local_port": 1080,
            "lan_mixed_enabled": False,
            "lan_mixed_port": 1080,
            "skip_protocols": "",
            "urltest_interval": "3m",
            "urltest_tolerance": 50,
            "interrupt_connections": True,
            "test_url": "https://www.gstatic.com/generate_204",
            "log_level": "info",
        }
        s.update(kw)
        return s

    def test_build_structure(self):
        obs, tags, _ = config_gen.parse_lines([VLESS_REALITY, HY2], "")
        cfg = config_gen.build_config(obs, tags, self._settings())
        self.assertEqual(len(cfg["inbounds"]), 1)
        self.assertEqual(cfg["inbounds"][0]["type"], "mixed")
        self.assertEqual(cfg["inbounds"][0]["listen_port"], 1080)
        ut = [o for o in cfg["outbounds"] if o["type"] == "urltest"][0]
        self.assertEqual(ut["interval"], "3m")
        self.assertEqual(ut["tolerance"], 50)
        self.assertEqual(len(ut["outbounds"]), 2)
        self.assertEqual(cfg["route"]["final"], "proxy-auto")

    def test_lan_inbound_toggle(self):
        obs, tags, _ = config_gen.parse_lines([VLESS_REALITY], "")
        cfg = config_gen.build_config(obs, tags, self._settings(lan_mixed_enabled=True))
        self.assertEqual(len(cfg["inbounds"]), 2)
        self.assertEqual(cfg["inbounds"][1]["listen"], "0.0.0.0")

    def test_config_serializes(self):
        obs, tags, _ = config_gen.parse_lines([VLESS_REALITY, HY2, TROJAN], "")
        cfg = config_gen.build_config(obs, tags, self._settings())
        json.dumps(cfg)  # must not raise


class TestHelpers(unittest.TestCase):
    def test_settings_normalization(self):
        raw = {
            "subscription_url": "http://x",
            "local_port": "1234",
            "lan_mixed_enabled": "true",
            "urltest_tolerance": "75",
            "log_level": "2",
        }
        s = helpers.get_settings(reader=lambda: raw)
        self.assertEqual(s["subscription_url"], "http://x")
        self.assertEqual(s["local_port"], 1234)
        self.assertIs(s["lan_mixed_enabled"], True)
        self.assertEqual(s["urltest_tolerance"], 75)
        self.assertEqual(s["log_level"], "warn")

    def test_settings_defaults(self):
        s = helpers.get_settings(reader=lambda: {})
        self.assertEqual(s["local_port"], 1080)
        self.assertEqual(s["skip_protocols"], "trojan,xhttp")
        self.assertEqual(s["urltest_interval"], "3m")
        self.assertIs(s["interrupt_connections"], True)


class TestBinaryManagerPaths(unittest.TestCase):
    def test_paths(self):
        import tempfile
        with tempfile.TemporaryDirectory() as addon, tempfile.TemporaryDirectory() as work:
            bm = binary_manager.BinaryManager(addon, work, platform_override="linux_x64")
            self.assertTrue(bm.bundled_binary.endswith(
                os.path.join("resources", "bin", "linux_x64", "sing-box")))
            self.assertTrue(bm.work_binary.endswith(
                os.path.join("bin", "linux_x64", "sing-box")))
            self.assertEqual(bm.platform, "linux_x64")


if __name__ == "__main__":
    unittest.main(verbosity=2)
