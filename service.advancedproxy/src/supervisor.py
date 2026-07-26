# -*- coding: utf-8 -*-
"""ProxySupervisor: generate config, run sing-box, keep it alive.

Kodi-free. Driven by calling `tick()` periodically from an outer loop
(Kodi monitor loop or a test harness). Owns no threads of its own.
"""
import json
import os
import time

import binary_manager
import config_gen


def _default_log(msg, level="info"):
    pass


class ProxySupervisor(object):
    def __init__(self, settings, addon_dir, work_dir, logger=None):
        self.settings = dict(settings)
        self.log = logger or _default_log
        self.work_dir = work_dir
        self.config_path = os.path.join(work_dir, "sing-box.json")
        self.settings.setdefault("log_path", os.path.join(work_dir, "sing-box.log"))
        self.bin = binary_manager.BinaryManager(
            addon_dir=addon_dir,
            work_dir=work_dir,
            platform_override=self.settings.get("binary_platform_override", "auto"),
            logger=self.log,
        )
        self.last_reload = 0.0
        self.reload_interval = 180.0  # re-pull subscription every 3 min
        self.consecutive_failures = 0
        self.last_error = None
        self._restart_at = None

    # ----- config ----------------------------------------------------
    def build_and_write_config(self):
        """Generate config from subscription, validate, write. Returns True on success."""
        sub = self.settings.get("subscription_url")
        if not sub:
            self.log("subscription_url is empty; nothing to run", "warn")
            return False
        try:
            config, stats = config_gen.generate(sub, self.settings)
        except Exception as e:
            self.last_error = "subscription fetch/parse failed: %s" % e
            self.log(self.last_error, "error")
            return False

        tmp = self.config_path + ".new"
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)

        ok, out = self.bin.check(tmp)
        if not ok:
            self.last_error = "sing-box check failed: %s" % out.strip()[:300]
            self.log(self.last_error, "error")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

        os.replace(tmp, self.config_path)
        self.last_error = None
        self.log("config written: %d outbounds, %d skipped" %
                 (stats["used"], len(stats["skipped"])))
        return True

    # ----- lifecycle -------------------------------------------------
    def start(self):
        if not self.build_and_write_config():
            return False
        try:
            self.bin.start(self.config_path)
        except Exception as e:
            self.last_error = "failed to start sing-box: %s" % e
            self.log(self.last_error, "error")
            return False
        self.last_reload = time.time()
        self.consecutive_failures = 0
        return True

    def stop(self):
        self.bin.stop()

    def restart(self):
        self.bin.restart(self.config_path)
        self.last_reload = time.time()

    # ----- tick ------------------------------------------------------
    def tick(self):
        """Called periodically. Keeps sing-box alive and config fresh."""
        now = time.time()

        if self.bin.is_running():
            self.consecutive_failures = 0
            self._restart_at = None
            if now - self.last_reload >= self.reload_interval:
                self.log("refreshing subscription config")
                if self.build_and_write_config():
                    self.restart()
                else:
                    self.last_reload = now
            return

        if self.bin.proc is not None:
            code = self.bin.proc.returncode
            self.consecutive_failures += 1
            self.bin.proc = None
            delay = min(2 ** self.consecutive_failures, 60)
            self._restart_at = now + delay
            self.log("sing-box exited (code %s); restart #%d in %ds"
                     % (code, self.consecutive_failures, delay), "warn")
            return

        if self._restart_at is None:
            return
        if self.consecutive_failures > 10:
            if self.consecutive_failures == 11:
                self.log("too many restart failures; giving up until settings change",
                         "error")
            self._restart_at = None
            return
        if now >= self._restart_at:
            self._restart_at = None
            try:
                self.bin.start(self.config_path)
            except Exception as e:
                self.log("restart failed: %s" % e, "error")

    def status(self):
        return {
            "running": self.bin.is_running(),
            "platform": self.bin.platform,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "config_path": self.config_path,
        }
