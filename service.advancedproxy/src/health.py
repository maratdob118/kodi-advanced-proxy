# -*- coding: utf-8 -*-
"""Health monitor for auto (urltest) mode. Kodi-free.

The engine's own urltest/leastPing only re-evaluates on its interval and
cannot tell a dead outbound from a slow one when the test target itself is
blocked; on LibreELEC boxes this presented as "internet is gone, nothing
switches, nobody says anything". This monitor probes real connectivity
THROUGH the local proxy every `interval` seconds and, on sustained failure,
actively walks the urltest group (sing-box, via the Clash API) or restarts
the engine (xray) until some outbound answers - notifying on outage, on
switch and on recovery. When sing-box reports active proxy traffic, a probe
is unnecessary. Otherwise the probe downloads enough data to expose DPI that
permits a tiny request but disrupts media streams.
"""
import json
import socket
import time
import urllib.request

DEFAULT_INTERVAL = 30
FAIL_THRESHOLD = 1
OUTAGE_RETRY_EVERY = 4  # re-run failover every Nth failed check
PROBE_BYTES = 500 * 1024
PROBE_TIMEOUT = 4
PROBE_URL = "https://speed.cloudflare.com/__down?bytes=%d" % PROBE_BYTES
DIRECT_PROBE_URL = "https://cp.cloudflare.com/generate_204"
DIRECT_TCP_TARGET = ("1.1.1.1", 443)


def _proxy_fetch(url, port, timeout=PROBE_TIMEOUT, min_bytes=PROBE_BYTES):
    """Download MIN_BYTES through the local HTTP proxy."""
    proxy = "http://127.0.0.1:%d" % port
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    request = urllib.request.Request(url, headers={"User-Agent": "advancedproxy"})
    try:
        with opener.open(request, timeout=timeout) as response:
            received = 0
            while received < min_bytes:
                chunk = response.read(min(64 * 1024, min_bytes - received))
                if not chunk:
                    return False
                received += len(chunk)
        return True
    except Exception:
        return False


def _direct_internet_available(timeout=PROBE_TIMEOUT):
    """Check that the LAN itself can reach the Internet without the proxy."""
    try:
        sock = socket.create_connection(DIRECT_TCP_TARGET, timeout=timeout)
        sock.close()
    except OSError:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(DIRECT_PROBE_URL,
                                     headers={"User-Agent": "advancedproxy"})
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(64)
        return True
    except Exception:
        return False


