# -*- coding: utf-8 -*-
"""Platform / architecture detection for sing-box binary selection.

Kodi-free: uses only stdlib so it can be unit tested outside Kodi.
Android detection uses xbmc.getCondVisibility when available, but degrades
gracefully to pure-platform heuristics when xbmc is absent.

Internal platform names map to sing-box GitHub release asset names.
"""
import os
import platform
import sys

# internal_name -> sing-box release asset suffix
SINGBOX_ASSET = {
    "linux_x64": "linux-amd64",
    "linux_x86": "linux-386",
    "linux_arm64": "linux-arm64",
    "linux_armv7": "linux-armv7-glibc",
    "linux_armv6": "linux-armv6",
    "linux_armv5": "linux-armv5",
    "android_arm64": "android-arm64",
    "android_arm": "android-arm",
    "windows_x64": "windows-amd64",
    "windows_x86": "windows-386",
    "darwin_x64": "darwin-amd64",
    "darwin_arm64": "darwin-arm64",
}

_SUPPORTED = set(SINGBOX_ASSET.keys())
SUPPORTED = _SUPPORTED


def is_supported(platform_name):
    return platform_name in _SUPPORTED


def _safe_platform_name(value):
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    # Reject path traversal and path-separator characters before any lookup.
    if any(c in value for c in ("/", "\\", "..")):
        return None
    if value in _SUPPORTED:
        return value
    return None


def _is_android():
    try:
        import xbmc  # noqa
        return xbmc.getCondVisibility("system.platform.android")
    except Exception:
        return hasattr(sys, "getandroidapilevel") or (
            "ANDROID_ROOT" in os.environ or "ANDROID_DATA" in os.environ
        )


def _linux_arch():
    machine = platform.machine().lower()
    if machine.startswith("armv7") or machine.startswith("aarch64") and machine == "aarch64_be":
        return "linux_armv7"
    if machine.startswith("armv6"):
        return "linux_armv6"
    if machine.startswith("armv5"):
        return "linux_armv5"
    if machine.startswith("armv8") or machine.startswith("aarch64"):
        return "linux_arm64"
    if machine.startswith("arm"):
        return "linux_armv7"
    if "64" in machine:
        return "linux_x64"
    return "linux_x86"


def get_platform(override="auto"):
    """Return internal platform name, e.g. 'linux_x64', 'linux_armv7'.

    override: 'auto' or an explicit internal name like 'linux_armv7'.
    Invalid overrides (including path traversal or path separators) fall back
    to auto-detection instead of being returned verbatim.
    """
    if override and override.lower() != "auto":
        safe = _safe_platform_name(override)
        if safe:
            return safe

    system = platform.system().lower()
    if system == "windows":
        return "windows_x64" if sys.maxsize > 2 ** 32 else "windows_x86"
    if system == "darwin":
        return "darwin_arm64" if platform.machine().lower() in ("arm64", "aarch64") else "darwin_x64"
    if _is_android():
        return "android_arm64" if sys.maxsize > 2 ** 32 else "android_arm"
    # default linux
    return _linux_arch()


def asset_name(platform_name, version):
    """sing-box release tarball asset filename (without extension)."""
    suffix = SINGBOX_ASSET.get(platform_name)
    if not suffix:
        return None
    return "sing-box-%s-%s" % (version, suffix)


def asset_url(platform_name, version, ext="tar.gz"):
    """Full download URL for a sing-box release asset."""
    name = asset_name(platform_name, version)
    if not name:
        return None
    return "https://github.com/SagerNet/sing-box/releases/download/v%s/%s.%s" % (
        version, name, ext)