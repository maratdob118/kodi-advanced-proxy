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
        self._calls = []
        self.pid = 12345
        self._wait_calls = 0
    
    def terminate(self):
        self._calls.append("terminate")
        self._terminated = True
    
    def kill(self):
        self._calls.append("kill")
        self._killed = True
    
    def poll(self):
        if self._exit_delay > 0:
            self._exit_delay -= 1
            return None
        if self._terminated and self._exit_code is None:
            self._exit_code = 0
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

    def test_private_ip_traffic_goes_direct(self):
        profs, _ = parsers.parse_lines([VLESS, HY2])
        cfg, _ = build_singbox.build_config(profs, self._settings())
        self.assertEqual(cfg["route"]["rules"], [
            {"action": "sniff"},
            {"protocol": "dns", "action": "hijack-dns"},
            {"ip_is_private": True, "action": "route", "outbound": "direct"},
        ])
        self.assertEqual(cfg["route"]["final"], "proxy")

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

            # Simulate process exit after delay
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
            self.assertIn("Process %s (pid %s) did not exit after SIGKILL" % (bm.engine, fake_proc.pid), log_recorder.entries[-1][1])

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
    """xbmc.Monitor stand-in that aborts after `iterations` loop passes."""

    def __init__(self, iterations=1):
        self._left = iterations

    def abortRequested(self):
        return self._left <= 0

    def waitForAbort(self, seconds):
        self._left -= 1
        return self._left <= 0


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
    configured one.
    """

    def __init__(self, calls, settings, start_ok=True, profiles_enabled=True):
        self.calls = calls
        self.settings = dict(settings)
        self.store = _FakeStore(profiles_enabled)
        self.bin = _FakeEngine()
        self.effective_port = None
        self.last_error = "start failed"
        self.start_ok = start_ok

    def _bring_up(self):
        if not self.start_ok:
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

    def reconfigure_engine(self):
        self.calls.append(("sup.reconfigure",))
        return self._bring_up()

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
              iterations=1, mtimes=None):
    """Run main() end to end against fakes; returns (module, supervisor)."""
    module = _import_main()
    tmp = tempfile.mkdtemp()
    seq = [dict(s) for s in settings_seq]
    holder = {}

    def _get_settings(reader=None):
        return dict(seq.pop(0) if len(seq) > 1 else seq[0])

    def _make_supervisor(**kwargs):
        holder["sup"] = _FakeSupervisor(
            manager.calls, kwargs["settings"], start_ok=start_ok,
            profiles_enabled=profiles_enabled)
        return holder["sup"]

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
        patch(_patched(module.xbmc, "Monitor", lambda: _FakeMonitor(iterations)))
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
                         [("sup.start",), ("restore",), ("sup.stop",)])

    def test_no_profiles_restores_stale_backup(self):
        manager = _FakeIntegrationManager(backup=True)
        _run_main([_settings()], manager, profiles_enabled=False)
        self.assertEqual(manager.calls, [("restore",), ("sup.stop",)])

    def test_autostart_off_restores_stale_backup(self):
        manager = _FakeIntegrationManager(backup=True)
        _run_main([_settings(autostart=False)], manager)
        self.assertEqual(manager.calls, [("restore",), ("sup.stop",)])

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
                          ("sup.stop",)])

    def test_enabling_setting_at_runtime_ensures(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(auto_configure_integration=False), _settings()],
                  manager)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("ensure", INTEGRATION_HOST, 1081),
                          ("restore",), ("sup.stop",)])

    def test_port_change_reconfigures_then_ensures_new_port(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(), _settings(local_port=9090)], manager)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("ensure", INTEGRATION_HOST, 1081),
                          ("sup.reconfigure",),
                          ("ensure", INTEGRATION_HOST, 9091),
                          ("restore",), ("sup.stop",)])

    def test_failed_reconfigure_restores_instead_of_ensuring(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(), _settings(local_port=9090)], manager,
                  start_ok=False)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("sup.reconfigure",), ("sup.stop",)])

    def test_unrelated_setting_change_does_not_touch_integration(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(), _settings(notify=False)], manager)
        self.assertEqual(manager.calls,
                         [("sup.start",), ("ensure", INTEGRATION_HOST, 1081),
                          ("restore",), ("sup.stop",)])

    def test_start_after_profile_change_ensures_effective_port(self):
        manager = _FakeIntegrationManager()
        _run_main([_settings(autostart=False), _settings()], manager,
                  mtimes=[0, 5])
        self.assertEqual(manager.calls,
                         [("sup.reload_profiles",), ("sup.start",),
                          ("ensure", INTEGRATION_HOST, 1081),
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

    def start(self, config_path):
        self.calls.append(("start", config_path))
        self.proc = _FakeProcess()
        return self.proc

    def stop(self):
        self.calls.append(("stop",))
        self.proc = None

    def restart(self, config_path):
        self.calls.append(("restart", config_path))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
