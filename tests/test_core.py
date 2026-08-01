# -*- coding: utf-8 -*-
"""Unit tests for the Kodi-free core modules (dual-engine redesign).

Run:  python3 tests/test_core.py
No Kodi required; xbmc modules are never imported by the tested code.
"""
import json
import os
import re
import socket
import sys
import tempfile
import time
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
    def __init__(self, name, calls, engine="sing-box", platform="linux_x64"):
        self.name = name
        self.engine = engine
        self.platform = platform
        self._calls = calls
        self._running = True

    def is_running(self):
        return self._running

    def stop(self):
        self._calls.append(("stop", self.name))
        self._running = False

    def start(self, config_path):
        self._calls.append(("start", self.name))
        self._running = True


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
        self.sup.start = lambda: self.sup.bin.start(self.sup.config_path)

    def test_stops_old_binary_before_swapping_in_new_one(self):
        self.sup.reconfigure_engine()
        self.assertEqual(self.calls[0], ("stop", "old"))
        self.assertEqual(self.sup.bin.name, "new")

    def test_starts_new_binary_after_old_one_was_running(self):
        self.sup.reconfigure_engine()
        self.assertIn(("start", "new"), self.calls)
        stop_index = self.calls.index(("stop", "old"))
        start_index = self.calls.index(("start", "new"))
        self.assertLess(stop_index, start_index)

    def test_does_not_start_if_was_stopped_and_autostart_off(self):
        self.sup.settings["autostart"] = False
        self.sup.bin = _FakeBin("old", self.calls)
        self.sup.bin.stop()
        self.calls[:] = []
        self.sup.reconfigure_engine()
        self.assertNotIn(("start", "new"), self.calls)


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

        start = src.index("def _action_activate(")
        end = src.index("\ndef ", start + 1)
        activate = src[start:end]
        self.assertIn("_finish_action(handle)", activate)
        self.assertNotIn("_show_listing(handle)", activate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
