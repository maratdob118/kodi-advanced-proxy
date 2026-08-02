# -*- coding: utf-8 -*-
"""Unit tests for the Kodi-free proxy integration state machine.

Run:  python3 -m unittest tests.test_proxy_integration -v
No Kodi required; proxy_integration never imports xbmc modules.
"""
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "service.advancedproxy", "src")
sys.path.insert(0, os.path.abspath(SRC))

import proxy_integration  # noqa: E402
from proxy_integration import (  # noqa: E402
    BACKUP_SCHEMA,
    DEFAULT_HOST,
    IntegrationManager,
    KODI_SETTING_IDS,
    YOUTUBE_ADDON_ID,
    YOUTUBE_EXPECTED,
    YOUTUBE_SETTING_ID,
)


def default_kodi_values():
    return {
        "network.usehttpproxy": False,
        "network.httpproxytype": 0,
        "network.httpproxyserver": "",
        "network.httpproxyport": 8080,
    }


class FakeKodi(object):
    """In-memory Kodi system-settings adapter with call recording."""

    def __init__(self, values=None, fail_on=(), events=None, on_write=None):
        self.values = dict(values or {})
        self.fail_on = set(fail_on)
        self.events = events if events is not None else []
        self.on_write = on_write
        self.reads = []
        self.writes = []

    def read(self, setting_id):
        self.reads.append(setting_id)
        return self.values.get(setting_id)

    def write(self, setting_id, value):
        if self.on_write:
            self.on_write(setting_id, value)
        if setting_id in self.fail_on:
            return False
        self.writes.append((setting_id, value))
        self.events.append(("kodi", setting_id, value))
        self.values[setting_id] = value
        return True


class FakeAddon(object):
    """In-memory addon-settings adapter for the YouTube plugin."""

    def __init__(self, available=True, source=0, fail=False, events=None):
        self.available = available
        self.source = source
        self.fail = fail
        self.events = events if events is not None else []
        self.writes = []

    def is_available(self):
        return self.available

    def read(self, addon_id, setting_id):
        if not self.available:
            return None
        return self.source

    def write(self, addon_id, setting_id, value):
        if self.fail:
            return False
        self.writes.append((setting_id, value))
        self.events.append(("addon", setting_id, value))
        self.source = value
        return True


def build(tmp, kodi=None, addon=None, logger=None, notify=None):
    """Construct a manager wired to fresh fakes; returns (mgr, kodi, addon, backup_path)."""
    kodi = kodi or FakeKodi(default_kodi_values())
    addon = addon or FakeAddon()
    backup_path = os.path.join(tmp, "integration_backup.json")
    mgr = IntegrationManager(
        backup_path=backup_path,
        read_kodi=kodi.read,
        write_kodi=kodi.write,
        addon_available=addon.is_available,
        read_addon=addon.read,
        write_addon=addon.write,
        logger=logger,
        notify=notify,
    )
    return mgr, kodi, addon, backup_path


def read_backup(path):
    with open(path) as f:
        return json.load(f)


class TestIntegrationManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    # ---- structure -----------------------------------------------------

    def test_planned_signatures_exist(self):
        mgr, _, _, _ = build(self.tmp)
        for name in ("ensure_configured", "validate", "restore_previous",
                     "backup_exists"):
            self.assertTrue(callable(getattr(mgr, name, None)), name)

    def test_constructor_accepts_all_planned_arguments(self):
        # build() already exercises the exact constructor signature
        mgr, _, _, _ = build(self.tmp, logger=lambda m, l="info": None,
                             notify=lambda m: None)
        self.assertIsNotNone(mgr)

    def test_module_source_has_no_xbmc_import(self):
        with open(os.path.join(SRC, "proxy_integration.py")) as f:
            src = f.read()
        for lineno, line in enumerate(src.splitlines(), 1):
            if re.match(r"^\s*(import xbmc|from xbmc)", line):
                self.fail("xbmc import at line %d: %s" % (lineno, line))

    def test_module_imports_without_xbmc(self):
        # the module imported at collection time with no xbmc installed
        self.assertNotIn("xbmc", dir(proxy_integration))

    def test_youtube_constants(self):
        self.assertEqual(YOUTUBE_ADDON_ID, "plugin.video.youtube")
        self.assertEqual(YOUTUBE_SETTING_ID, "requests.proxy.source")
        self.assertEqual(YOUTUBE_EXPECTED, 1)

    def test_kodi_setting_ids_order(self):
        self.assertEqual(tuple(KODI_SETTING_IDS), (
            "network.usehttpproxy", "network.httpproxytype",
            "network.httpproxyserver", "network.httpproxyport"))

    def test_backup_schema_version(self):
        self.assertEqual(BACKUP_SCHEMA, 1)

    # ---- no-op when already matching -----------------------------------

    def test_noop_when_already_matching(self):
        kodi = FakeKodi({"network.usehttpproxy": True, "network.httpproxytype": 0,
                         "network.httpproxyserver": "127.0.0.1",
                         "network.httpproxyport": 1080})
        addon = FakeAddon(source=1)
        mgr, kodi, addon, backup_path = build(self.tmp, kodi=kodi, addon=addon)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        self.assertFalse(mgr.backup_exists())

    def test_noop_when_values_arrive_as_strings(self):
        kodi = FakeKodi({"network.usehttpproxy": "true", "network.httpproxytype": "0",
                         "network.httpproxyserver": "127.0.0.1",
                         "network.httpproxyport": "1080"})
        addon = FakeAddon(source="1")
        mgr, kodi, addon, backup_path = build(self.tmp, kodi=kodi, addon=addon)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        self.assertFalse(mgr.backup_exists())

    # ---- compare-and-set -----------------------------------------------

    def test_effective_port_correction(self):
        mgr, kodi, addon, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(kodi.values["network.usehttpproxy"], True)
        self.assertEqual(kodi.values["network.httpproxytype"], 0)
        self.assertEqual(kodi.values["network.httpproxyserver"], "127.0.0.1")
        self.assertEqual(kodi.values["network.httpproxyport"], 1080)
        self.assertEqual(addon.source, 1)
        # deterministic apply order: kodi settings in tuple order, then addon
        self.assertEqual(kodi.writes, [
            ("network.usehttpproxy", True),
            ("network.httpproxyserver", "127.0.0.1"),
            ("network.httpproxyport", 1080),
        ])
        self.assertEqual(addon.writes, [(YOUTUBE_SETTING_ID, 1)])
        # schema-v1 backup captures previous values before mutation
        backup = read_backup(backup_path)
        self.assertEqual(backup["schema"], 1)
        self.assertEqual(backup["applied_port"], 1080)
        self.assertEqual(backup["kodi"]["network.httpproxyport"],
                         {"previous": 8080, "applied": 1080})
        self.assertEqual(backup["kodi"]["network.usehttpproxy"],
                         {"previous": False, "applied": True})
        self.assertEqual(backup["kodi"]["network.httpproxyserver"],
                         {"previous": "", "applied": "127.0.0.1"})
        self.assertEqual(backup["addons"][YOUTUBE_ADDON_ID],
                         {"setting": YOUTUBE_SETTING_ID, "previous": 0,
                          "applied": 1})

    def test_apply_order_kodi_before_addon(self):
        events = []
        kodi = FakeKodi(default_kodi_values(), events=events)
        addon = FakeAddon(events=events)
        backup_path = os.path.join(self.tmp, "integration_backup.json")
        mgr = IntegrationManager(backup_path=backup_path, read_kodi=kodi.read,
                                 write_kodi=kodi.write,
                                 addon_available=addon.is_available,
                                 read_addon=addon.read, write_addon=addon.write)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        kodi_positions = [i for i, e in enumerate(events) if e[0] == "kodi"]
        addon_positions = [i for i, e in enumerate(events) if e[0] == "addon"]
        self.assertTrue(kodi_positions)
        self.assertTrue(addon_positions)
        self.assertLess(max(kodi_positions), min(addon_positions))

    def test_youtube_source_0_corrected_to_1(self):
        mgr, _, addon, _ = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(addon.source, 1)

    def test_youtube_source_2_corrected_to_1(self):
        addon = FakeAddon(source=2)
        mgr, _, addon, _ = build(self.tmp, addon=addon)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(addon.source, 1)

    def test_missing_youtube_is_non_fatal(self):
        kodi = FakeKodi(default_kodi_values())
        addon = FakeAddon(available=False)
        mgr, kodi, addon, backup_path = build(self.tmp, kodi=kodi, addon=addon)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(addon.writes, [])
        self.assertEqual(kodi.values["network.httpproxyport"], 1080)
        backup = read_backup(backup_path)
        self.assertEqual(backup["addons"], {})

    def test_host_fallback_when_bind_is_any(self):
        mgr, kodi, _, _ = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("0.0.0.0", 1080))
        self.assertEqual(kodi.values["network.httpproxyserver"], DEFAULT_HOST)

    def test_invalid_port_rejected_without_writes(self):
        mgr, kodi, addon, _ = build(self.tmp)
        self.assertFalse(mgr.ensure_configured("127.0.0.1", "not-a-port"))
        self.assertFalse(mgr.ensure_configured("127.0.0.1", None))
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        self.assertFalse(mgr.backup_exists())

    # ---- validate ------------------------------------------------------

    def test_validate_true_when_matching(self):
        kodi = FakeKodi({"network.usehttpproxy": True, "network.httpproxytype": 0,
                         "network.httpproxyserver": "127.0.0.1",
                         "network.httpproxyport": 1080})
        addon = FakeAddon(source=1)
        mgr, _, _, _ = build(self.tmp, kodi=kodi, addon=addon)
        self.assertTrue(mgr.validate("127.0.0.1", 1080))

    def test_validate_false_when_port_differs(self):
        mgr, _, _, _ = build(self.tmp)
        self.assertFalse(mgr.validate("127.0.0.1", 1080))

    def test_validate_false_when_youtube_differs(self):
        kodi = FakeKodi({"network.usehttpproxy": True, "network.httpproxytype": 0,
                         "network.httpproxyserver": "127.0.0.1",
                         "network.httpproxyport": 1080})
        addon = FakeAddon(source=0)
        mgr, _, _, _ = build(self.tmp, kodi=kodi, addon=addon)
        self.assertFalse(mgr.validate("127.0.0.1", 1080))

    def test_validate_true_when_youtube_missing(self):
        kodi = FakeKodi({"network.usehttpproxy": True, "network.httpproxytype": 0,
                         "network.httpproxyserver": "127.0.0.1",
                         "network.httpproxyport": 1080})
        addon = FakeAddon(available=False)
        mgr, _, _, _ = build(self.tmp, kodi=kodi, addon=addon)
        self.assertTrue(mgr.validate("127.0.0.1", 1080))

    # ---- backup-before-write / failed persistence ----------------------

    def test_backup_persisted_before_first_write(self):
        backup_path = os.path.join(self.tmp, "integration_backup.json")
        seen = {}
        def on_write(setting_id, value):
            if not seen:
                seen["backup_existed_at_first_write"] = os.path.exists(backup_path)
        kodi = FakeKodi(default_kodi_values(), on_write=on_write)
        addon = FakeAddon()
        mgr = IntegrationManager(backup_path=backup_path, read_kodi=kodi.read,
                                 write_kodi=kodi.write,
                                 addon_available=addon.is_available,
                                 read_addon=addon.read, write_addon=addon.write)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertTrue(seen["backup_existed_at_first_write"])
        self.assertTrue(os.path.exists(backup_path))

    def test_failed_backup_persistence_aborts_without_mutation(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        backup_path = os.path.join(blocker, "integration_backup.json")
        kodi = FakeKodi(default_kodi_values())
        addon = FakeAddon()
        mgr = IntegrationManager(backup_path=backup_path, read_kodi=kodi.read,
                                 write_kodi=kodi.write,
                                 addon_available=addon.is_available,
                                 read_addon=addon.read, write_addon=addon.write)
        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)
        self.assertEqual(kodi.values["network.usehttpproxy"], False)

    # ---- backup survival across restart ---------------------------------

    def test_backup_survives_restart_and_restores(self):
        mgr1, kodi, addon, backup_path = build(self.tmp)
        self.assertTrue(mgr1.ensure_configured("127.0.0.1", 1080))
        backup = read_backup(backup_path)
        self.assertEqual(backup["kodi"]["network.httpproxyport"]["previous"], 8080)

        # a fresh manager over the same backup file and adapters = restart
        mgr2 = IntegrationManager(backup_path=backup_path, read_kodi=kodi.read,
                                  write_kodi=kodi.write,
                                  addon_available=addon.is_available,
                                  read_addon=addon.read, write_addon=addon.write)
        n_writes = len(kodi.writes)
        self.assertTrue(mgr2.ensure_configured("127.0.0.1", 1080))
        # already matching: nothing new written, original previous preserved
        self.assertEqual(len(kodi.writes), n_writes)
        self.assertEqual(read_backup(backup_path)["kodi"]
                         ["network.httpproxyport"]["previous"], 8080)

        self.assertTrue(mgr2.restore_previous())
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)
        self.assertEqual(kodi.values["network.usehttpproxy"], False)
        self.assertEqual(kodi.values["network.httpproxyserver"], "")
        self.assertEqual(addon.source, 0)
        self.assertFalse(mgr2.backup_exists())

    def test_port_change_updates_backup_keeps_original_previous(self):
        mgr, kodi, _, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        # effective port falls back to 1081 (1080 became busy)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1081))
        self.assertEqual(kodi.values["network.httpproxyport"], 1081)
        backup = read_backup(backup_path)
        self.assertEqual(backup["applied_port"], 1081)
        self.assertEqual(backup["kodi"]["network.httpproxyport"],
                         {"previous": 8080, "applied": 1081})
        # restore still goes back to the ORIGINAL port
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)

    def test_failed_first_write_during_port_change_preserves_prior_backup(self):
        mgr, kodi, _, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        with open(backup_path, "rb") as f:
            original_backup = f.read()

        kodi.fail_on.add("network.httpproxyport")
        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1081))
        with open(backup_path, "rb") as f:
            self.assertEqual(f.read(), original_backup)
        self.assertEqual(kodi.values["network.httpproxyport"], 1080)

        kodi.fail_on.clear()
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)

    def test_failed_later_write_during_port_change_rolls_back_attempt_only(self):
        mgr, kodi, _, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        with open(backup_path, "rb") as f:
            original_backup = f.read()
        kodi.values["network.usehttpproxy"] = False
        kodi.fail_on.add("network.httpproxyport")

        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1081))
        self.assertEqual(kodi.values["network.usehttpproxy"], False)
        self.assertEqual(kodi.values["network.httpproxyport"], 1080)
        with open(backup_path, "rb") as f:
            self.assertEqual(f.read(), original_backup)

        kodi.fail_on.clear()
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)

    def test_user_drift_rolls_previous_forward(self):
        mgr, kodi, _, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        # user switches the Kodi proxy server while AP is running
        kodi.values["network.httpproxyserver"] = "10.0.0.1"
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(kodi.values["network.httpproxyserver"], "127.0.0.1")
        backup = read_backup(backup_path)
        self.assertEqual(backup["kodi"]["network.httpproxyserver"],
                         {"previous": "10.0.0.1", "applied": "127.0.0.1"})
        # restore returns to the user's chosen server, not the original ""
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(kodi.values["network.httpproxyserver"], "10.0.0.1")

    def test_prior_youtube_record_preserved_when_only_kodi_drifts(self):
        mgr, kodi, addon, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))  # youtube 0 -> 1
        kodi.values["network.httpproxyport"] = 8080                # port drifts back
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))  # re-apply port only
        backup = read_backup(backup_path)
        self.assertEqual(backup["addons"][YOUTUBE_ADDON_ID]["previous"], 0)
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(addon.source, 0)      # youtube still restored
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)

    # ---- restore --------------------------------------------------------

    def test_backup_exists_lifecycle(self):
        mgr, _, _, _ = build(self.tmp)
        self.assertFalse(mgr.backup_exists())
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertTrue(mgr.backup_exists())
        self.assertTrue(mgr.restore_previous())
        self.assertFalse(mgr.backup_exists())

    def test_restore_without_backup_is_noop(self):
        mgr, kodi, addon, _ = build(self.tmp)
        self.assertFalse(mgr.restore_previous())
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])

    def test_user_change_protection(self):
        mgr, kodi, addon, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        # user picks a different port after AP configured
        kodi.values["network.httpproxyport"] = 9999
        self.assertTrue(mgr.restore_previous())
        # port NOT clobbered; everything still matching applied is restored
        self.assertEqual(kodi.values["network.httpproxyport"], 9999)
        self.assertEqual(kodi.values["network.usehttpproxy"], False)
        self.assertEqual(kodi.values["network.httpproxyserver"], "")
        self.assertEqual(addon.source, 0)
        self.assertFalse(mgr.backup_exists())

    def test_stale_backup_restore_fully_applied(self):
        backup_path = os.path.join(self.tmp, "integration_backup.json")
        with open(backup_path, "w") as f:
            json.dump({"schema": 1,
                       "kodi": {
                           "network.usehttpproxy": {"previous": False,
                                                    "applied": True},
                           "network.httpproxyport": {"previous": 8080,
                                                     "applied": 1080}},
                       "addons": {},
                       "applied_port": 1080}, f)
        # current state still equals what was applied (crash before shutdown)
        kodi = FakeKodi({"network.usehttpproxy": True,
                         "network.httpproxyport": 1080})
        addon = FakeAddon()
        mgr = IntegrationManager(backup_path=backup_path, read_kodi=kodi.read,
                                 write_kodi=kodi.write,
                                 addon_available=addon.is_available,
                                 read_addon=addon.read, write_addon=addon.write)
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(kodi.values["network.usehttpproxy"], False)
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)
        self.assertFalse(mgr.backup_exists())

    def test_stale_backup_restore_skips_already_previous(self):
        backup_path = os.path.join(self.tmp, "integration_backup.json")
        with open(backup_path, "w") as f:
            json.dump({"schema": 1,
                       "kodi": {
                           "network.usehttpproxy": {"previous": False,
                                                    "applied": True},
                           "network.httpproxyport": {"previous": 8080,
                                                     "applied": 1080}},
                       "addons": {},
                       "applied_port": 1080}, f)
        # port already back at previous (an interrupted earlier restore)
        kodi = FakeKodi({"network.usehttpproxy": True,
                         "network.httpproxyport": 8080})
        addon = FakeAddon()
        mgr = IntegrationManager(backup_path=backup_path, read_kodi=kodi.read,
                                 write_kodi=kodi.write,
                                 addon_available=addon.is_available,
                                 read_addon=addon.read, write_addon=addon.write)
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(kodi.values["network.usehttpproxy"], False)  # restored
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)  # untouched
        self.assertEqual(kodi.writes, [("network.usehttpproxy", False)])
        self.assertFalse(mgr.backup_exists())

    def test_unknown_schema_backup_ignored(self):
        backup_path = os.path.join(self.tmp, "integration_backup.json")
        with open(backup_path, "w") as f:
            json.dump({"schema": 99, "kodi": {}, "addons": {}}, f)
        kodi = FakeKodi(default_kodi_values())
        addon = FakeAddon()
        mgr = IntegrationManager(backup_path=backup_path, read_kodi=kodi.read,
                                 write_kodi=kodi.write,
                                 addon_available=addon.is_available,
                                 read_addon=addon.read, write_addon=addon.write)
        self.assertFalse(mgr.restore_previous())
        self.assertEqual(kodi.writes, [])
        self.assertTrue(mgr.backup_exists())  # left in place, not trusted

    def test_malformed_backup_blocks_ensure_and_restore_without_overwrite(self):
        mgr, kodi, addon, backup_path = build(self.tmp)
        malformed = b"{not valid json\n"
        with open(backup_path, "wb") as f:
            f.write(malformed)

        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertFalse(mgr.restore_previous())
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        with open(backup_path, "rb") as f:
            self.assertEqual(f.read(), malformed)

    def test_invalid_schema_one_backup_blocks_ensure_and_restore(self):
        mgr, kodi, addon, backup_path = build(self.tmp)
        invalid = {
            "schema": 1,
            "kodi": {"network.unknown": {"previous": 1, "applied": 2}},
            "addons": {},
            "applied_port": 1080,
        }
        with open(backup_path, "w") as f:
            json.dump(invalid, f)
        before = read_backup(backup_path)

        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertFalse(mgr.restore_previous())
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        self.assertEqual(read_backup(backup_path), before)

    def test_invalid_addon_backup_record_is_rejected(self):
        mgr, kodi, addon, backup_path = build(self.tmp)
        invalid = {
            "schema": 1,
            "kodi": {},
            "addons": {YOUTUBE_ADDON_ID: {
                "setting": "unexpected.setting", "previous": 0, "applied": 1,
            }},
            "applied_port": 1080,
        }
        with open(backup_path, "w") as f:
            json.dump(invalid, f)

        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertFalse(mgr.restore_previous())
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        self.assertEqual(read_backup(backup_path), invalid)

    def test_non_scalar_backup_values_are_rejected(self):
        mgr, kodi, addon, backup_path = build(self.tmp)
        invalid = {
            "schema": 1,
            "kodi": {"network.httpproxyserver": {
                "previous": {"unexpected": "object"},
                "applied": "127.0.0.1",
            }},
            "addons": {},
            "applied_port": 1080,
        }
        with open(backup_path, "w") as f:
            json.dump(invalid, f)

        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertFalse(mgr.restore_previous())
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        self.assertEqual(read_backup(backup_path), invalid)

    def test_restore_retry_after_partial_failure(self):
        mgr, kodi, addon, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        kodi.fail_on.add("network.usehttpproxy")
        self.assertFalse(mgr.restore_previous())
        self.assertTrue(mgr.backup_exists())  # kept for retry
        # everything except the blocked flag was restored
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)
        self.assertEqual(kodi.values["network.httpproxyserver"], "")
        self.assertEqual(addon.source, 0)
        self.assertEqual(kodi.values["network.usehttpproxy"], True)
        # clear the fault and retry
        kodi.fail_on.clear()
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(kodi.values["network.usehttpproxy"], False)
        self.assertFalse(mgr.backup_exists())

    def test_restore_keeps_addon_backup_until_addon_returns(self):
        mgr, _, addon, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        addon.available = False

        self.assertFalse(mgr.restore_previous())
        self.assertTrue(mgr.backup_exists())
        self.assertEqual(addon.source, 1)

        addon.available = True
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(addon.source, 0)
        self.assertFalse(mgr.backup_exists())

    def test_restore_keeps_backup_when_addon_read_fails(self):
        mgr, _, addon, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        mgr._read_addon_adapter = lambda *args: (_ for _ in ()).throw(
            RuntimeError("unreadable"))

        self.assertFalse(mgr.restore_previous())
        self.assertTrue(mgr.backup_exists())
        self.assertEqual(addon.source, 1)

    def test_restore_reports_backup_deletion_failure_and_can_retry(self):
        mgr, kodi, addon, backup_path = build(self.tmp)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        real_remove = proxy_integration.os.remove
        proxy_integration.os.remove = lambda path: (_ for _ in ()).throw(
            OSError("busy"))
        try:
            self.assertFalse(mgr.restore_previous())
        finally:
            proxy_integration.os.remove = real_remove
        self.assertTrue(mgr.backup_exists())
        self.assertEqual(kodi.values, default_kodi_values())
        self.assertEqual(addon.source, 0)

        self.assertTrue(mgr.restore_previous())
        self.assertFalse(mgr.backup_exists())

    # ---- rollback ------------------------------------------------------

    def test_partial_apply_rollback_on_kodi_failure(self):
        kodi = FakeKodi(default_kodi_values(),
                        fail_on=("network.httpproxyport",))
        addon = FakeAddon()
        mgr, kodi, addon, backup_path = build(self.tmp, kodi=kodi, addon=addon)
        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        # settings applied before the failure were rolled back to previous
        self.assertEqual(kodi.values["network.usehttpproxy"], False)
        self.assertEqual(kodi.values["network.httpproxyserver"], "")
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)
        self.assertEqual(addon.source, 0)
        self.assertFalse(mgr.backup_exists())  # full rollback, no stale backup

    def test_incomplete_first_apply_rollback_keeps_backup_for_retry(self):
        def fail_server_rollback(setting_id, value):
            if setting_id == "network.httpproxyserver" and value == "":
                kodi.fail_on.add(setting_id)

        kodi = FakeKodi(default_kodi_values(),
                        fail_on=("network.httpproxyport",),
                        on_write=fail_server_rollback)
        addon = FakeAddon()
        mgr, kodi, addon, backup_path = build(self.tmp, kodi=kodi, addon=addon)

        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(kodi.values["network.httpproxyserver"], "127.0.0.1")
        self.assertTrue(mgr.backup_exists())
        self.assertEqual(read_backup(backup_path)["kodi"]
                         ["network.httpproxyserver"]["previous"], "")

        kodi.on_write = None
        kodi.fail_on.clear()
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(kodi.values, default_kodi_values())
        self.assertFalse(mgr.backup_exists())

    def test_rollback_on_addon_write_failure(self):
        kodi = FakeKodi(default_kodi_values())
        addon = FakeAddon(fail=True)
        mgr, kodi, addon, backup_path = build(self.tmp, kodi=kodi, addon=addon)
        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(kodi.values["network.usehttpproxy"], False)
        self.assertEqual(kodi.values["network.httpproxyserver"], "")
        self.assertEqual(kodi.values["network.httpproxyport"], 8080)
        self.assertEqual(addon.source, 0)
        self.assertFalse(mgr.backup_exists())

    # ---- notify / logging / robustness ----------------------------------

    def test_notify_called_only_when_changes_applied(self):
        notifications = []
        mgr, _, _, _ = build(self.tmp, notify=notifications.append)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(len(notifications), 1)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))  # no-op
        self.assertEqual(len(notifications), 1)
        self.assertTrue(mgr.restore_previous())
        self.assertEqual(len(notifications), 2)

    def test_logger_records_activity(self):
        logs = []
        def logger(msg, level="info"):
            logs.append((level, msg))
        mgr, _, _, _ = build(self.tmp, logger=logger)
        self.assertTrue(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertTrue(logs)

    def test_adapter_exceptions_are_non_fatal(self):
        def boom(*args, **kwargs):
            raise RuntimeError("boom")
        mgr = IntegrationManager(
            backup_path=os.path.join(self.tmp, "b.json"),
            read_kodi=boom, write_kodi=boom,
            addon_available=lambda: True, read_addon=boom, write_addon=boom)
        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertFalse(mgr.validate("127.0.0.1", 1080))
        self.assertFalse(mgr.restore_previous())

    def test_unreadable_required_kodi_value_aborts_before_any_mutation(self):
        kodi = FakeKodi(default_kodi_values())
        addon = FakeAddon()
        def read_kodi(setting_id):
            if setting_id == "network.httpproxyserver":
                raise RuntimeError("unreadable")
            return kodi.read(setting_id)
        backup_path = os.path.join(self.tmp, "integration_backup.json")
        mgr = IntegrationManager(
            backup_path, read_kodi, kodi.write, addon.is_available,
            addon.read, addon.write)

        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        self.assertFalse(os.path.exists(backup_path))

    def test_unreadable_available_addon_value_aborts_before_any_mutation(self):
        kodi = FakeKodi(default_kodi_values())
        addon = FakeAddon()
        backup_path = os.path.join(self.tmp, "integration_backup.json")
        def read_addon(*args):
            raise RuntimeError("unreadable")
        mgr = IntegrationManager(
            backup_path, kodi.read, kodi.write, addon.is_available,
            read_addon, addon.write)

        self.assertFalse(mgr.ensure_configured("127.0.0.1", 1080))
        self.assertEqual(kodi.writes, [])
        self.assertEqual(addon.writes, [])
        self.assertFalse(os.path.exists(backup_path))


if __name__ == "__main__":
    unittest.main()
