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

Scoring: failover candidates are ordered by measured evidence, not by list
position. Each candidate accumulates data-transfer successes/failures plus
urltest delay signals; failures penalize heavily with hysteresis so one slow
sample cannot flap the proxy. A pinned emergency selection returns to
"proxy-auto" once the automatic choice proves healthy again.

Traffic gating: only byte growth on connections chained through the
currently effective outbound counts as "traffic is flowing". Unrelated
background bytes must not mask an outage of the stream the user watches.
"""
import json
import socket
import time
import urllib.parse
import urllib.request

DEFAULT_INTERVAL = 30
FAIL_THRESHOLD = 1
OUTAGE_RETRY_EVERY = 4  # re-run failover every Nth failed check
PROBE_BYTES = 500 * 1024
PROBE_TIMEOUT = 10
LIVENESS_TIMEOUT = 5
PROBE_URL = "https://speed.cloudflare.com/__down?bytes=%d" % PROBE_BYTES
DIRECT_PROBE_URL = "https://cp.cloudflare.com/generate_204"
DIRECT_TCP_TARGET = ("1.1.1.1", 443)
# Byte growth on the effective chain required to skip the active probe.
# Small keep-alives must not mask a stalled stream.
ACTIVE_TRAFFIC_MIN_BYTES = 64 * 1024
MAX_CANDIDATES = 3
# Healthy checks while pinned before attempting a return to automatic.
RETURN_TO_AUTO_AFTER = 10


def _liveness_fetch(url, port, timeout=LIVENESS_TIMEOUT):
    """True when any HTTP response (< 400) arrives through the proxy.

    Liveness targets (generate_204 and friends) answer with an empty body
    by design, so - unlike the data probe - an empty read still counts.
    """
    proxy = "http://127.0.0.1:%d" % port
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    request = urllib.request.Request(url, headers={"User-Agent": "advancedproxy"})
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            try:
                response.read(64)
            except Exception:
                pass
            return 200 <= status < 400
    except Exception:
        return False


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


class NodeStats(object):
    """Per-candidate evidence collected across checks."""

    def __init__(self):
        self.successes = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.last_delay_ms = None
        self.last_throughput_kbs = None


class AutoScorer(object):
    """Orders failover candidates by measured evidence.

    Higher score wins. Delay lowers the score, failures penalize heavily,
    measured throughput adds a bounded bonus. Unknown nodes start below any
    node with a fresh delay sample but above repeatedly failed ones.
    """

    UNKNOWN_DELAY_PENALTY = 600
    CONSECUTIVE_FAILURE_PENALTY = 250
    FAILURE_RATE_PENALTY = 300
    MAX_DELAY_PENALTY = 1000
    MAX_THROUGHPUT_BONUS = 200

    def __init__(self):
        self.stats = {}

    def _stats(self, tag):
        return self.stats.setdefault(tag, NodeStats())

    def record_success(self, tag, delay_ms=None, throughput_kbs=None):
        st = self._stats(tag)
        st.successes += 1
        st.consecutive_failures = 0
        if delay_ms is not None:
            st.last_delay_ms = delay_ms
        if throughput_kbs is not None:
            st.last_throughput_kbs = throughput_kbs

    def record_failure(self, tag):
        st = self._stats(tag)
        st.failures += 1
        st.consecutive_failures += 1

    def score(self, tag, delay_ms=None):
        st = self.stats.get(tag, NodeStats())
        delay = delay_ms if delay_ms is not None else st.last_delay_ms
        result = 1000.0
        if delay is not None:
            try:
                result -= min(float(delay), self.MAX_DELAY_PENALTY)
            except (TypeError, ValueError):
                result -= self.UNKNOWN_DELAY_PENALTY
        else:
            result -= self.UNKNOWN_DELAY_PENALTY
        result -= st.consecutive_failures * self.CONSECUTIVE_FAILURE_PENALTY
        total = st.successes + st.failures
        if total:
            result -= self.FAILURE_RATE_PENALTY * (st.failures / total)
        if st.last_throughput_kbs:
            try:
                result += min(self.MAX_THROUGHPUT_BONUS,
                              float(st.last_throughput_kbs) / 10.0)
            except (TypeError, ValueError):
                pass
        return result

    def ranked(self, candidates, delays=None):
        """Candidates ordered best-first; ties keep the original order."""
        delays = delays or {}
        indexed = []
        for index, tag in enumerate(candidates):
            indexed.append((self.score(tag, delays.get(tag)), index, tag))
        indexed.sort(key=lambda item: (-item[0], item[1]))
        return [tag for _, _, tag in indexed]


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

    def delays(self):
        """Last urltest delay per node, from the Clash API history."""
        try:
            payload = self._get("/proxies")
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        result = {}
        for tag, info in payload.items():
            if not isinstance(info, dict):
                continue
            history = info.get("history") or []
            if not history:
                continue
            delay = history[-1].get("delay")
            if isinstance(delay, (int, float)):
                result[tag] = int(delay)
        return result

    def active_delay(self, tag, url, timeout=PROBE_TIMEOUT):
        """Actively measure one node's delay without switching traffic."""
        try:
            path = "/proxies/%s/delay?url=%s&timeout=%d" % (
                urllib.parse.quote(tag, safe=""),
                urllib.parse.quote(url, safe=""),
                int(timeout * 1000))
            data = self._get(path)
        except Exception:
            return None
        delay = data.get("delay") if isinstance(data, dict) else None
        return int(delay) if isinstance(delay, (int, float)) else None

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

    def effective_traffic_total(self, effective):
        """Bytes on connections chained through EFFECTIVE, else the totals."""
        try:
            payload = self._get("/connections")
        except Exception:
            return None
        connections = payload.get("connections") or []
        if effective:
            total = 0
            found = False
            for connection in connections:
                chains = connection.get("chains") or []
                if effective not in chains:
                    continue
                for key in ("download", "upload"):
                    value = connection.get(key)
                    if isinstance(value, (int, float)):
                        total += value
                        found = True
            if found:
                return total
        return self.traffic_total()


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
                 sleeper=None, direct_probe=None, scorer=None,
                 return_after=RETURN_TO_AUTO_AFTER,
                 min_active_bytes=ACTIVE_TRAFFIC_MIN_BYTES,
                 liveness_urls=None, probe_url=None, preferred_tag=None,
                 startup_grace=0):
        self.port = port
        self.test_url = test_url
        self.liveness_urls = list(liveness_urls) if liveness_urls else (
            [test_url] if test_url else [DIRECT_PROBE_URL])
        template = probe_url or PROBE_URL
        try:
            data_url = template.replace("{n}", str(PROBE_BYTES))
        except Exception:
            data_url = PROBE_URL
        self.data_url = data_url
        self.urls = [data_url]
        self.preferred_tag = preferred_tag
        self._restore_counter = 0
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
        self.scorer = scorer or AutoScorer()
        self.return_after = return_after
        self.min_active_bytes = min_active_bytes
        self.pinned_tag = None
        self._healthy_while_pinned = 0
        self.startup_grace = startup_grace
        self._checks = 0
        # Playback freeze: while a stream is playing the active node is
        # pinned (urltest would otherwise hop every few minutes and change
        # the exit IP) and no failover/probe is attempted.
        self.frozen = False
        self._frozen_pin = None

    def set_frozen(self, active):
        """Enter/leave playback freeze. Idempotent."""
        active = bool(active)
        if active == self.frozen:
            return
        self.frozen = active
        if active:
            self._freeze_pin()
        else:
            self._unfreeze_pin()

    def _freeze_pin(self):
        """Pin the selector to the current outbound so urltest stops hopping."""
        ctl = self.control
        auto_tag = getattr(ctl, "auto_tag", None) if ctl else None
        if not auto_tag:
            return
        try:
            if ctl.current() != auto_tag:
                return
        except Exception:
            return
        current = self._effective_tag()
        if not current or current == auto_tag:
            return
        try:
            if ctl.select(current):
                self._frozen_pin = current
                self.log("health: pinned %s during playback" % current)
        except Exception:
            pass

    def _unfreeze_pin(self):
        """Resume automatic (urltest) selection after playback."""
        pin = self._frozen_pin
        self._frozen_pin = None
        if not pin:
            return
        auto_tag = getattr(self.control, "auto_tag", None)
        if not auto_tag:
            return
        try:
            if self.control.select(auto_tag):
                self._last_selected = None  # avoid a spurious auto-switch notify
                self.log("health: resumed automatic selection after playback")
        except Exception:
            pass

    def tick(self, now=None):
        now = time.time() if now is None else now
        if self.frozen:
            return None
        if now - self._last_check < self.interval:
            return None
        self._last_check = now
        return self.check()

    def check(self):
        """One connectivity check. Returns True/False, or None on skip."""
        if self.frozen:
            return None
        self._checks += 1
        self._observe_selection()
        effective = self._effective_tag()
        if self._traffic_is_active():
            self._failures = 0
            self._down = False
            if effective:
                self.scorer.record_success(effective)
            self._maybe_return_to_auto()
            self._maybe_restore_preferred()
            return True
        ok, elapsed = self._check_data()
        self._rebaseline_traffic()
        if ok:
            if self._down:
                self._down = False
                self.notify("Proxy connectivity restored")
            self._failures = 0
            if effective:
                self.scorer.record_success(
                    effective, throughput_kbs=self._throughput(elapsed))
            self._maybe_return_to_auto()
            self._maybe_restore_preferred()
            return True
        self._failures += 1
        self.log("health check failed (%d consecutive)" % self._failures,
                 "warn")
        if effective:
            self.scorer.record_failure(effective)
        if self._failures < self.fail_threshold:
            return False
        if self._checks <= self.startup_grace:
            # The engine (and urltest) is still warming up right after
            # start: observe, do not walk candidates on the first check.
            self.log("health: warm-up check failed, waiting for engine",
                     "warn")
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

    def _liveness(self, url):
        if self.fetch is not None:
            return self.fetch(url, self.port)
        return _liveness_fetch(url, self.port)

    def _probe_timed(self, url):
        start = time.time()
        try:
            ok = bool(self._probe(url))
        except Exception:
            ok = False
        return ok, max(time.time() - start, 0.001)

    def _throughput(self, elapsed):
        try:
            return (PROBE_BYTES / 1024.0) / max(elapsed, 0.001)
        except Exception:
            return None

    def _any_liveness_ok(self):
        """Stage 1 (cheap): any configured check URL answers through proxy."""
        for url in self.liveness_urls:
            try:
                if self._liveness(url):
                    return True
            except Exception:
                continue
        return False

    def _check_data(self):
        """Two stages: liveness first, then the data-transfer probe.

        Returns (ok, elapsed_seconds). The data stage exposes DPI that
        answers tiny requests while breaking streams.
        """
        if not self._any_liveness_ok():
            return False, 0.0
        return self._probe_timed(self.urls[0])

    def _any_url_ok(self):
        ok, _ = self._check_data()
        return ok

    def _effective_tag(self):
        try:
            return self.control.effective() if self.control else None
        except Exception:
            return None

    def _current_traffic_total(self):
        """Cumulative proxied bytes, or None when unavailable.

        Totals (not per-connection chains) are used on purpose: chain tags
        lag behind urltest switches and long-lived connections keep the
        previous outbound, so chain filtering reports zero while data
        flows. Totals are cumulative, hence immune to naming lag.
        """
        if self.control is None:
            return None
        if not hasattr(self.control, "traffic_total"):
            return None
        try:
            return self.control.traffic_total()
        except Exception:
            return None

    def _rebaseline_traffic(self):
        """Forget bytes our own probes just moved.

        Without this, an idle box would see its own 500 KiB probe as
        "active traffic" and skip every second check.
        """
        total = self._current_traffic_total()
        if total is not None:
            self._last_traffic_total = total

    def _traffic_is_active(self):
        total = self._current_traffic_total()
        if total is None:
            return False
        previous = self._last_traffic_total
        self._last_traffic_total = total
        if previous is None or total < previous:
            return False
        return (total - previous) >= self.min_active_bytes

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
        try:
            selector_now = self.control.current()
        except Exception:
            selector_now = None
        auto_tag = getattr(self.control, "auto_tag", None)
        if auto_tag and selector_now == auto_tag:
            self.pinned_tag = None
            self._healthy_while_pinned = 0

    def _maybe_return_to_auto(self):
        ctl = self.control
        if ctl is None or not self.auto_failover:
            return
        auto_tag = getattr(ctl, "auto_tag", None)
        if not auto_tag or not self.pinned_tag:
            return
        try:
            selector_now = ctl.current()
        except Exception:
            return
        if selector_now == auto_tag:
            self.pinned_tag = None
            self._healthy_while_pinned = 0
            return
        self._healthy_while_pinned += 1
        if self._healthy_while_pinned < self.return_after:
            return
        self.log("health: pinned %s healthy, trying automatic selection"
                 % self.pinned_tag)
        try:
            selected = ctl.select(auto_tag)
        except Exception:
            selected = False
        if not selected:
            self._healthy_while_pinned = 0
            return
        self.sleeper(2)
        ok, _ = self._check_data()
        self._rebaseline_traffic()
        if ok:
            best = self._effective_tag() or auto_tag
            self.pinned_tag = None
            self._healthy_while_pinned = 0
            self._last_selected = best
            self.notify("Auto: back to automatic selection (best: %s)" % best)
        else:
            try:
                ctl.select(self.pinned_tag)
            except Exception:
                pass
            self._healthy_while_pinned = 0
            self.log("health: automatic selection not healthy, kept %s"
                     % self.pinned_tag, "warn")

    def _maybe_restore_preferred(self):
        """Supervised manual: return to the user's preferred profile.

        Runs on healthy checks while the effective outbound differs from the
        preferred one. A delay pre-check (when the control supports it)
        avoids disrupting traffic for a still-dead preferred profile.
        """
        ctl = self.control
        if ctl is None or not self.auto_failover or not self.preferred_tag:
            return
        effective = self._effective_tag()
        if effective == self.preferred_tag:
            self._restore_counter = 0
            return
        self._restore_counter += 1
        if self._restore_counter < self.return_after:
            return
        self._restore_counter = 0
        if hasattr(ctl, "active_delay"):
            try:
                delay = ctl.active_delay(self.preferred_tag,
                                         self.liveness_urls[0])
            except Exception:
                delay = None
            if delay is None:
                self.log("health: preferred %s still failing delay check"
                         % self.preferred_tag)
                return
        try:
            selected = ctl.select(self.preferred_tag)
        except Exception:
            selected = False
        if not selected:
            return
        self.sleeper(2)
        ok, elapsed = self._check_data()
        self._rebaseline_traffic()
        if ok:
            self.scorer.record_success(
                self.preferred_tag,
                throughput_kbs=self._throughput(elapsed))
            self._down = False
            self._failures = 0
            self._last_selected = ctl.effective() or self.preferred_tag
            self.pinned_tag = self.preferred_tag
            self.notify("Preferred profile is back: %s" % self.preferred_tag)
        else:
            self.scorer.record_failure(self.preferred_tag)
            try:
                if effective:
                    ctl.select(effective)
            except Exception:
                pass
            self.log("health: preferred %s not back yet" % self.preferred_tag,
                     "warn")

    def _failover_or_report_network(self):
        # Check the LAN first: pointless to walk proxies while offline.
        try:
            online = bool(self.direct_probe())
        except Exception:
            online = True
        if not online:
            self.log("health: direct network check failed; not switching",
                     "warn")
            self.notify("Internet connection unavailable", error=True)
            return
        if self._failover():
            return
        self.notify("All proxy servers unreachable", error=True)

    def _candidate_delays(self):
        ctl = self.control
        if ctl is None or not hasattr(ctl, "delays"):
            return {}
        try:
            delays = ctl.delays() or {}
        except Exception:
            return {}
        return delays if isinstance(delays, dict) else {}

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
            candidates = [m for m in members
                          if m != current and m != auto_tag]
            ordered = self.scorer.ranked(
                candidates, self._candidate_delays())[:MAX_CANDIDATES]
            # Supervised manual: the user's preferred profile goes first.
            if self.preferred_tag and self.preferred_tag in candidates:
                if self.preferred_tag in ordered:
                    ordered.remove(self.preferred_tag)
                ordered.insert(0, self.preferred_tag)
                ordered = ordered[:MAX_CANDIDATES]
            for candidate in ordered:
                delay = self._candidate_delays().get(candidate)
                if not ctl.select(candidate):
                    self.scorer.record_failure(candidate)
                    continue
                self.sleeper(2)
                ok, elapsed = self._check_data()
                self._rebaseline_traffic()
                if ok:
                    self.scorer.record_success(
                        candidate, delay_ms=delay,
                        throughput_kbs=self._throughput(elapsed))
                    self._down = False
                    self._failures = 0
                    new_effective = ctl.effective() or candidate
                    self._last_selected = new_effective
                    self.pinned_tag = candidate
                    self._healthy_while_pinned = 0
                    self.log("health: switched %s -> %s (score %.0f)"
                             % (old_effective or current, new_effective,
                                self.scorer.score(candidate, delay)))
                    self.notify("Auto-switch: %s -> %s"
                                % (old_effective or current, new_effective))
                    return True
                self.scorer.record_failure(candidate)
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
