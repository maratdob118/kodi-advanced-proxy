#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Profile manager UI entry (xbmc.python.pluginsource).

Kodi calls default.py with argv[1] = directory handle (int) and argv[2] =
query string (?action=...&tag=...). The root listing shows every configured
profile as a clickable ListItem (click-to-switch in manual mode, warns in
automatic mode) plus Add / Test / Clear / Settings actions. Per-profile
Enable/Disable/Remove are offered through the context menu. Every call always
finishes with xbmcplugin.endOfDirectory so Kodi closes the listing cleanly.
"""
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "src"))

import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402
import xbmcplugin  # noqa: E402

from src import helpers, parsers, profiles  # noqa: E402

ADDON_ID = helpers.ADDON_ID
ADDON_NAME = "Advanced Proxy"
BASE_URL = "plugin://%s/" % ADDON_ID


def _log(msg):
    xbmc.log("[%s] %s" % (ADDON_ID, msg), xbmc.LOGINFO)


def _ls(loc_id):
    return xbmcaddon.Addon(ADDON_ID).getLocalizedString(loc_id)


def _store():
    return profiles.ProfileStore(helpers.profiles_path())


def _settings():
    return helpers.get_settings()


def _mode():
    return _settings().get("mode", "urltest")


def _notify(msg, error=False):
    icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
    xbmcgui.Dialog().notification(ADDON_NAME, msg, icon, 3000)


def _profile_label(e):
    latency = e.get("latency_ms")
    if e["enabled"]:
        latency_text = "%dms" % latency if latency is not None else "timeout"
    else:
        latency_text = "disabled"
    label = "%s (%s) - %s" % (e["tag"], e["protocol"], latency_text)
    if e["is_active"]:
        return "[COLOR lime]%s[/COLOR]" % label
    elif not e["enabled"]:
        return "[COLOR slategrey]%s[/COLOR]" % label
    return label


def _profile_item(e):
    liz = xbmcgui.ListItem(label=_profile_label(e))
    liz.setProperty("isPlayable", "false")
    ctx = []
    if e["enabled"]:
        ctx.append((_ls(32205), "RunPlugin(%s)" % e["toggle_url"]))
    else:
        ctx.append((_ls(32206), "RunPlugin(%s)" % e["toggle_url"]))
    ctx.append((_ls(32204), "RunPlugin(%s)" % e["remove_url"]))
    liz.addContextMenuItems(ctx)
    return liz


def _end(handle, ok):
    if not xbmcplugin.endOfDirectory(handle, succeeded=ok):
        _log("endOfDirectory(handle=%s, ok=%s) returned False" % (handle, ok))


def _show_listing(handle):
    store = _store()
    settings = _settings()
    mode = settings.get("mode", "urltest")
    latencies = helpers.measure_latencies(store.enabled(), timeout=2.0)
    sub_store = _subscription_store()
    entries = helpers.build_directory_entries(store, mode, BASE_URL,
                                              latencies=latencies,
                                              subscriptions=sub_store.groups())

    st = helpers.read_proxy_state()
    if st:
        running = "RUNNING" if st.get("running") else "stopped"
        port = st.get("port") or settings.get("local_port", 1080)
        label = "Proxy: 127.0.0.1:%s (%s, %s, %s)" % (
            port, st.get("engine", "?"), st.get("mode", "?"), running)
        liz = xbmcgui.ListItem(label="[COLOR lime]%s[/COLOR]" % label
                               if st.get("running") else label)
        liz.setProperty("isPlayable", "false")
        xbmcplugin.addDirectoryItem(handle, BASE_URL, liz, isFolder=False)

    for e in entries:
        kind = e["kind"]
        if kind == "profile":
            xbmcplugin.addDirectoryItem(handle, e["click_url"],
                                        _profile_item(e), isFolder=False)
        elif kind == "subscription":
            liz = xbmcgui.ListItem(label="[COLOR orange]%s[/COLOR] (%s)"
                                   % (e["url"], e["status"]))
            liz.setProperty("isPlayable", "false")
            liz.addContextMenuItems([
                (_ls(32227), "RunPlugin(%s)" % e["refresh_url"]),
                (_ls(32228), "RunPlugin(%s)" % e["remove_url"]),
            ])
            xbmcplugin.addDirectoryItem(handle, e["click_url"], liz,
                                        isFolder=False)
        elif kind == "mode_toggle":
            mode_labels = {"urltest": _ls(32108), "manual": _ls(32109),
                           "direct": _ls(32251)}
            mode_label = mode_labels.get(mode, _ls(32108))
            label = _ls(32220) % mode_label
            liz = xbmcgui.ListItem(label=label)
            liz.setProperty("isPlayable", "false")
            xbmcplugin.addDirectoryItem(handle, e["url"], liz, isFolder=False)
        else:
            liz = xbmcgui.ListItem(label=_ls(e["str_id"]))
            liz.setProperty("isPlayable", "false")
            xbmcplugin.addDirectoryItem(handle, e["url"], liz, isFolder=False)
    _end(handle, True)


def _finish_action(handle):
    if handle < 0:
        xbmc.executebuiltin("Container.Refresh")
    else:
        _show_listing(handle)


def _action_add(handle):
    default = (xbmcaddon.Addon(ADDON_ID).getSetting("subscription_url")
               or "").strip()
    kb = xbmcgui.Dialog().input(
        _ls(32201), default, type=xbmcgui.INPUT_ALPHANUM)
    _log("add dialog returned %d chars" % len(kb or ""))
    if kb:
        _action_sub_add(handle, kb)
    else:
        _finish_action(handle)


def _action_test(handle):
    store = _store()
    en = store.enabled()
    if not en:
        _notify(_ls(32218), error=True)
        _finish_action(handle)
        return
    prog = xbmcgui.DialogProgress()
    prog.create(ADDON_NAME, _ls(32217))
    results = []
    try:
        for i, p in enumerate(en):
            if prog.iscanceled():
                break
            prog.update(int(100 * i / len(en)), _ls(32217) + " " + p["tag"])
            ms = helpers._real_prober(p["server"], p["port"], timeout=4)
            results.append((p["tag"], ms))
        prog.update(100)
    finally:
        prog.close()
    lines = ["%s: %s" % (t, ("%d ms" % m) if m is not None else "timeout")
             for t, m in sorted(results, key=lambda r: (r[1] is None, r[1]))]
    xbmcgui.Dialog().textviewer(_ls(32209), "\n".join(lines))
    _finish_action(handle)


def _action_clear(handle):
    store = _store()
    if not store.profiles:
        _finish_action(handle)
        return
    n = len(store.profiles)
    if xbmcgui.Dialog().yesno(ADDON_NAME, _ls(32216) % n):
        store.profiles = []
        store.active_tag = None
        store.save()
    _finish_action(handle)


def _action_activate_reachable(handle, tag):
    """Activate TAG only when reachable; otherwise offer the next one."""
    store = _store()
    tag, err = helpers.pick_reachable(store.profiles, tag)
    if err:
        _notify(_ls(32218), error=True)
        _finish_action(handle)
        return
    if _mode() == "manual":
        if store.set_active(tag):
            _notify(_ls(32212) % tag)
        else:
            _notify(_ls(32219), error=True)
    else:
        if store.set_active(tag):
            _notify(_ls(32221) % tag)
        else:
            _notify(_ls(32219), error=True)
    _finish_action(handle)


def _action_toggle_mode(handle):
    addon = xbmcaddon.Addon(ADDON_ID)
    current = addon.getSetting("mode")
    modes = ["0", "1", "2"]
    current_index = modes.index(current) if current in modes else 0
    next_index = (current_index + 1) % len(modes)
    addon.setSetting("mode", modes[next_index])
    mode_labels = [_ls(32108), _ls(32109), _ls(32251)]
    _notify(_ls(32220) % mode_labels[next_index])
    _finish_action(handle)


def _action_toggle(handle, tag):
    store = _store()
    store.toggle(tag)
    _finish_action(handle)


def _action_remove(handle, tag):
    store = _store()
    store.remove(tag)
    _finish_action(handle)


def _action_settings(handle):
    xbmcaddon.Addon(ADDON_ID).openSettings()
    _finish_action(handle)


def _subscription_store():
    import subscriptions
    return subscriptions.SubscriptionStore(
        os.path.join(helpers.profile_dir(), "subscriptions.json"))


def _action_sub_add(handle, url):
    """Add a pasted link: a profile when it parses, else a subscription URL."""
    if not url:
        url = (xbmcaddon.Addon(ADDON_ID).getSetting("subscription_url")
               or "").strip()
        if not url:
            _notify(_ls(32214), error=True)
            _finish_action(handle)
            return
    _log("sub_add url (%d chars): %r" % (len(url), url[:120]))
    store = _store()
    if parsers.parse_uri(url) is not None:
        _log("sub_add: url parses as a profile")
        p, err = store.add_uri(url)
        if err:
            _notify(_ls(32214), error=True)
        else:
            _notify(_ls(32213) % p["tag"])
        _finish_action(handle)
        return
    if not parsers.is_subscription_url(url):
        _log("sub_add: not a subscription URL")
        _notify(_ls(32214), error=True)
        _finish_action(handle)
        return
    sub_store = _subscription_store()
    _log("sub_add: fetching subscription")
    group, err = sub_store.add(url, profile_store=store)
    if err:
        _log("sub_add failed: %s" % err)
        _notify(_ls(32229), error=True)
    else:
        _log("sub_add OK: %s (%d profiles)" % (group["id"], len(store.profiles)))
        _notify(_ls(32223) % url)
    _finish_action(handle)


def _action_sub_refresh(handle, group_id):
    sub_store = _subscription_store()
    _, _, err = sub_store.refresh(
        group_id,
        disabled_protocols=helpers.disabled_protocols(),
        profile_store=_store())
    if err:
        _notify(_ls(32229), error=True)
    else:
        _notify(_ls(32224))
    _finish_action(handle)


def _action_sub_remove(handle, group_id):
    sub_store = _subscription_store()
    sub_store.remove(group_id, profile_store=_store())
    _finish_action(handle)


def _action_copy(handle, tag):
    store = _store()
    p = store.get(tag)
    if p and p.get("uri"):
        helpers.copy_to_clipboard(p["uri"])
        _notify(_ls(32225) % tag)
    else:
        _notify(_ls(32219), error=True)
    _finish_action(handle)


def main():
    handle, params = helpers.parse_plugin_args(sys.argv)
    action = params.get("action", "")
    _log("directory action=%s handle=%s params=%s" % (action, handle, params))

    if action == "add":
        _action_add(handle)
    elif action == "test":
        _action_test(handle)
    elif action == "clear":
        _action_clear(handle)
    elif action == "activate":
        _action_activate_reachable(handle, params.get("tag", ""))
    elif action == "toggle_mode":
        _action_toggle_mode(handle)
    elif action == "toggle":
        _action_toggle(handle, params.get("tag", ""))
    elif action == "remove":
        _action_remove(handle, params.get("tag", ""))
    elif action == "copy":
        _action_copy(handle, params.get("tag", ""))
    elif action == "sub_add":
        _action_sub_add(handle, params.get("url", ""))
    elif action == "sub_refresh":
        _action_sub_refresh(handle, params.get("id", ""))
    elif action == "sub_remove":
        _action_sub_remove(handle, params.get("id", ""))
    elif action == "settings":
        _action_settings(handle)
    else:
        _show_listing(handle)


if __name__ == "__main__":
    main()
