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


def _is_android():
    try:
        import xbmc  # noqa
        return xbmc.getCondVisibility("system.platform.android")
    except Exception:
        return hasattr(sys, "getandroidapilevel") or (
            "ANDROID_ROOT" in os.environ or "ANDROID_DATA" in os.environ
        )


def _read_cpuinfo_model():
    """Return first 'model name'/'Processor' value from /proc/cpuinfo, lowercased."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, val = line.split(":", 1)
                key = key.strip().lower()
                if key in ("model name", "processor", "hardware"):
                    v = val.strip().lower()
                    if v:
                        return v
    except Exception:
        pass
    return ""


def _linux_arch():
    machine = platform.machine().lower()
    is64 = sys.maxsize > 2 ** 32

    if machine in ("x86_64", "amd64"):
        return "linux_x64"
    if machine in ("i386", "i486", "i586", "i686", "x86"):
        return "linux_x86"

    # ARM family
    if machine.startswith(("arm", "aarch")):
        if "aarch64" in machine or "arm64" in machine:
            return "linux_arm64" if is64 else "linux_armv7"
        if "armv7" in machine or "v7l" in machine:
            return "linux_armv7"
        if "armv6" in machine or "v6l" in machine:
            return "linux_armv6"
        # Generic 'arm' / 'armv7l' fallback: inspect cpuinfo
        model = _read_cpuinfo_model()
        if "aarch" in model or "arm64" in model or "v8" in model:
            return "linux_arm64" if is64 else "linux_armv7"
        if "armv7" in model or "v7l" in model:
            return "linux_armv7"
        if "armv6" in model or "v6l" in model:
            return "linux_armv6"
        # LibreELEC RPi2/3 reports armv7l -> armv7; bare 'arm' assume armv7 hard-float
        return "linux_armv7" if "hf" in (platform.libc_ver()[1] or "") or True else "linux_armv6"

    return "linux_x64" if is64 else "linux_x86"


def get_platform(override="auto"):
    """Return internal platform name, e.g. 'linux_x64', 'linux_armv7'.

    override: 'auto' or an explicit internal name like 'linux_armv7'.
    """
    if override and override.lower() != "auto":
        return override if override in _SUPPORTED else override

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
        raise ValueError("Unsupported platform: %s" % platform_name)
    return "sing-box-%s-%s" % (version, suffix)


def asset_url(platform_name, version):
    """Download URL for the sing-box release tarball for a platform."""
    name = asset_name(platform_name, version)
    ext = "zip" if platform_name.startswith("windows") else "tar.gz"
    return ("https://github.com/SagerNet/sing-box/releases/download/"
            "v%s/%s.%s" % (version, name, ext))


def binary_filename(platform_name):
    return "sing-box.exe" if platform_name.startswith("windows") else "sing-box"


def is_supported(platform_name):
    return platform_name in _SUPPORTED


PLATFORM = get_platform()
