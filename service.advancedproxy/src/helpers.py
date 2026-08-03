# -*- coding: utf-8 -*-
"""Helpers: read addon settings + resolve paths.

The ONLY module that touches xbmc* APIs, keeping the rest Kodi-free and
unit-testable. In tests, `get_settings` is driven by an injected reader.
"""
import json
import os
import socket
import threading
import time
import urllib.parse

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
    "subscription_interval_hours": "0",
    "disable_proto_vless": "false",
    "disable_proto_trojan": "false",
    "disable_proto_hysteria2": "false",
    "log_level": "1",
    "binary_platform_override": "auto",
    "binary_custom_path": "",
    "auto_configure_integration": "true",
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
        "subscription_interval_hours": i("subscription_interval_hours"),
        "disable_proto_vless": b("disable_proto_vless"),
        "disable_proto_trojan": b("disable_proto_trojan"),
        "disable_proto_hysteria2": b("disable_proto_hysteria2"),
        "log_level": _LOG_LEVELS.get(str(s("log_level")), "info"),
        "binary_platform_override": s("binary_platform_override"),
        "binary_custom_path": s("binary_custom_path"),
        "auto_configure_integration": b("auto_configure_integration"),
    }


def disabled_protocols(reader=None):
    """Tuple of protocols to skip: toggles merged with legacy skip list.

    The legacy `skip_protocols` value counts only when it was explicitly set
    (present in the raw reader output), so a fresh install does not inherit
    the old default silently.
    """
    raw = (reader or _read_kodi_settings)() or {}
    toggles = []
    for key, protocol in (("disable_proto_vless", "vless"),
                          ("disable_proto_trojan", "trojan"),
                          ("disable_proto_hysteria2", "hysteria2")):
        if str(raw.get(key, "false")).lower() in ("true", "1"):
            toggles.append(protocol)
    legacy = []
    if raw.get("skip_protocols"):
        legacy = [part.strip() for part in
                  str(raw["skip_protocols"]).split(",") if part.strip()]
    return tuple(dict.fromkeys(toggles + legacy))


def copy_to_clipboard(text):
    """Copy TEXT to the Kodi clipboard; falls back to a text-view dialog."""
    try:
        import xbmc
        xbmc.Keyboard(text).setText(text)
        return True
    except Exception:
        try:
            import xbmcgui
            xbmcgui.Dialog().textviewer("Advanced Proxy", text)
        except Exception:
            pass
        return False


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


def state_path():
    return os.path.join(profile_dir(), "state.json")


def read_proxy_state():
    """Read the runtime snapshot written by the supervisor, or None.

    Kodi-free: used by the UI (default.py) to show the actual local port and
    engine state without duplicating supervisor logic.
    """
    try:
        with open(state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def parse_plugin_args(argv):
    handle = -1
    query = ""
    if len(argv) > 1:
        h = argv[1]
        if h.lstrip("-").isdigit():
            handle = int(h)
        if len(argv) > 2:
            query = argv[2]
            if query.startswith("?"):
                query = query[1:]
    params = dict(urllib.parse.parse_qsl(query)) if query else {}
    return handle, params


def _real_prober(host, port, timeout):
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return int((time.time() - t0) * 1000)
    except (socket.error, OSError):
        return None


def measure_latencies(profiles, prober=None, timeout=2.0):
    """Return {tag: ms_or_None} for every profile.

    Disabled profiles are reported as None and are not probed.
    Enabled profiles are probed concurrently so total wall time is capped
    near a single timeout.
    """
    prober = prober or _real_prober
    results = {}
    enabled = [p for p in profiles if p.get("enabled", True)]
    lock = threading.Lock()

    def run(p):
        try:
            ms = prober(p["server"], p["port"], timeout)
        except (socket.error, OSError):
            ms = None
        with lock:
            results[p["tag"]] = ms

    threads = [threading.Thread(target=run, args=(p,)) for p in enabled]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout + 0.5)

    for p in profiles:
        results.setdefault(p["tag"], None)
    return results


def pick_reachable(profiles, preferred_tag, prober=None, timeout=2.0):
    """Pick the first reachable enabled profile, preferring PREFERRED_TAG.

    Returns (tag, None) when a reachable profile exists, else
    (None, "all profiles unreachable").
    """
    prober = prober or _real_prober
    candidates = [p for p in profiles if p.get("enabled", True)]
    ordered = []
    preferred = next((p for p in candidates if p["tag"] == preferred_tag),
                     None)
    if preferred is not None:
        ordered.append(preferred)
    ordered += [p for p in candidates if p is not preferred]
    for p in ordered:
        try:
            ms = prober(p["server"], p["port"], timeout)
        except (socket.error, OSError):
            ms = None
        if ms is not None:
            return p["tag"], None
    return None, "all profiles unreachable"


