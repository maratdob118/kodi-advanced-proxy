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
import port_utils
import profiles


def _default_log(msg, level="info"):
    pass


def _default_notify(msg, error=False):
    pass


class ProxySupervisor(object):
    def __init__(self, settings, addon_dir, work_dir, logger=None, notify=None):
        self.settings = dict(settings)
        self.log = logger or _default_log
        self.notify = notify or _default_notify
        self.addon_dir = addon_dir
        self.work_dir = work_dir
        self.config_path = os.path.join(work_dir, "engine.json")
        self.state_path = os.path.join(work_dir, "state.json")
        self.settings.setdefault("log_path", os.path.join(work_dir, "engine.log"))
        self.store = profiles.ProfileStore(
            os.path.join(work_dir, "profiles.json"))
        self.bin = self._make_binary_manager()
        self.last_reload = 0.0
        self.reload_interval = 180.0
        self.consecutive_failures = 0
        self.last_error = None
        self._restart_at = None
        self._was_running = False
        self._last_active_tag = None
        self.effective_port = None

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
        port = port_utils.find_free_port(preferred)
        self.effective_port = port
        if port != preferred:
            self.log("port %d is busy, using %d instead"
                     % (preferred, port), "warn")
            self.notify("Port %d busy, proxy on %d" % (preferred, port),
                        error=True)
        return port

    def _build_settings(self):
        """Settings copy with the effective port injected for config gen."""
        s = dict(self.settings)
        if self.effective_port:
            s["local_port"] = self.effective_port
        return s

    def build_and_write_config(self):
        enabled = self.store.enabled()
        if not enabled:
            self.last_error = "no enabled profiles"
            self.log(self.last_error, "warn")
            return False
        try:
            engine = self.settings.get("engine", "sing-box")
            active = self.store.active_tag
            build_settings = self._build_settings()
            if engine == "xray":
                config, skipped = build_xray.build_config(enabled, build_settings, active)
            else:
                config, skipped = build_singbox.build_config(enabled, build_settings, active)
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

    def start(self):
        self._resolve_effective_port()
        if not self.build_and_write_config():
            self._write_state()
            return False
        try:
            self.bin.start(self.config_path)
        except Exception as e:
            self.last_error = "failed to start %s: %s" % (self.bin.engine, e)
            self.log(self.last_error, "error")
            self._write_state()
            return False
        self.last_reload = time.time()
        self.consecutive_failures = 0
        self._was_running = True
        self._last_active_tag = self.store.active_tag
        self._write_state()
        return True

    def stop(self):
        self.bin.stop()
        self._was_running = False
        self._write_state()

    def restart(self):
        """Rebuild config from current profiles/settings, then restart the engine."""
        ok = self.build_and_write_config()
        self.last_reload = time.time()
        if not ok:
            self.log("restart: config build failed, keeping current process", "warn")
            return
        self.bin.restart(self.config_path)
        self._last_active_tag = self.store.active_tag

    def reconfigure_engine(self):
        """Rebuild self.bin after engine/mode/port settings changed.

        Stops the currently running process through the OLD BinaryManager
        instance before swapping in a new one, so the old process releases
        its port before a new one tries to bind it. Replacing self.bin first
        would silently orphan the old process (it holds the port forever).
        """
        was_running = self.bin.is_running()
        if was_running:
            self.bin.stop()
        self.bin = self._make_binary_manager()
        self._resolve_effective_port()
        if was_running or (self.settings.get("autostart") and self.store.enabled()):
            self.start()
        return self.bin.is_running()

    # ----- tick ------------------------------------------------------
    def tick(self):
        now = time.time()

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
            if now - self.last_reload >= self.reload_interval:
                self.log("refreshing config")
                self.restart()
            return

        if self.bin.proc is not None:
            code = self.bin.proc.returncode
            self.bin.proc = None
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
        if self.consecutive_failures > 10:
            if self.consecutive_failures == 11:
                self.log("too many restart failures; giving up until settings change", "error")
            self._restart_at = None
            return
        if now >= self._restart_at:
            self._restart_at = None
            try:
                self.bin.start(self.config_path)
            except Exception as e:
                self.log("restart failed: %s" % e, "error")

    def _watch_active_change(self):
        """Notify when the active profile changed (manual mode user switch)."""
        tag = self.store.active_tag
        if tag and self._last_active_tag and tag != self._last_active_tag:
            self.notify("Active profile: %s" % tag)
        if tag:
            self._last_active_tag = tag

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