class ClashGroupControl(object):
    """sing-box Clash API control over the selector group.

    In urltest mode the selector wraps a "proxy-auto" urltest group (the
    Clash API cannot force-select inside urltest groups): current() is the
    selector value, effective() follows through to the urltest's pick.
    """

    def __init__(self, api_port, group="proxy", auto_tag=None, opener=None):
        self.base = "http://127.0.0.1:%d" % api_port
        self.group = group
        self.auto_tag = auto_tag
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}))

    def _get(self, path):
        with self.opener.open(self.base + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def current(self):
        """Currently selected outbound tag, or None."""
        try:
            return self._get("/proxies/%s" % self.group).get("now")
        except Exception:
            return None

    def effective(self):
        """The outbound actually carrying traffic (follows the auto group)."""
        now = self.current()
        if now and self.auto_tag and now == self.auto_tag:
            try:
                return self._get("/proxies/%s" % self.auto_tag).get("now") or now
            except Exception:
                return now
        return now

    def members(self):
        """Failover candidates: real nodes first, the auto group last."""
        try:
            members = list(self._get("/proxies/%s" % self.group).get("all")
                           or [])
        except Exception:
            return []
        if self.auto_tag and self.auto_tag in members:
            members.remove(self.auto_tag)
            members.append(self.auto_tag)
        return members

    def select(self, tag):
        request = urllib.request.Request(
            "%s/proxies/%s" % (self.base, self.group),
            data=json.dumps({"name": tag}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PUT")
        try:
            with self.opener.open(request, timeout=5) as response:
                return 200 <= response.status < 300
        except Exception:
            return False

    def traffic_total(self):
        """Return observed proxied bytes, or None when the API lacks totals."""
        try:
            payload = self._get("/connections")
        except Exception:
            return None
        total = 0
        found = False
        for key in ("downloadTotal", "uploadTotal"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                total += value
                found = True
        if found:
            return total
        for connection in payload.get("connections", []) or []:
            for key in ("download", "upload"):
                value = connection.get(key)
                if isinstance(value, (int, float)):
                    total += value
                    found = True
        return total if found else None


class RestartControl(object):
    """xray has no group-switch API; restarting re-runs leastPing fully."""

    def __init__(self, restart):
        self._restart = restart

    def current(self):
        return None

    def effective(self):
        return None

    def members(self):
        return []

    def select(self, tag):
        return False

    def restart(self):
        self._restart()
        return True


class HealthMonitor(object):
    """Periodic connectivity check through the local proxy + failover.

    All side effects are injected (fetcher, control, notify, log, sleeper),
    so the state machine is unit-testable without sockets.
    """

    def __init__(self, port, test_url, control=None, fetch=None,
                  notify=None, logger=None, interval=DEFAULT_INTERVAL,
                  fail_threshold=FAIL_THRESHOLD, auto_failover=True,
                  sleeper=None, direct_probe=None):
        self.port = port
        self.urls = [PROBE_URL]
        self.control = control
        self.fetch = fetch
        self.notify = notify or (lambda msg, error=False: None)
        self.log = logger or (lambda msg, level="info": None)
        self.interval = interval
        self.fail_threshold = fail_threshold
        self.auto_failover = auto_failover
        self.sleeper = sleeper or time.sleep
        self._last_check = 0
        self._failures = 0
        self._down = False
        self._last_selected = None
        self._last_traffic_total = None
        self.direct_probe = direct_probe or _direct_internet_available

    def tick(self, now=None):
        now = time.time() if now is None else now
        if now - self._last_check < self.interval:
            return None
        self._last_check = now
        return self.check()

    def check(self):
        """One connectivity check. Returns True/False, or None on skip."""
        self._observe_selection()
        if self._traffic_is_active():
            self._failures = 0
            self._down = False
            return True
        if self._any_url_ok():
            if self._down:
                self._down = False
                self.notify("Proxy connectivity restored")
            self._failures = 0
            return True
        self._failures += 1
        self.log("health check failed (%d consecutive)" % self._failures,
                 "warn")
        if self._failures < self.fail_threshold:
            return False
        if not self._down:
            self._down = True
            self.notify("No internet via proxy, switching...", error=True)
            self._failover_or_report_network()
        elif self._failures % OUTAGE_RETRY_EVERY == 0:
            self._failover_or_report_network()
        return False

    # ----- internals -------------------------------------------------
    def _probe(self, url):
        if self.fetch is not None:
            return self.fetch(url, self.port)
        return _proxy_fetch(url, self.port)

    def _any_url_ok(self):
        for url in self.urls:
            if self._probe(url):
                return True
        return False

    def _traffic_is_active(self):
        if self.control is None or not hasattr(self.control, "traffic_total"):
            return False
        total = self.control.traffic_total()
        if total is None:
            return False
        active = (self._last_traffic_total is not None and
                  total > self._last_traffic_total)
        self._last_traffic_total = total
        return active

    def _observe_selection(self):
        """Notify when the engine's urltest picked a different outbound."""
        if self.control is None:
            return
        current = self.control.effective()
        if current is None:
            return
        if self._last_selected and current != self._last_selected:
            self.notify("Auto-switch: %s -> %s"
                        % (self._last_selected, current))
        self._last_selected = current

    def _failover_or_report_network(self):
        if self._failover():
            return
        if self.direct_probe():
            self.notify("All proxy servers unreachable", error=True)
        else:
            self.log("health: direct network check failed; not switching", "warn")
            self.notify("Internet connection unavailable", error=True)

    def _failover(self):
        ctl = self.control
        if ctl is None:
            return False
        if not self.auto_failover:
            return False
        members = ctl.members()
        current = ctl.current()
        old_effective = ctl.effective()
        if members:
            auto_tag = getattr(ctl, "auto_tag", None)
            ordered = [m for m in members
                       if m != current and m != auto_tag][:3]
            for candidate in ordered:
                if not ctl.select(candidate):
                    continue
                self.sleeper(2)
                if self._any_url_ok():
                    self._down = False
                    self._failures = 0
                    new_effective = ctl.effective() or candidate
                    self._last_selected = new_effective
                    self.notify("Auto-switch: %s -> %s"
                                % (old_effective or current, new_effective))
                    return True
            if current:
                ctl.select(current)
            return False
        elif hasattr(ctl, "restart"):
            self.log("health: restarting engine to re-evaluate outbounds",
                     "warn")
            ctl.restart()
            self.sleeper(3)
            if self._any_url_ok():
                self._down = False
                self._failures = 0
                self.notify("Proxy connectivity restored")
                return True
            else:
                return False
        return False
