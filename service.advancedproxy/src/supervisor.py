# -*- coding: utf-8 -*-
"""ProxySupervisor: build engine config from profiles, run engine, keep alive.

Engine-agnostic (sing-box / xray), mode-aware (urltest / manual). Kodi-free;
UI notifications are delivered via an injected `notify` callable. Driven by
calling `tick()` from an outer loop.
"""
import json
import os
import time

import binary_manager
import build_singbox
import build_xray
import health
import helpers
import port_utils
import profiles

LOG_ROTATE_BYTES = 4 * 1024 * 1024
LOG_KEEP_BYTES = 1 * 1024 * 1024

IDLE_PROBE_INTERVAL = 180
AWAY_PROBE_INTERVAL = 3600


def _default_log(msg, level="info"):
    pass


def _default_notify(msg, error=False):
    pass


class ProxySupervisor(object):
    def __init__(self, settings, addon_dir, work_dir, logger=None, notify=None,
                 should_stop=None):
        self.settings = dict(settings)
        self._logger = logger or _default_log
        self.log = self._log
        self.notify = notify or _default_notify
        self.should_stop = should_stop or (lambda: False)
        self.addon_dir = addon_dir
        self.work_dir = work_dir
        self.config_path = os.path.join(work_dir, "engine.json")
        self.state_path = os.path.join(work_dir, "state.json")
        self.settings.setdefault("log_path", os.path.join(work_dir, "engine.log"))
        self.store = profiles.ProfileStore(
            os.path.join(work_dir, "profiles.json"))
        self.bin = self._make_binary_manager()
        self.consecutive_failures = 0
        self.last_error = None
        self._restart_at = None
        self._was_running = False
        self._last_active_tag = None
        self.effective_port = None
        self.clash_port = None
        self.health = None
        self._shutting_down = False
        self._refreshing_subscriptions = False
        self.refresh_subscriptions = None  # injectable; real default set lazily
        # Set by the outer loop (screensaver / idle time): overrides the
        # configured health interval while the box is unattended.
        self.health_interval_override = None
        # Set by the outer loop while Kodi plays media. While true the
        # engine is never switched or restarted: a node change alters the
        # exit IP and breaks in-flight streams (YouTube googlevideo URLs are
        # IP-signed; Elementum tracker announces die with the connection).
        self.playback_active = False
        self._pending_reload = False       # profiles/subscriptions changed
        self._pending_reconfigure = False  # engine/mode/port changed

    def _log(self, msg, level="info"):
        """Log and surface every error as a toast notification."""
        try:
            self._logger(msg, level)
        except Exception:
            pass
        if level == "error":
            try:
                self.notify(msg, error=True)
            except Exception:
                pass

    def _rotate_engine_log(self):
        """Cap engine.log growth (SD-card wear): keep the tail only."""
        path = os.path.join(self.work_dir, "engine.log")
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size <= LOG_ROTATE_BYTES:
            return
        try:
            with open(path, "rb") as f:
                f.seek(max(0, size - LOG_KEEP_BYTES))
                tail = f.read()
            with open(path, "wb") as f:
                f.write(b"...truncated by log rotation...\n" + tail)
            self._logger("rotated engine.log (%d -> %d bytes)"
                         % (size, len(tail)))
        except OSError as e:
            self._logger("engine log rotation failed: %s" % e, "warn")

    def _make_binary_manager(self):
        return binary_manager.BinaryManager(
            addon_dir=self.addon_dir,
            work_dir=self.work_dir,
            engine=self.settings.get("engine", "sing-box"),
            platform_override=self.settings.get("binary_platform_override", "auto"),
            logger=self.log,
            custom_path=self.settings.get("binary_custom_path", ""),
        )

    # ----- config ----------------------------------------------------
    def _resolve_effective_port(self):
        """Pick the port the engine will actually listen on.

        If the configured local_port is already taken (by a previously
        installed sing-box/xray/shadowsocks or any other service), fall back
        to the next free port so the proxy can always come up. The chosen port
        is kept for the whole session for stability (Kodi's system proxy keeps
        pointing at it) and only re-evaluated when the setting changes.
        """
        preferred = int(self.settings.get("local_port", 1080))
        if self.settings.get("engine") == "xray":
            # Xray serves HTTP on the effective port and SOCKS on the next
            # one (it cannot multiplex both on a single listener), so the
            # pair must be free.
            port = port_utils.find_free_port_pair(preferred)
        else:
            port = port_utils.find_free_port(preferred)
        self.effective_port = port
        self.clash_port = None
        if (self.settings.get("engine", "sing-box") == "sing-box"
                and self.settings.get("mode") != "direct"):
            self.clash_port = port_utils.find_free_port(port + 100)
        if port != preferred:
            self.log("port %d is busy, using %d instead"
                     % (preferred, port), "warn")
            self.notify("Port %d busy, proxy on %d" % (preferred, port),
                        error=True)
        return port

    def _cleanup_before_port_selection(self):
        """Wait briefly for a removed orphan to release the configured port."""
        stale = self.bin.kill_stale()
        if not stale:
            return
        preferred = int(self.settings.get("local_port", 1080))
        ports = (preferred, preferred + 1) if self.settings.get("engine") == "xray" else (preferred,)
        deadline = time.monotonic() + 3.0
        while any(port_utils.port_in_use(port) for port in ports):
            if time.monotonic() >= deadline:
                self.log("configured port still occupied after stale engine cleanup", "warn")
                return
            time.sleep(0.1)

    def _build_settings(self):
        """Settings copy with the effective port injected for config gen.

        Unless debug mode is on, the engine log level is forced to "warn":
        per-connection info logging would otherwise grow engine.log without
        bounds and wear SD cards.
        """
        s = dict(self.settings)
        if self.effective_port:
            s["local_port"] = self.effective_port
        if self.clash_port:
            s["clash_api_port"] = self.clash_port
        if not s.get("debug", False):
            s["log_level"] = "warn"
        s["geo_paths"] = {"geoip": os.path.join(self.work_dir, "geoip.dat"),
                          "geosite": os.path.join(self.work_dir, "geosite.dat")}
        return s

    def _supervision(self):
        """Supervised-manual state, dropping a preferred profile that is gone.

        Subscription refreshes rename and remove nodes; a preferred tag that
        no longer maps to an enabled profile would make the monitor retry a
        dead restore forever, so such supervision is cleared on sight.
        """
        try:
            data = helpers.load_supervision(self.work_dir)
        except Exception:
            return {"supervised": False, "preferred": None}
        if not data.get("supervised"):
            return data
        preferred = data.get("preferred")
        enabled_tags = [p["tag"] for p in self.store.enabled()]
        if preferred and preferred not in enabled_tags:
            self.log("supervised profile %s no longer exists, clearing"
                     % preferred, "warn")
            try:
                helpers.clear_supervision(self.work_dir)
            except Exception:
                pass
            return {"supervised": False, "preferred": None}
        return data

    def _make_health_monitor(self):
        """Connectivity monitor for supervised modes; None when disabled.

        Hard manual gets no background probes (on-demand latency test in
        the UI is enough); supervised temporary manual keeps failover plus
        preferred-profile restore.
        """
        if not self.settings.get("health_check", True):
            return None
        if not self.settings.get("auto_check", True):
            return None
        mode = self.settings.get("mode")
        if mode == "direct":
            return None
        supervision = self._supervision()
        preferred = None
        if mode == "manual":
            if not supervision.get("supervised"):
                return None
            preferred = supervision.get("preferred")
        engine = self.settings.get("engine", "sing-box")
        if engine == "sing-box":
            auto_tag = ("proxy-auto" if mode == "urltest" else None)
            control = (health.ClashGroupControl(self.clash_port,
                                                auto_tag=auto_tag)
                       if self.clash_port else None)
        else:
            control = health.RestartControl(
                lambda: self.bin.restart(self.config_path,
                                         port=self.effective_port))
        test_urls = self.settings.get("test_urls") or None
        if isinstance(test_urls, str):
            test_urls = [test_urls]
        return health.HealthMonitor(
            port=self.effective_port or int(self.settings.get("local_port", 1080)),
            test_url=self.settings.get("test_url"),
            control=control,
            notify=self.notify,
            logger=self.log,
            interval=int(self.settings.get("health_interval", 30) or 30),
            auto_failover=(mode == "urltest" or bool(preferred)),
            liveness_urls=test_urls,
            probe_url=self.settings.get("probe_url"),
            preferred_tag=preferred,
            startup_grace=2)

    def _generate_config(self):
        """Build the engine config from current profiles/settings.

        Returns (config, skipped)). Raises on failure.
        """
        engine = self.settings.get("engine", "sing-box")
        active = self.store.active_tag
        build_settings = self._build_settings()
        if engine == "xray":
            return build_xray.build_config(self.store.enabled(), build_settings, active)
        return build_singbox.build_config(self.store.enabled(), build_settings, active)

    @staticmethod
    def _canonical(config):
        return json.dumps(config, indent=2, sort_keys=True)

    def _config_matches(self, config):
        """True when CONFIG equals the config file currently on disk."""
        try:
            with open(self.config_path) as f:
                current = json.load(f)
        except (OSError, ValueError):
            return False
        return self._canonical(current) == self._canonical(config)

    def build_and_write_config(self):
        enabled = self.store.enabled()
        try:
            config, skipped = self._generate_config()
        except Exception as e:
            self.last_error = "config build failed: %s" % e
            self.log(self.last_error, "error")
            return False

        tmp = self.config_path + ".new.json"
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)

        ok, out = self.bin.check(tmp)
        if not ok:
            self.last_error = "%s config invalid: %s" % (self.bin.engine, out.strip()[:300])
            self.log(self.last_error, "error")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

        os.replace(tmp, self.config_path)
        self.last_error = None
        self.log("config written: %s, %d profiles, %d skipped, mode=%s"
                 % (self.bin.engine, len(enabled), len(skipped), self.settings.get("mode")))
        if skipped:
            self.log("skipped: %s" % "; ".join("%s(%s)" % (t, r) for t, r in skipped), "warn")
        return True

    # ----- lifecycle -------------------------------------------------
    def _write_state(self):
        """Persist a small runtime snapshot for the UI (default.py)."""
        state = {
            "engine": self.bin.engine,
            "platform": self.bin.platform,
            "mode": self.settings.get("mode"),
            "port": self.effective_port or self.settings.get("local_port"),
            "running": self.bin.is_running(),
            "active": self.store.active_tag,
            "last_error": self.last_error,
        }
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def begin_shutdown(self):
        """Enter the shutting-down state. Idempotent.

        A pending watchdog restart is cancelled immediately and no later
        start/restart call can bring the engine back up.
        """
        self._shutting_down = True
        self._restart_at = None

    def start(self):
        if self._shutting_down:
            self.log("start: shutting down, engine not started", "warn")
            return False
        # Kill leftovers from a previous run BEFORE picking a port: a stale
        # engine still holding the configured port must not push us onto a
        # fallback port.
        self._rotate_engine_log()
        self._cleanup_before_port_selection()
        self._resolve_effective_port()
        return self._start_with_port()

    def _start_with_port(self):
        """Start the engine with the already-resolved effective port."""
        if not self.build_and_write_config():
            self._write_state()
            return False
        try:
            self.bin.start(self.config_path, port=self.effective_port)
        except Exception as e:
            self.last_error = "failed to start %s: %s" % (self.bin.engine, e)
            self.log(self.last_error, "error")
            self._write_state()
            return False
        self.consecutive_failures = 0
        self._was_running = True
        self._last_active_tag = self.store.active_tag
        self.health = self._make_health_monitor()
        self._write_state()
        return True

    def stop(self):
        self.begin_shutdown()
        self.bin.stop(port=self.effective_port)
        self.health = None
        self._was_running = False
        self._write_state()

    def restart(self):
        """Rebuild config from current profiles/settings, then restart the engine.

        Skipped when the rebuilt config is byte-identical to the running one:
        the UI writes profiles.json more than once for a single edit, and a
        pointless restart drops every in-flight connection.
        """
        if self._shutting_down:
            self.log("restart: shutting down, engine not restarted", "warn")
            return False
        try:
            config, _skipped = self._generate_config()
        except Exception as e:
            self.log("restart: config build failed, keeping current process: %s"
                     % e, "warn")
            return False
        if self.bin.is_running() and self._config_matches(config):
            self.log("config unchanged, skipping restart")
            return False
        if not self.build_and_write_config():
            self.log("restart: config invalid, keeping current process", "warn")
            return False
        self.bin.restart(self.config_path, port=self.effective_port)
        self._last_active_tag = self.store.active_tag
        return True

    def request_reload(self):
        """Apply a profiles/subscriptions change, deferring during playback.

        Returns True when the engine was (re)started, False otherwise.
        """
        if self.playback_active:
            self._pending_reload = True
            self.log("proxy update deferred until playback stops")
            return False
        self.reload_profiles()
        if self.bin.is_running():
            return self.restart()
        if self.settings.get("autostart"):
            return self.start()
        return False

    def request_reconfigure(self):
        """Apply an engine/mode/port change, deferring during playback."""
        if self.playback_active:
            self._pending_reconfigure = True
            self.log("engine reconfiguration deferred until playback stops")
            return False
        return self.reconfigure_engine()

    def _apply_pending(self):
        """Run work that was deferred while media was playing."""
        if self.playback_active:
            return
        if self._pending_reconfigure:
            self._pending_reconfigure = False
            self.log("applying deferred engine reconfiguration")
            self.reconfigure_engine()
        if self._pending_reload:
            self._pending_reload = False
            self.log("applying deferred profile update")
            self.reload_profiles()
            if self.bin.is_running():
                self.restart()
            elif self.settings.get("autostart"):
                self.start()

    def reconfigure_engine(self):
        """Rebuild self.bin after engine/mode/port settings changed.

        Stops the currently running process through the OLD BinaryManager
        instance before swapping in a new one, so the old process releases
        its port before a new one tries to bind it. Replacing self.bin first
        would silently orphan the old process (it holds the port forever).
        The effective port is resolved once and handed to the internal
        already-resolved start path.
        """
        if self._shutting_down:
            return False
        was_running = self.bin.is_running()
        if was_running:
            self.bin.stop(port=self.effective_port)
        self.bin = self._make_binary_manager()
        self._cleanup_before_port_selection()
        self._resolve_effective_port()
        if was_running or (self.settings.get("autostart") and self.store.enabled()):
            self._start_with_port()
        return self.bin.is_running()

    # ----- tick ------------------------------------------------------
    def tick(self):
        now = time.time()
        self._maybe_refresh_subscriptions(now)
        self._apply_pending()

        if self.bin.is_running():
            if not self._was_running:
                self._was_running = True
                self.consecutive_failures = 0
                self._restart_at = None
                self._write_state()
                self.notify("%s proxy up on 127.0.0.1:%s"
                            % (self.bin.engine,
                               self.effective_port or self.settings.get("local_port")))
            self._watch_active_change()
            if self.health is not None:
                try:
                    self.health.set_frozen(self.playback_active)
                    if self.health_interval_override:
                        self.health.interval = self.health_interval_override
                    else:
                        self.health.interval = int(
                            self.settings.get("health_interval", 30) or 30)
                    self.health.tick(now)
                except Exception as e:
                    self.log("health check error: %s" % e, "warn")
            return

        if self.bin.proc is not None:
            code = self.bin.proc.returncode
            self.bin.proc = None
            if self._shutting_down or self.should_stop():
                self.begin_shutdown()
                self.log("%s exited during shutdown (code %s)"
                         % (self.bin.engine, code), "info")
                return
            if self._was_running:
                self._was_running = False
                self._write_state()
                self.notify("%s proxy stopped (code %s)" % (self.bin.engine, code), error=True)
            self.consecutive_failures += 1
            delay = min(2 ** self.consecutive_failures, 60)
            self._restart_at = now + delay
            self.log("%s exited (code %s); restart #%d in %ds"
                     % (self.bin.engine, code, self.consecutive_failures, delay), "warn")
            return

        if self._restart_at is None:
            return
        if self._shutting_down or self.should_stop():
            self.begin_shutdown()
            return
        if self.consecutive_failures > 10:
            if self.consecutive_failures == 11:
                self.log("too many restart failures; giving up until settings change", "error")
            self._restart_at = None
            return
        if now >= self._restart_at:
            self._restart_at = None
            try:
                self.bin.start(self.config_path, port=self.effective_port)
            except Exception as e:
                self.log("restart failed: %s" % e, "error")

    def _watch_active_change(self):
        """Notify when the active profile changed (manual mode user switch)."""
        tag = self.store.active_tag
        if tag and self._last_active_tag and tag != self._last_active_tag:
            self.notify("Active profile: %s" % tag)
        if tag:
            self._last_active_tag = tag

    # ----- subscription refresh -------------------------------------
    def _maybe_refresh_subscriptions(self, now):
        """Refresh due subscription groups, once per tick, never re-entered."""
        interval = int(self.settings.get("subscription_interval_hours", 0) or 0)
        if not interval or self._refreshing_subscriptions:
            return
        self._refreshing_subscriptions = True
        try:
            refresher = self.refresh_subscriptions or self._refresh_due
            changed = refresher(now, interval)
            if changed:
                self._apply_subscription_changes()
        except Exception as e:
            self.log("subscription refresh failed: %s" % e, "error")
        finally:
            self._refreshing_subscriptions = False

    def _refresh_due(self, now, interval_hours):
        """Real refresh of every due group; returns True when profiles changed."""
        import helpers
        import subscriptions

        store = subscriptions.SubscriptionStore(
            os.path.join(self.work_dir, "subscriptions.json"))
        changed = False
        for group in store.due(now, interval_hours):
            added, removed, err = store.refresh(
                group["id"],
                disabled_protocols=helpers.disabled_protocols(),
                profile_store=self.store)
            if err:
                self.log("subscription %s refresh failed: %s"
                         % (group["url"], err), "warn")
            elif added or removed:
                changed = True
        return changed

    def _apply_subscription_changes(self):
        """Re-pick the active profile and rebuild the engine config.

        When the watchdog already armed a restart, only rebuild the config:
        the pending restart fires on its own backoff schedule and must not be
        preempted by an early start.
        """
        if not self.store.enabled():
            self.log("no enabled profiles after subscription refresh", "warn")
            return
        if self.playback_active:
            self._pending_reload = True
            self.log("subscription changes deferred until playback stops")
            return
        if self.bin.is_running():
            self.reconfigure_engine()
        elif self._restart_at is not None:
            self.build_and_write_config()
        elif self.settings.get("autostart"):
            self._start_with_port()

    def reload_profiles(self):
        """Re-read profiles.json (called when the UI changed profiles)."""
        self.store.load()

    def status(self):
        return {
            "running": self.bin.is_running(),
            "engine": self.bin.engine,
            "platform": self.bin.platform,
            "mode": self.settings.get("mode"),
            "port": self.effective_port or self.settings.get("local_port"),
            "profiles": len(self.store.profiles),
            "enabled": len(self.store.enabled()),
            "active": self.store.active_tag,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }
