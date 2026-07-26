# -*- coding: utf-8 -*-
"""Helpers: read addon settings + resolve paths.

The ONLY module that touches xbmc* APIs, keeping the rest Kodi-free and
unit-testable. In tests, `get_settings` is driven by an injected reader.
"""
import os

ADDON_ID = "service.advancedproxy"

_DEFAULTS = {
    "engine": "0",                    # 0 sing-box, 1 xray
    "autostart": "true",
    "notify": "true",
    "local_port": "1080",
    "mode": "0",                      # 0 urltest, 1 manual
    "urltest_interval": "3m",
    "urltest_tolerance": "50",
    "test_url": "https://www.gstatic.com/generate_204",
    "interrupt_connections": "true",
    "skip_protocols": "trojan,xhttp",
    "log_level": "1",
    "binary_platform_override": "auto",
    "binary_custom_path": "",
}

_LOG_LEVELS = {"0": "debug", "1": "info", "2": "warn", "3": "error"}
_ENGINES = {"0": "sing-box", "1": "xray"}
_MODES = {"0": "urltest", "1": "manual"}


def _read_kodi_settings():
    import xbmcaddon
    addon = xbmcaddon.Addon(ADDON_ID)
    return {k: addon.getSetting(k) for k in _DEFAULTS}


def get_settings(reader=None):
    try:
        raw = (reader or _read_kodi_settings)()
    except Exception:
        raw = {}

    def s(key):
        v = raw.get(key)
        return v if (v is not None and v != "") else _DEFAULTS[key]

    def b(key):
        return str(s(key)).lower() in ("true", "1", "yes", "on")

    def i(key):
        try:
            return int(str(s(key)))
        except (TypeError, ValueError):
            return int(_DEFAULTS[key])

    return {
        "engine": _ENGINES.get(str(s("engine")), "sing-box"),
        "autostart": b("autostart"),
        "notify": b("notify"),
        "local_port": i("local_port"),
        "mode": _MODES.get(str(s("mode")), "urltest"),
        "urltest_interval": s("urltest_interval"),
        "urltest_tolerance": i("urltest_tolerance"),
        "test_url": s("test_url"),
        "interrupt_connections": b("interrupt_connections"),
        "skip_protocols": s("skip_protocols"),
        "log_level": _LOG_LEVELS.get(str(s("log_level")), "info"),
        "binary_platform_override": s("binary_platform_override"),
        "binary_custom_path": s("binary_custom_path"),
    }


def addon_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def profile_dir():
    try:
        import xbmcvfs
        path = xbmcvfs.translatePath(
            "special://profile/addon_data/%s/" % ADDON_ID)
        if path:
            os.makedirs(path, exist_ok=True)
            return path
    except Exception:
        pass
    path = os.path.join(os.path.expanduser("~"), ".kodi-advancedproxy")
    os.makedirs(path, exist_ok=True)
    return path


def profiles_path():
    return os.path.join(profile_dir(), "profiles.json")


def config_path():
    return os.path.join(profile_dir(), "engine.json")


def log_path():
    return os.path.join(profile_dir(), "engine.log")
