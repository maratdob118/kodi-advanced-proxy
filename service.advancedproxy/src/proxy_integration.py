# -*- coding: utf-8 -*-
"""Kodi-free proxy integration state machine.

``IntegrationManager`` owns compare/apply/backup/restore decisions for aligning
Kodi system proxy settings and supported addon proxy settings with Advanced
Proxy's effective local port. It never imports xbmc* modules: every Kodi access
goes through adapters injected at construction time (supplied by helpers.py),
which keeps the whole state machine unit-testable and crash-safe.

Backup layout (schema 1), persisted to ``backup_path`` BEFORE any setting is
mutated so previous values survive crashes and service restarts::

    {
      "schema": 1,
      "kodi": {"setting.id": {"previous": <value>, "applied": <value>}, ...},
      "addons": {"plugin.video.youtube":
                     {"setting": "requests.proxy.source", "previous": 0,
                      "applied": 1}},
      "applied_port": 1080
    }

Restore is guarded: a setting is only written back while its current value
still matches what Advanced Proxy applied, so user changes are never
clobbered. Integration failures are always non-fatal (methods return False
instead of raising); they never propagate into the proxy service loop.
"""
import json
import os

BACKUP_SCHEMA = 1

KODI_SETTING_IDS = (
    "network.usehttpproxy",
    "network.httpproxytype",
    "network.httpproxyserver",
    "network.httpproxyport",
)

YOUTUBE_ADDON_ID = "plugin.video.youtube"
YOUTUBE_SETTING_ID = "requests.proxy.source"
YOUTUBE_EXPECTED = 1

DEFAULT_HOST = "127.0.0.1"
_ANY_HOSTS = ("", "0.0.0.0", "::", "::0", "0:0:0:0:0:0:0:0")
_BACKUP_ABSENT = object()
_BACKUP_INVALID = object()