def build_directory_entries(store, mode, base_url, latencies=None,
                            subscriptions=None):
    latencies = latencies or {}
    entries = [{"kind": "mode_toggle", "mode": mode,
                "url": base_url + "?action=toggle_mode"}]
    if not store.profiles:
        entries.append({"kind": "info",
                        "str_id": 32208,
                        "url": base_url + "?action=add"})
    for p in store.profiles:
        tag_q = urllib.parse.urlencode({"tag": p["tag"]})
        entries.append({
            "kind": "profile",
            "tag": p["tag"],
            "protocol": p["protocol"],
            "enabled": p.get("enabled", True),
            "is_active": p["tag"] == store.active_tag,
            "latency_ms": latencies.get(p["tag"]) if p.get("enabled", True) else None,
            "click_url": base_url + "?action=activate&" + tag_q,
            "toggle_url": base_url + "?action=toggle&" + tag_q,
            "remove_url": base_url + "?action=remove&" + tag_q,
            "copy_url": base_url + "?action=copy&" + tag_q,
        })
    for group in subscriptions or ():
        id_q = urllib.parse.urlencode({"id": group["id"]})
        status = ("error: %s" % group["last_error"]) if group.get("last_error") \
            else ("updated" if group.get("last_updated") else "never")
        entries.append({
            "kind": "subscription",
            "id": group["id"],
            "url": group["url"],
            "status": status,
            "refresh_url": base_url + "?action=sub_refresh&" + id_q,
            "remove_url": base_url + "?action=sub_remove&" + id_q,
        })
    entries.append({"kind": "action", "action": "add", "str_id": 32200,
                    "url": base_url + "?action=add"})
    entries.append({"kind": "action", "action": "test", "str_id": 32202,
                    "url": base_url + "?action=test"})
    if store.profiles:
        entries.append({"kind": "action", "action": "clear", "str_id": 32207,
                        "url": base_url + "?action=clear"})
    entries.append({"kind": "action", "action": "settings", "str_id": 32203,
                    "url": base_url + "?action=settings"})
    return entries


def _execute_jsonrpc(method, params):
    try:
        import xbmc
    except Exception:
        return None
    request = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    try:
        raw = xbmc.executeJSONRPC(json.dumps(request))
    except Exception:
        return None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "result" not in payload:
        return None
    return payload["result"]


def read_kodi_proxy_setting(setting_id):
    result = _execute_jsonrpc("Settings.GetSettingValue",
                              {"setting": setting_id})
    if not isinstance(result, dict) or "value" not in result:
        return None
    return result["value"]


def write_kodi_proxy_setting(setting_id, value):
    result = _execute_jsonrpc("Settings.SetSettingValue",
                              {"setting": setting_id, "value": value})
    return result is True


def addon_available(addon_id):
    try:
        import xbmcaddon
    except Exception:
        return False
    try:
        xbmcaddon.Addon(addon_id)
        return True
    except Exception:
        return False


def _coerce_addon_value(raw):
    if raw is None:
        return None
    low = str(raw).strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(low)
    except (TypeError, ValueError):
        return raw


def read_addon_setting(addon_id, setting_id):
    try:
        import xbmcaddon
        addon = xbmcaddon.Addon(addon_id)
        if setting_id == "requests.proxy.source":
            return addon.getSettingInt(setting_id)
        return _coerce_addon_value(addon.getSetting(setting_id))
    except Exception:
        return None


def write_addon_setting(addon_id, setting_id, value):
    try:
        import xbmcaddon
        addon = xbmcaddon.Addon(addon_id)
    except Exception:
        return False
    try:
        if setting_id == "requests.proxy.source":
            addon.setSettingInt(setting_id, int(value))
        elif isinstance(value, bool):
            addon.setSettingBool(setting_id, value)
        elif isinstance(value, int):
            addon.setSettingInt(setting_id, value)
        else:
            addon.setSetting(setting_id, str(value))
        return True
    except Exception:
        return False


def integration_backup_path():
    return os.path.join(profile_dir(), "integration_backup.json")
