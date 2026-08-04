# -*- coding: utf-8 -*-
"""Functional end-to-end tests: real engines, real proxy traffic.

These tests launch the actual bundled sing-box / Xray binaries with configs
produced by the builders and verify observable behaviour through the proxy:

  * SOCKS and HTTP CONNECT both work on the mixed inbound
  * with no profiles the proxy starts in direct mode and reaches the network
  * subscription fetching ignores Kodi's exported proxy environment
  * adding the same subscription URL twice keeps a single profile set
  * geo rules are only emitted when the database actually exists, so the
    config stays valid for the bundled geoip.dat

Real binaries and real sockets only. Run with the built resources present:
    python3 -m unittest tests.test_functional
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SRC = os.path.join(REPO, "service.advancedproxy", "src")
BIN = os.path.join(REPO, "service.advancedproxy", "resources", "bin", "linux_x64")
sys.path.insert(0, SRC)

import build_singbox  # noqa: E402
import build_xray  # noqa: E402
import subscriptions  # noqa: E402

PROBE = "https://www.gstatic.com/generate_204"
BIGPING = ("https://bigping.duckdns.org/sub/Xj7kM9pQ2wR5vN8sT4fL1hY6gA3dE0cB/urls")

SETTINGS = {
    "local_port": 1080, "mode": "urltest", "urltest_interval": "3m",
    "urltest_tolerance": 50, "test_url": PROBE, "log_level": "info",
}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EngineRunner(object):
    """Start a real engine binary with a config; stop it on teardown."""

    def __init__(self, engine, config, port):
        self.engine = engine
        self.config = config
        self.port = port
        binary = os.path.join(BIN, engine)
        if not os.path.exists(binary):
            self.skip = True
            self.proc = None
            return
        self.skip = False
        if engine == "xray":
            work = os.path.dirname(self.config)
            for name in ("geoip.dat", "geosite.dat"):
                src = os.path.join(BIN, name)
                if os.path.exists(src) and not os.path.exists(
                        os.path.join(work, name)):
                    shutil.copy2(src, os.path.join(work, name))
        args = [binary, "run", "-c", self.config]
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_listen(port)

    def _wait_listen(self, port, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("%s exited early" % self.engine)
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=1)
                s.close()
                time.sleep(0.5)
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("%s did not listen on %d" % (self.engine, port))

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def proxy_get(port, scheme, url, timeout=10):
    """GET URL through the local proxy (scheme: socks5h or http)."""
    import subprocess as sp
    proxy = "%s://127.0.0.1:%d" % (scheme, port)
    result = sp.run(
        ["curl", "-s", "--max-time", str(timeout), "-x", proxy,
         "-o", "/dev/null", "-w", "%{http_code}", url],
        capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _base_settings(**kw):
    s = dict(SETTINGS)
    s.update(kw)
    return s


def _build_config(engine, profiles, settings):
    if engine == "xray":
        return build_xray.build_config(profiles, settings)[0]
    return build_singbox.build_config(profiles, settings)[0]


def _profiles_from_subscription(body):
    parsed, _ = subscriptions.decode_subscription(body)
    return parsed


class TestEngineProxyTraffic(unittest.TestCase):
    """Real sing-box and Xray: the proxy actually serves traffic."""

    def _run(self, engine, profiles=None):
        profiles = profiles if profiles is not None else []
        port = free_port()
        settings = _base_settings(local_port=port)
        config = _build_config(engine, profiles, settings)
        tmp = tempfile.mkdtemp(prefix="func-")
        path = os.path.join(tmp, "engine.json")
        with open(path, "w") as f:
            json.dump(config, f)
        runner = EngineRunner(engine, path, port)
        self.addCleanup(runner.stop)
        self.addCleanup(shutil.rmtree, tmp)
        if runner.skip:
            self.skipTest("no %s binary bundled" % engine)
        return runner, port

    def test_singbox_serves_socks_and_http(self):
        runner, port = self._run("sing-box")
        self.assertEqual(proxy_get(port, "socks5h", PROBE), "204")
        self.assertEqual(proxy_get(port, "http", PROBE), "204")

    def test_xray_serves_socks_and_http(self):
        runner, port = self._run("xray")
        self.assertEqual(proxy_get(port, "socks5h", PROBE), "204")
        self.assertEqual(proxy_get(port, "http", PROBE), "204")

    def test_direct_mode_without_profiles_reaches_the_network(self):
        runner, port = self._run("sing-box")
        self.assertEqual(proxy_get(port, "socks5h", PROBE), "204")

    def test_config_validates_with_real_binary(self):
        for engine in ("sing-box", "xray"):
            with self.subTest(engine=engine):
                port = free_port()
                config = _build_config(engine, [], _base_settings(local_port=port))
                tmp = tempfile.mkdtemp(prefix="func-check-")
                path = os.path.join(tmp, "engine.json")
                with open(path, "w") as f:
                    json.dump(config, f)
                binary = os.path.join(BIN, engine)
                if not os.path.exists(binary):
                    self.skipTest("no %s binary bundled" % engine)
                if engine == "xray":
                    shutil.copy2(os.path.join(BIN, "geoip.dat"), tmp)
                    shutil.copy2(os.path.join(BIN, "geosite.dat"), tmp)
                args = ([binary, "run", "-test", "-c", path]
                        if engine == "xray"
                        else [binary, "check", "-c", path])
                result = subprocess.run(args, capture_output=True, text=True,
                                        timeout=30)
                self.assertEqual(
                    result.returncode, 0,
                    "%s rejected the empty-profiles config: %s"
                    % (engine, (result.stdout + result.stderr)[-500:]))
                shutil.rmtree(tmp)


class TestSubscriptionBehaviour(unittest.TestCase):
    """Subscription add is idempotent and fetch avoids Kodi's proxy env."""

    def test_fetch_ignores_http_proxy_environment(self):
        import importlib
        old = os.environ.get("http_proxy")
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        os.environ["https_proxy"] = "http://127.0.0.1:1"
        try:
            body = subscriptions.fetch(BIGPING, timeout=15)
        finally:
            if old is None:
                os.environ.pop("http_proxy", None)
            else:
                os.environ["http_proxy"] = old
        self.assertGreater(len(body), 100)

    def test_add_same_url_twice_keeps_single_profile_set(self):
        import profiles as profiles_mod
        tmp = tempfile.mkdtemp(prefix="func-sub-")
        self.addCleanup(shutil.rmtree, tmp)
        store = profiles_mod.ProfileStore(os.path.join(tmp, "profiles.json"))
        sub_store = subscriptions.SubscriptionStore(
            os.path.join(tmp, "subscriptions.json"))
        group, err = sub_store.add(BIGPING, profile_store=store)
        self.assertIsNone(err)
        first = len(store.profiles)
        self.assertGreater(first, 10)
        group2, err = sub_store.add(BIGPING, profile_store=store)
        self.assertIsNone(err)
        self.assertEqual(len(store.profiles), first,
                         "re-adding the same URL must not duplicate profiles")
        self.assertEqual(len(sub_store.groups()), 1)

    def test_subscription_profiles_build_valid_config(self):
        import profiles as profiles_mod
        tmp = tempfile.mkdtemp(prefix="func-subcfg-")
        self.addCleanup(shutil.rmtree, tmp)
        store = profiles_mod.ProfileStore(os.path.join(tmp, "profiles.json"))
        sub_store = subscriptions.SubscriptionStore(
            os.path.join(tmp, "subscriptions.json"))
        group, err = sub_store.add(BIGPING, profile_store=store)
        self.assertIsNone(err)
        enabled = store.enabled()
        for engine in ("sing-box", "xray"):
            with self.subTest(engine=engine):
                port = free_port()
                config = _build_config(engine, enabled,
                                       _base_settings(local_port=port))
                path = os.path.join(tmp, "engine-%s.json" % engine)
                with open(path, "w") as f:
                    json.dump(config, f)
                binary = os.path.join(BIN, engine)
                if not os.path.exists(binary):
                    self.skipTest("no %s binary bundled" % engine)
                if engine == "xray":
                    shutil.copy2(os.path.join(BIN, "geoip.dat"), tmp)
                    shutil.copy2(os.path.join(BIN, "geosite.dat"), tmp)
                args = ([binary, "run", "-test", "-c", path]
                        if engine == "xray"
                        else [binary, "check", "-c", path])
                result = subprocess.run(args, capture_output=True, text=True,
                                        timeout=30)
                self.assertEqual(
                    result.returncode, 0,
                    "%s rejected the subscription config: %s"
                    % (engine, (result.stdout + result.stderr)[-500:]))


class TestGeoRules(unittest.TestCase):
    """geoip:ru-blocked must only appear when the database is on disk."""

    def test_rule_absent_without_database(self):
        for engine in ("sing-box", "xray"):
            port = free_port()
            config = _build_config(engine, [],
                                   _base_settings(
                                       local_port=port,
                                       geoip_url="https://a/geoip.dat"))
            if engine == "xray":
                rules = config["routing"]["rules"]
                self.assertFalse(
                    any(r.get("ip") == ["geoip:ru-blocked"] for r in rules),
                    "xray must not emit ru-blocked without the database")

    def test_rule_present_when_database_exists(self):
        tmp = tempfile.mkdtemp(prefix="func-geo-")
        self.addCleanup(shutil.rmtree, tmp)
        geo = os.path.join(tmp, "geoip.dat")
        with open(geo, "w") as f:
            f.write("geo")
        port = free_port()
        config = build_xray.build_config(
            [], _base_settings(local_port=port, geoip_url="https://a/geoip.dat",
                               geo_paths={"geoip": geo,
                                          "geosite": os.path.join(tmp, "geosite.dat")}))[0]
        self.assertTrue(
            any(r.get("ip") == ["geoip:ru-blocked"] for r in config["routing"]["rules"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
