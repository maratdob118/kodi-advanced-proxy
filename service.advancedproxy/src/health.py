# -*- coding: utf-8 -*-
"""Health monitor for auto (urltest) mode. Kodi-free.

The engine's own urltest/leastPing only re-evaluates on its interval and
cannot tell a dead outbound from a slow one when the test target itself is
blocked; on LibreELEC boxes this presented as "internet is gone, nothing
switches, nobody says anything". This monitor probes real connectivity
THROUGH the local proxy every `interval` seconds and, on sustained failure,
actively walks the urltest group (sing-box, via the Clash API) or restarts
the engine (xray) until some outbound answers - notifying on outage, on
switch and on recovery.
"""
import json
import time
import urllib.request

# The primary test_url comes from settings; the fallbacks cover the case
# where the primary target itself is blocked (gstatic is a popular block
# target). http:// probes double as captive-portal-style checks.
FALLBACK_URLS = (
    "https://cp.cloudflare.com/generate_204",
    "http://detectportal.firefox.com/success.txt",
    "http://connectivitycheck.gstatic.com/generate_204",
)

DEFAULT_INTERVAL = 30
FAIL_THRESHOLD = 2
OUTAGE_RETRY_EVERY = 4  # re-run failover every Nth failed check


def _proxy_fetch(url, port, timeout=10):
    """GET URL through the local HTTP proxy. Returns True on any response."""
    proxy = "http://127.0.0.1:%d" % port
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    request = urllib.request.Request(url, headers={"User-Agent": "advancedproxy"})
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
                 sleeper=None):
        self.port = port
        self.urls = [u for u in [test_url] if u] + [
            u for u in FALLBACK_URLS if u != test_url]
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

    def tick(self, now=None):
        now = time.time() if now is None else now
        if now - self._last_check < self.interval:
            return None
        self._last_check = now
        return self.check()

    def check(self):
        """One connectivity check. Returns True/False, or None on skip."""
        self._observe_selection()
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
            self._failover()
        elif self._failures % OUTAGE_RETRY_EVERY == 0:
            self._failover()
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

    def _failover(self):
        ctl = self.control
        if ctl is None:
            return
        if not self.auto_failover:
            return
        members = ctl.members()
        current = ctl.current()
        old_effective = ctl.effective()
        if members:
            ordered = [m for m in members if m != current]
            for candidate in ordered:
                if not ctl.select(candidate):
                    continue
                self.sleeper(2)
                if self._any_url_ok():
                    self._down = False
                    self._failures = 0
                    new_effective = ctl.effective() or candidate
                    self._last_selected = new_effective
                    self.notify("Switched: %s -> %s"
                                % (old_effective or current, new_effective))
                    return
            if current:
                ctl.select(current)
            self.notify("All proxy servers unreachable", error=True)
        elif hasattr(ctl, "restart"):
            self.log("health: restarting engine to re-evaluate outbounds",
                     "warn")
            ctl.restart()
            self.sleeper(3)
            if self._any_url_ok():
                self._down = False
                self._failures = 0
                self.notify("Proxy connectivity restored")
            else:
                self.notify("All proxy servers unreachable", error=True)
