# -*- coding: utf-8 -*-
"""Helpers: read addon settings + resolve paths.

This is the ONLY module that touches xbmc* APIs, keeping the rest of the
codebase Kodi-free and unit-testable. In tests, `get_settings` is replaced by
a plain dict / fake.
"""
import os

ADDON_ID = "service.advancedproxy"

_DEFAULTS = {
    "subscription_url": "",
    "skip_protocols": "trojan,xhttp",
    "local_port": "1080",
    "lan_mixed_enabled": "false",
    "lan_mixed_port": "1080",
    "urltest_interval": "3m",
    "urltest_tolerance": "50",
    "interrupt_connections": "true",
    "test_url": "https://www.gstatic.com/generate_204",
    "log_level": "1",          # 0 debug 1 info 2 warn 3 error
    "binary_platform_override": "auto",
}

_LOG_LEVELS = {"0": "debug", "1": "info", "2": "warn", "3": "error"}


def _read_kodi_settings():
    import xbmcaddon
    addon = xbmcaddon.Addon(ADDON_ID)
    return {k: addon.getSetting(k) for k in _DEFAULTS}


def get_settings(reader=None):
    """Return a normalized settings dict (typed values).

    reader: optional callable returning a raw {key: str} dict (defaults to
    reading via xbmcaddon). Lets tests inject settings without Kodi.
    """
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
        "subscription_url": s("subscription_url"),
        "skip_protocols": s("skip_protocols"),
        "local_port": i("local_port"),
        "lan_mixed_enabled": b("lan_mixed_enabled"),
        "lan_mixed_port": i("lan_mixed_port"),
        "urltest_interval": s("urltest_interval"),
        "urltest_tolerance": i("urltest_tolerance"),
        "interrupt_connections": b("interrupt_connections"),
        "test_url": s("test_url"),
        "log_level": _LOG_LEVELS.get(str(s("log_level")), "info"),
        "binary_platform_override": s("binary_platform_override"),
    }


def addon_dir():
    """Directory containing addon.xml (the addon root)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def profile_dir():
    """Writable addon profile dir for configs/logs/binary copies."""
    try:
        import xbmcvfs
        path = xbmcvfs.translatePath(
            "special://profile/addon_data/%s/" % ADDON_ID)
        if path and os.path.isdir(os.path.dirname(path)):
            os.makedirs(path, exist_ok=True)
            return path
    except Exception:
        pass
    # Fallback for local testing outside Kodi
    path = os.path.join(os.path.expanduser("~"), ".kodi-advancedproxy")
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    return os.path.join(profile_dir(), "sing-box.json")


def log_path():
    return os.path.join(profile_dir(), "sing-box.log")


def pid_path():
    return os.path.join(profile_dir(), "sing-box.pid")
