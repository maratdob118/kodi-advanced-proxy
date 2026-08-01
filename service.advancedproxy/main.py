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

from src import helpers, supervisor  # noqa: E402

ADDON_ID = helpers.ADDON_ID
ADDON_NAME = "Advanced Proxy"


def _xbmc_log(msg, level="info"):
    lvl = {"debug": xbmc.LOGDEBUG, "info": xbmc.LOGINFO,
           "warn": xbmc.LOGWARNING, "error": xbmc.LOGERROR}.get(level, xbmc.LOGINFO)
    xbmc.log("[%s] %s" % (ADDON_ID, msg), lvl)


def _profiles_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def main():
    settings = helpers.get_settings()

    notify_enabled = {"v": settings.get("notify", True)}

    def _notify(msg, error=False):
        if not notify_enabled["v"]:
            return
        icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
        xbmcgui.Dialog().notification(ADDON_NAME, msg, icon, 3000)

    work_dir = helpers.profile_dir()
    sup = supervisor.ProxySupervisor(
        settings=settings,
        addon_dir=helpers.addon_dir(),
        work_dir=work_dir,
        logger=_xbmc_log,
        notify=_notify,
    )

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

    monitor = xbmc.Monitor()
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

            if new_settings != prev_settings:
                prev_settings = new_settings
                sup.settings.update(new_settings)
                sup.settings.setdefault("log_path", helpers.log_path())
                if engine_or_mode_changed:
                    _xbmc_log("engine/mode/port changed, reconfiguring")
                    started = sup.reconfigure_engine()

            # profiles changed via UI -> reload and apply
            mtime = _profiles_mtime(helpers.profiles_path())
            if mtime != prev_profiles_mtime:
                prev_profiles_mtime = mtime
                _xbmc_log("profiles changed, reloading")
                sup.reload_profiles()
                if sup.bin.is_running():
                    sup.restart()
                elif new_settings.get("autostart") and sup.store.enabled():
                    sup.start()

            if sup.bin.is_running() or sup.store.enabled():
                sup.tick()
        except Exception as e:
            _xbmc_log("loop error: %s" % e, "error")

        if monitor.waitForAbort(3):
            break

    _xbmc_log("shutting down proxy")
    sup.stop()


if __name__ == "__main__":
    main()
