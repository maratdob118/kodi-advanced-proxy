#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Profile manager UI entry (plugin source).

Invoked from settings action buttons via RunPlugin(plugin://service.advancedproxy/?action=...).
Provides dialogs to add, manage, select, latency-test and clear profiles.
"""
import os
import sys
import time
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "src"))

import xbmc  # noqa: E402
import xbmcgui  # noqa: E402

from src import helpers, profiles  # noqa: E402

ADDON_NAME = "Advanced Proxy"


def _log(msg):
    xbmc.log("[%s] %s" % (helpers.ADDON_ID, msg), xbmc.LOGINFO)


def _store():
    return profiles.ProfileStore(helpers.profiles_path())


def _notify(msg, error=False):
    icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
    xbmcgui.Dialog().notification(ADDON_NAME, msg, icon, 3000)


def action_add():
    kb = xbmcgui.Dialog().input(
        "Add profile link (vless:// hy2:// trojan://)", type=xbmcgui.INPUT_ALPHANUM)
    if not kb:
        return
    store = _store()
    p, err = store.add_uri(kb)
    if err:
        _notify("Invalid link: %s" % err, error=True)
    else:
        _notify("Added: %s" % p["tag"])
    _sync_info(store)


def action_manage():
    store = _store()
    while True:
        if not store.profiles:
            _notify("No profiles. Add one first.")
            return
        items = []
        for p in store.profiles:
            mark = "[x]" if p.get("enabled", True) else "[ ]"
            active = " *" if p["tag"] == store.active_tag else ""
            items.append("%s %s (%s)%s" % (mark, p["tag"], p["protocol"], active))
        items.append("<< Back")
        idx = xbmcgui.Dialog().select("Manage profiles (* = active)", items)
        if idx < 0 or idx == len(items) - 1:
            _sync_info(store)
            return
        p = store.profiles[idx]
        choice = xbmcgui.Dialog().contextmenu(
            ["Enable/Disable", "Set active", "Delete", "Cancel"])
        if choice == 0:
            store.toggle(p["tag"])
        elif choice == 1:
            store.set_active(p["tag"])
        elif choice == 2:
            store.remove(p["tag"])


def action_select():
    store = _store()
    en = store.enabled()
    if not en:
        _notify("No enabled profiles", error=True)
        return
    items = [p["tag"] for p in en]
    preselect = items.index(store.active_tag) if store.active_tag in items else 0
    idx = xbmcgui.Dialog().select("Select active profile", items, preselect=preselect)
    if idx >= 0:
        store.set_active(en[idx]["tag"])
        _notify("Active: %s" % en[idx]["tag"])
    _sync_info(store)


def action_test():
    store = _store()
    en = store.enabled()
    if not en:
        _notify("No enabled profiles", error=True)
        return
    prog = xbmcgui.DialogProgress()
    prog.create(ADDON_NAME, "Testing latency...")
    results = []
    try:
        import socket
        for i, p in enumerate(en):
            prog.update(int(100 * i / len(en)), "Testing %s..." % p["tag"])
            ms = _tcp_latency(p["server"], p["port"], timeout=4)
            results.append((p["tag"], ms))
        prog.update(100, "Done")
    finally:
        prog.close()
    lines = ["%s: %s" % (t, ("%d ms" % m) if m is not None else "timeout")
             for t, m in sorted(results, key=lambda r: (r[1] is None, r[1]))]
    xbmcgui.Dialog().textviewer("Latency (TCP connect)", "\n".join(lines))


def _tcp_latency(host, port, timeout=4):
    import socket
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return int((time.time() - t0) * 1000)
    except Exception:
        return None


def action_clear():
    if xbmcgui.Dialog().yesno(ADDON_NAME, "Remove ALL profiles?"):
        store = _store()
        store.profiles = []
        store.active_tag = None
        store.save()
        _notify("All profiles cleared")
        _sync_info(store)


def _sync_info(store):
    """Reflect profile count + active profile into the settings info line."""
    try:
        import xbmcaddon
        addon = xbmcaddon.Addon(helpers.ADDON_ID)
        n = len(store.profiles)
        en = len(store.enabled())
        active = store.active_tag or "-"
        addon.setSetting("profiles_info", "%d profiles (%d enabled) | active: %s" % (n, en, active))
    except Exception as e:
        _log("sync info failed: %s" % e)


def main():
    query = sys.argv[2] if len(sys.argv) > 2 else ""
    params = urllib.parse.parse_qs(query.lstrip("?"))
    action = params.get("action", [""])[0]
    _log("UI action: %s argv=%s" % (action, sys.argv))

    if action == "add":
        action_add()
    elif action == "manage":
        action_manage()
    elif action == "select":
        action_select()
    elif action == "test":
        action_test()
    elif action == "clear":
        action_clear()
    else:
        # Opened as a plugin directory (addon browser) with no action:
        # close the directory cleanly to avoid a GetDirectory error,
        # then offer the profile manager.
        _close_directory()
        action_manage()


def _close_directory():
    try:
        import xbmcplugin
        handle = int(sys.argv[1])
        if handle >= 0:
            xbmcplugin.endOfDirectory(handle, succeeded=True)
    except (ValueError, IndexError):
        pass


if __name__ == "__main__":
    main()