class IntegrationManager(object):
    """Compare-and-set / backup / restore for Kodi proxy integration.

    Adaptor contracts (callables):

    - ``read_kodi(setting_id) -> value|None``  (None = unreadable)
    - ``write_kodi(setting_id, value) -> bool``
    - ``addon_available() -> bool``            (zero-arg; YouTube present?)
    - ``read_addon(addon_id, setting_id) -> value|None``
    - ``write_addon(addon_id, setting_id, value) -> bool``
    - ``logger(msg, level="info")`` and ``notify(msg)`` are optional
      best-effort callbacks.
    """

    def __init__(self, backup_path, read_kodi, write_kodi,
                 addon_available, read_addon, write_addon,
                 logger=None, notify=None):
        self._backup_path = backup_path
        self._read_kodi_adapter = read_kodi
        self._write_kodi_adapter = write_kodi
        self._addon_available_adapter = addon_available
        self._read_addon_adapter = read_addon
        self._write_addon_adapter = write_addon
        self._logger = logger
        self._notify = notify

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def ensure_configured(self, host, port):
        """Compare current settings against the expected ones and fix drift.

        The expected Kodi values are: system proxy enabled, HTTP type,
        server ``host`` (falling back to 127.0.0.1 for any-bind hosts) and the
        effective local ``port``; the YouTube addon proxy source is set to 1.

        A schema-v1 backup is persisted before the first mutation so previous
        values survive crashes, then changes are applied in order with
        rollback on partial failure. Returns True when everything matches (or
        was made to match); False on failure. Never raises.
        """
        try:
            port = int(port)
        except (TypeError, ValueError):
            self._log("integration: invalid port %r" % (port,), "warn")
            return False
        try:
            return self._ensure(host, port)
        except Exception as e:
            self._log("integration: ensure failed: %s" % e, "error")
            return False

    def validate(self, host, port):
        """Read-only check that current settings match the expected values.

        Returns True only if every Kodi setting matches and, when the YouTube
        addon is present, its proxy source is 1. Never raises.
        """
        try:
            return self._validate(host, port)
        except Exception as e:
            self._log("integration: validate failed: %s" % e, "error")
            return False

    def restore_previous(self):
        """Restore the values recorded before integration took over.

        Guarded: a setting is written back only while its current value still
        matches what Advanced Proxy applied (user changes are left alone).
        The backup is removed once every restorable write succeeded, so a
        failed restore can be retried. Returns True when the backup was
        concluded; False when there is no usable backup or a write failed.
        Never raises.
        """
        try:
            return self._restore()
        except Exception as e:
            self._log("integration: restore failed: %s" % e, "error")
            return False

    def backup_exists(self):
        """True when a backup file is present."""
        return os.path.isfile(self._backup_path)

    # ------------------------------------------------------------------
    # core flows
    # ------------------------------------------------------------------

    def _ensure(self, host, port):
        existing = self._load_backup()
        if existing is _BACKUP_INVALID:
            self._log("integration: invalid backup; refusing to overwrite it",
                      "error")
            return False

        expected = self._expected_values(host, port)

        current = {}
        for sid in KODI_SETTING_IDS:
            current[sid] = self._read_kodi(sid)
            if current[sid] is None:
                self._log("integration: cannot read %s; nothing changed" % sid,
                          "error")
                return False

        addon_ok = self._addon_available()
        youtube = self._read_addon(YOUTUBE_ADDON_ID, YOUTUBE_SETTING_ID) \
            if addon_ok else None
        if addon_ok and youtube is None:
            self._log("integration: cannot read YouTube proxy setting; "
                      "nothing changed", "error")
            return False
        current[YOUTUBE_ADDON_ID] = youtube

        changes = {}
        for sid in KODI_SETTING_IDS:
            if not self._matches(current[sid], expected[sid]):
                changes[sid] = expected[sid]

        youtube_pending = None
        if addon_ok and not self._matches(youtube, YOUTUBE_EXPECTED):
            youtube_pending = YOUTUBE_EXPECTED

        if not changes and youtube_pending is None:
            self._log("integration: already configured for %s:%s" % (host, port))
            return True

        prior_bytes = None
        if existing is not _BACKUP_ABSENT:
            prior_bytes = self._read_backup_bytes()
            if prior_bytes is None:
                self._log("integration: cannot preserve existing backup; "
                          "nothing changed", "error")
                return False
        backup = self._build_backup(expected, current, youtube_pending, port,
                                    existing)
        if not self._persist_backup(backup):
            self._log("integration: cannot persist backup; nothing changed", "error")
            return False

        applied = []
        for sid in KODI_SETTING_IDS:
            if sid not in changes:
                continue
            if self._write_kodi(sid, changes[sid]):
                applied.append(("kodi", sid))
            else:
                self._log("integration: write %s failed; rolling back" % sid,
                          "error")
                self._rollback(applied, current, youtube, prior_bytes)
                return False
        if youtube_pending is not None:
            if self._write_addon(YOUTUBE_ADDON_ID, YOUTUBE_SETTING_ID,
                                 youtube_pending):
                applied.append(("addon", YOUTUBE_ADDON_ID))
            else:
                self._log("integration: addon write failed; rolling back",
                          "error")
                self._rollback(applied, current, youtube, prior_bytes)
                return False

        self._log("integration: configured Kodi proxy for %s:%s" % (host, port))
        self._notify_user("Kodi proxy configured on %s:%s" % (host, port))
        return True

    def _validate(self, host, port):
        try:
            port = int(port)
        except (TypeError, ValueError):
            return False
        expected = self._expected_values(host, port)
        for sid in KODI_SETTING_IDS:
            if not self._matches(self._read_kodi(sid), expected[sid]):
                return False
        if self._addon_available():
            current = self._read_addon(YOUTUBE_ADDON_ID, YOUTUBE_SETTING_ID)
            if not self._matches(current, YOUTUBE_EXPECTED):
                return False
        return True

    def _restore(self):
        backup = self._load_backup()
        if backup is _BACKUP_ABSENT:
            self._log("integration: no usable backup; nothing to restore")
            return False
        if backup is _BACKUP_INVALID:
            self._log("integration: invalid backup; refusing to restore it",
                      "error")
            return False

        all_ok = True
        restored = []

        kodi_records = backup.get("kodi", {}) or {}
        for sid, rec in kodi_records.items():
            if not isinstance(rec, dict):
                continue
            previous = rec.get("previous")
            applied = rec.get("applied")
            current = self._read_kodi(sid)
            if current is None:
                all_ok = False
                continue
            if not self._matches(current, applied):
                continue  # user changed it — never clobber
            if self._write_kodi(sid, previous):
                restored.append(sid)
            else:
                all_ok = False

        addon_records = backup.get("addons", {}) or {}
        for addon_id, rec in addon_records.items():
            if not isinstance(rec, dict):
                continue
            setting = rec.get("setting")
            previous = rec.get("previous")
            applied = rec.get("applied")
            if not self._addon_available():
                all_ok = False
                continue
            current = self._read_addon(addon_id, setting)
            if current is None:
                all_ok = False
                continue
            if not self._matches(current, applied):
                continue
            if self._write_addon(addon_id, setting, previous):
                restored.append(addon_id)
            else:
                all_ok = False

        if all_ok:
            all_ok = self._delete_backup()
            if all_ok:
                if restored:
                    self._log("integration: restored previous proxy settings")
                    self._notify_user("Kodi proxy settings restored")
                else:
                    self._log("integration: backup concluded; nothing to restore")
            else:
                self._log("integration: restored settings but could not delete "
                          "backup; kept for retry", "error")
        else:
            self._log("integration: partial restore; backup kept for retry",
                      "error")
        return all_ok

    # ------------------------------------------------------------------
    # backup construction / io
    # ------------------------------------------------------------------

    def _build_backup(self, expected, current, youtube_pending, port, existing):
        if existing is _BACKUP_ABSENT:
            existing = {}
        existing_kodi = existing.get("kodi", {}) or {}
        existing_addons = existing.get("addons", {}) or {}

        kodi_records = {}
        for sid in KODI_SETTING_IDS:
            prev = current[sid]
            rec = existing_kodi.get(sid)
            if isinstance(rec, dict):
                if self._matches(current[sid], rec.get("applied")):
                    # still what we applied → keep the ORIGINAL previous value
                    prev = rec.get("previous", prev)
                # else: user drifted away → current becomes the new previous
            kodi_records[sid] = {"previous": prev, "applied": expected[sid]}

        addon_records = dict(existing_addons)
        if youtube_pending is not None:
            rec = addon_records.get(YOUTUBE_ADDON_ID)
            prev = current[YOUTUBE_ADDON_ID]
            if isinstance(rec, dict) and self._matches(
                    current[YOUTUBE_ADDON_ID], rec.get("applied")):
                prev = rec.get("previous", prev)
            addon_records[YOUTUBE_ADDON_ID] = {
                "setting": YOUTUBE_SETTING_ID,
                "previous": prev,
                "applied": YOUTUBE_EXPECTED,
            }
        return {
            "schema": BACKUP_SCHEMA,
            "kodi": kodi_records,
            "addons": addon_records,
            "applied_port": int(port),
        }

    def _rollback(self, applied, current, youtube, prior_bytes):
        all_ok = True
        for kind, key in reversed(applied):
            if kind == "kodi":
                if not self._write_kodi(key, current[key]):
                    all_ok = False
            else:
                if not self._write_addon(key, YOUTUBE_SETTING_ID, youtube):
                    all_ok = False
        if prior_bytes is not None:
            backup_ok = self._persist_backup_bytes(prior_bytes)
        elif all_ok:
            backup_ok = self._delete_backup()
        else:
            backup_ok = True
        if not all_ok:
            self._log("integration: rollback incomplete; backup kept", "error")
        if not backup_ok:
            self._log("integration: could not reinstate pre-attempt backup",
                      "error")
        return all_ok and backup_ok

    def _load_backup(self):
        try:
            with open(self._backup_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return _BACKUP_ABSENT
        except (OSError, ValueError):
            return _BACKUP_INVALID
        return data if self._valid_backup(data) else _BACKUP_INVALID

    @staticmethod
    def _valid_backup(data):
        if not isinstance(data, dict) or set(data) != {
                "schema", "kodi", "addons", "applied_port"}:
            return False
        if type(data["schema"]) is not int or data["schema"] != BACKUP_SCHEMA:
            return False
        if type(data["applied_port"]) is not int:
            return False
        kodi = data["kodi"]
        addons = data["addons"]
        if not isinstance(kodi, dict) or not isinstance(addons, dict):
            return False
        if not set(kodi).issubset(KODI_SETTING_IDS):
            return False
        expected_applied_types = {
            "network.usehttpproxy": bool,
            "network.httpproxytype": int,
            "network.httpproxyserver": str,
            "network.httpproxyport": int,
        }
        scalar_types = (bool, int, float, str)
        for sid, rec in kodi.items():
            if (not isinstance(rec, dict) or set(rec) != {"previous", "applied"}
                    or not isinstance(rec["previous"], scalar_types)
                    or type(rec["applied"]) is not expected_applied_types[sid]):
                return False
        if not set(addons).issubset((YOUTUBE_ADDON_ID,)):
            return False
        for rec in addons.values():
            if (not isinstance(rec, dict)
                    or set(rec) != {"setting", "previous", "applied"}
                    or rec["setting"] != YOUTUBE_SETTING_ID
                    or not isinstance(rec["previous"], scalar_types)
                    or type(rec["applied"]) is not int
                    or rec["applied"] != YOUTUBE_EXPECTED):
                return False
        return True

    def _read_backup_bytes(self):
        try:
            with open(self._backup_path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _persist_backup(self, backup):
        try:
            data = json.dumps(backup, indent=2).encode("utf-8")
        except (TypeError, ValueError):
            return False
        return self._persist_backup_bytes(data)

    def _persist_backup_bytes(self, data):
        tmp_path = self._backup_path + ".tmp"
        try:
            directory = os.path.dirname(self._backup_path) or "."
            os.makedirs(directory, exist_ok=True)
            with open(tmp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._backup_path)
            return True
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False

    def _delete_backup(self):
        try:
            os.remove(self._backup_path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------

    def _expected_values(self, host, port):
        host = str(host or DEFAULT_HOST).strip()
        if host in _ANY_HOSTS:
            host = DEFAULT_HOST
        return {
            "network.usehttpproxy": True,
            "network.httpproxytype": 0,
            "network.httpproxyserver": host,
            "network.httpproxyport": int(port),
        }

    @staticmethod
    def _matches(current, expected):
        """Type-loose equality so string reads still count as matching."""
        if current is None:
            return False
        if isinstance(expected, bool):
            truthy = str(current).strip().lower() in ("true", "1", "yes", "on")
            return truthy == expected
        if isinstance(expected, int):
            try:
                return int(str(current).strip()) == expected
            except (TypeError, ValueError):
                return False
        return str(current) == str(expected)

    # guarded adapters — integration must never raise out of the manager

    def _read_kodi(self, setting_id):
        try:
            return self._read_kodi_adapter(setting_id)
        except Exception:
            return None

    def _write_kodi(self, setting_id, value):
        try:
            return bool(self._write_kodi_adapter(setting_id, value))
        except Exception:
            return False

    def _addon_available(self):
        try:
            return bool(self._addon_available_adapter())
        except Exception:
            return False

    def _read_addon(self, addon_id, setting_id):
        try:
            return self._read_addon_adapter(addon_id, setting_id)
        except Exception:
            return None

    def _write_addon(self, addon_id, setting_id, value):
        try:
            return bool(self._write_addon_adapter(addon_id, setting_id, value))
        except Exception:
            return False

    def _log(self, msg, level="info"):
        if self._logger is None:
            return
        try:
            self._logger(msg, level)
        except Exception:
            pass

    def _notify_user(self, msg):
        if self._notify is None:
            return
        try:
            self._notify(msg)
        except Exception:
            pass
