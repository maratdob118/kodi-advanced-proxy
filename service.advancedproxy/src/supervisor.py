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

    def _make_binary_manager(self):
        return binary_manager.BinaryManager(
            addon_dir=self.addon_dir,
            work_dir=self.work_dir,
            engine=self.settings.get("engine", "sing-box"),
            platform_override=self.settings.get("binary_platform_override", "auto"),
            logger=self.log,
        )

    # ----- config ----------------------------------------------------
    def build_and_write_config(self):
        enabled = self.store.enabled()
        if not enabled:
            self.last_error = "no enabled profiles"
            self.log(self.last_error, "warn")
            return False
        try:
            engine = self.settings.get("engine", "sing-box")
            active = self.store.active_tag
            if engine == "xray":
                config, skipped = build_xray.build_config(enabled, self.settings, active)
            else:
                config, skipped = build_singbox.build_config(enabled, self.settings, active)
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
    def start(self):
        if not self.build_and_write_config():
            return False
        try:
            self.bin.start(self.config_path)
        except Exception as e:
            self.last_error = "failed to start %s: %s" % (self.bin.engine, e)
            self.log(self.last_error, "error")
            return False
        self.last_reload = time.time()
        self.consecutive_failures = 0
        self._was_running = True
        self._last_active_tag = self.store.active_tag
        return True

    def stop(self):
        self.bin.stop()
        self._was_running = False

    def restart(self):
        self.bin.restart(self.config_path)
        self.last_reload = time.time()
        self._last_active_tag = self.store.active_tag

    # ----- tick ------------------------------------------------------
    def tick(self):
        now = time.time()

        if self.bin.is_running():
            if not self._was_running:
                self._was_running = True
                self.consecutive_failures = 0
                self._restart_at = None
                self.notify("%s proxy up on 127.0.0.1:%s"
                            % (self.bin.engine, self.settings.get("local_port")))
            self._watch_active_change()
            if now - self.last_reload >= self.reload_interval:
                self.log("refreshing config")
                if self.build_and_write_config():
                    self.restart()
                else:
                    self.last_reload = now
            return

        if self.bin.proc is not None:
            code = self.bin.proc.returncode
            self.bin.proc = None
            if self._was_running:
                self._was_running = False
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
            "profiles": len(self.store.profiles),
            "enabled": len(self.store.enabled()),
            "active": self.store.active_tag,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }
