#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Advanced Proxy (sing-box) — Kodi service entry point.

Starts a bundled sing-box as a local proxy and keeps it alive, refreshing the
config from the subscription on a timer. Point Kodi's proxy settings to the
local SOCKS5 (10808) or HTTP (10809) port.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "src"))

import xbmc  # noqa: E402

from src import helpers, supervisor  # noqa: E402

ADDON_ID = helpers.ADDON_ID


def _xbmc_log(msg, level="info"):
    lvl = {
        "debug": xbmc.LOGDEBUG,
        "info": xbmc.LOGINFO,
        "warn": xbmc.LOGWARNING,
        "error": xbmc.LOGERROR,
    }.get(level, xbmc.LOGINFO)
    xbmc.log("[%s] %s" % (ADDON_ID, msg), lvl)


def main():
    settings = helpers.get_settings()
    if not settings.get("subscription_url"):
        _xbmc_log("subscription_url not set; proxy disabled. Configure the addon.", "warn")

    work_dir = helpers.profile_dir()
    sup = supervisor.ProxySupervisor(
        settings=settings,
        addon_dir=helpers.addon_dir(),
        work_dir=work_dir,
        logger=_xbmc_log,
    )

    if settings.get("subscription_url"):
        if sup.start():
            _xbmc_log("sing-box proxy started on 127.0.0.1:%s (mixed, platform %s)" % (
                settings["local_port"], sup.bin.platform))
        else:
            _xbmc_log("failed to start: %s" % sup.last_error, "error")

    monitor = xbmc.Monitor()
    prev_settings = settings
    while not monitor.abortRequested():
        # re-read settings so user edits take effect
        try:
            new_settings = helpers.get_settings()
            if new_settings != prev_settings:
                prev_settings = new_settings
                _xbmc_log("settings changed, applying")
                sup.settings.update(new_settings)
                sup.settings.setdefault("log_path", helpers.log_path())
                if sup.bin.is_running():
                    sup.restart()
                elif new_settings.get("subscription_url"):
                    sup.start()
        except Exception as e:
            _xbmc_log("settings refresh error: %s" % e, "error")

        if sup.settings.get("subscription_url"):
            try:
                sup.tick()
            except Exception as e:
                _xbmc_log("tick error: %s" % e, "error")

        if monitor.waitForAbort(5):
            break

    _xbmc_log("shutting down sing-box")
    sup.stop()


if __name__ == "__main__":
    main()
