#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Advanced Proxy — Kodi service entry point.

Runs a bundled proxy engine (sing-box / Xray) as a local proxy, driven by the
profiles the user adds via links. Honors autostart, switching mode (urltest /
manual), port, and shows notifications on profile switch / connection loss.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "src"))

import xbmc  # noqa: E402
import xbmcgui  # noqa: E402

from src import helpers, proxy_integration, supervisor  # noqa: E402

ADDON_ID = helpers.ADDON_ID
ADDON_NAME = "Advanced Proxy"
INTEGRATION_HOST = "127.0.0.1"


def _xbmc_log(msg, level="info"):
    lvl = {"debug": xbmc.LOGDEBUG, "info": xbmc.LOGINFO,
           "warn": xbmc.LOGWARNING, "error": xbmc.LOGERROR}.get(level, xbmc.LOGINFO)
    xbmc.log("[%s] %s" % (ADDON_ID, msg), lvl)


def _profiles_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def build_integration_manager(logger=None, notify=None):
    """IntegrationManager bound to the Kodi adapters from helpers.

    The manager asks ``addon_available()`` without arguments, so the YouTube
    addon id is bound here.
    """
    return proxy_integration.IntegrationManager(
        backup_path=helpers.integration_backup_path(),
        read_kodi=helpers.read_kodi_proxy_setting,
        write_kodi=helpers.write_kodi_proxy_setting,
        addon_available=lambda: helpers.addon_available(
            proxy_integration.YOUTUBE_ADDON_ID),
        read_addon=helpers.read_addon_setting,
        write_addon=helpers.write_addon_setting,
        logger=logger,
        notify=notify,
    )


class IntegrationLifecycle(object):
    """Decides when Kodi's proxy settings follow the engine, and when they
    are handed back.

    Every manager call is guarded: aligning external settings is a
    convenience, so a broken JSON-RPC or addon must be reported without ever
    taking sing-box/Xray down — including at startup and shutdown, which run
    outside the service loop's own error handling.
    """

    def __init__(self, manager, logger=None, notify=None,
                 host=INTEGRATION_HOST):
        self.manager = manager
        self._logger = logger
        self._notify = notify
        self._host = host

    def sync(self, enabled, running, port):
        """Align external proxy settings with the current engine state.

        Applies only while the integration is enabled and the engine is
        actually up on ``port``; otherwise any backup left by a previous run
        is handed back. Returns True when the settings were applied.
        """
        if not enabled:
            return self._stand_down(port)
        if not running or not port:
            self._restore("proxy not running")
            return False
        return self._apply(port)

    def shutdown(self):
        """Hand the previous settings back; call before stopping the engine."""
        return self._restore("shutting down")

    def _apply(self, port):
        if self._call("ensure_configured", self._host, port):
            return True
        self._log("integration: could not configure %s:%s" % (self._host, port),
                  "warn")
        self._notify_error("Could not configure Kodi proxy")
        return False

    def _stand_down(self, port):
        if not self._call("backup_exists"):
            return False
        self._restore("integration disabled")
        if port and self._call("validate", self._host, port):
            self._log("integration: %s:%s still configured after restore"
                      % (self._host, port), "warn")
        return False

    def _restore(self, reason):
        if not self._call("backup_exists"):
            return False
        self._log("integration: %s; restoring previous proxy settings" % reason)
        if self._call("restore_previous"):
            return True
        self._notify_error("Could not restore Kodi proxy settings")
        return False

    def _call(self, method, *args):
        try:
            return getattr(self.manager, method)(*args)
        except Exception as e:
            self._log("integration: %s failed: %s" % (method, e), "error")
            self._notify_error("Proxy integration failed")
            return False

    def _log(self, msg, level="info"):
        if self._logger is None:
            return
        try:
            self._logger(msg, level)
        except Exception:
            pass

    def _notify_error(self, msg):
        if self._notify is None:
            return
        try:
            self._notify(msg, True)
        except Exception:
            pass


def main():
    settings = helpers.get_settings()

    notify_enabled = {"v": settings.get("notify", True)}

    def _notify(msg, error=False):
        if not notify_enabled["v"]:
            return
        icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
        xbmcgui.Dialog().notification(ADDON_NAME, msg, icon, 3000)

    work_dir = helpers.profile_dir()
    monitor = xbmc.Monitor()
    sup = supervisor.ProxySupervisor(
        settings=settings,
        addon_dir=helpers.addon_dir(),
        work_dir=work_dir,
        logger=_xbmc_log,
        notify=_notify,
        should_stop=monitor.abortRequested,
    )

    integration = IntegrationLifecycle(
        build_integration_manager(_xbmc_log, _notify), _xbmc_log, _notify)

    geo_status = helpers.sync_geo_databases(settings)
    for name, status in geo_status.items():
        if status == "ok":
            _xbmc_log("geo %s database updated" % name)
        elif status != "skipped":
            _xbmc_log("geo %s database update failed: %s" % (name, status),
                      "warn")

    def _sync_integration(current, running):
        integration.sync(current.get("auto_configure_integration", True),
                         running,
                         sup.effective_port)

    started = False
    if settings.get("autostart") and sup.store.enabled():
        if sup.start():
            started = True
            _xbmc_log("proxy autostarted: %s on 127.0.0.1:%s (%s)"
                      % (sup.bin.engine, settings["local_port"], settings["mode"]))
        else:
            _xbmc_log("autostart failed: %s" % sup.last_error, "error")
    elif not sup.store.enabled():
        _xbmc_log("no profiles configured; open addon settings to add links", "warn")
    else:
        _xbmc_log("autostart disabled; proxy idle")

    _sync_integration(settings, started)

    prev_settings = settings
    prev_profiles_mtime = _profiles_mtime(helpers.profiles_path())

    while not monitor.abortRequested():
        try:
            new_settings = helpers.get_settings()
            notify_enabled["v"] = new_settings.get("notify", True)

            engine_or_mode_changed = (
                new_settings.get("engine") != prev_settings.get("engine") or
                new_settings.get("mode") != prev_settings.get("mode") or
                new_settings.get("local_port") != prev_settings.get("local_port"))
            integration_changed = (
                new_settings.get("auto_configure_integration") !=
                prev_settings.get("auto_configure_integration"))

            if new_settings != prev_settings:
                prev_settings = new_settings
                sup.settings.update(new_settings)
                sup.settings.setdefault("log_path", helpers.log_path())
                if engine_or_mode_changed:
                    _xbmc_log("engine/mode/port changed, reconfiguring")
                    started = sup.reconfigure_engine()
                    _sync_integration(new_settings, started)
                elif integration_changed:
                    _sync_integration(new_settings, sup.bin.is_running())

            # profiles changed via UI -> reload and apply
            mtime = _profiles_mtime(helpers.profiles_path())
            if mtime != prev_profiles_mtime:
                prev_profiles_mtime = mtime
                _xbmc_log("profiles changed, reloading")
                sup.reload_profiles()
                if sup.bin.is_running():
                    sup.restart()
                elif new_settings.get("autostart") and sup.store.enabled():
                    started = sup.start()
                    _sync_integration(new_settings, started)

            if sup.bin.is_running() or sup.store.enabled():
                sup.tick()
        except Exception as e:
            _xbmc_log("loop error: %s" % e, "error")

        if monitor.waitForAbort(3):
            sup.begin_shutdown()
            break

    _xbmc_log("shutting down proxy")
    sup.begin_shutdown()
    integration.shutdown()
    sup.stop()


if __name__ == "__main__":
    main()
