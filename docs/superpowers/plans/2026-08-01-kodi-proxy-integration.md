# Kodi Proxy Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically align Kodi and supported addon proxy settings with Advanced Proxy's effective local port, then safely restore previous values on shutdown.

**Architecture:** A Kodi-free `IntegrationManager` owns compare/apply/backup/restore decisions. `helpers.py` supplies lazy Kodi JSON-RPC and `xbmcaddon` adapters. `main.py` invokes the manager only after a successful proxy start, after effective-port changes, and during shutdown/no-start cleanup.

**Tech Stack:** Python 3.11+, Kodi 21 Python API, stdlib `json`/`unittest`.

## Global Constraints

- Do not touch Kodi proxy username/password.
- Kodi system settings use `xbmc.executeJSONRPC`; addon settings use `xbmcaddon.Addon` typed accessors.
- YouTube absent/disabled is non-fatal and is never installed/enabled automatically.
- External settings are mutated only after backup persistence succeeds.
- Restore only values that still match what Advanced Proxy applied.
- Integration failure must never stop sing-box/Xray.

---

### Task 1: Add integration setting

**Files:**
- Modify: `service.advancedproxy/src/helpers.py`
- Modify: `service.advancedproxy/resources/settings.xml`
- Modify: `service.advancedproxy/resources/language/resource.language.en_gb/strings.po`
- Test: `tests/test_core.py`

**Interfaces:**
- Produces: `helpers.get_settings()["auto_configure_integration"] -> bool`, default `True`.

- [ ] **Step 1: Add failing default/normalization tests**

```python
def test_auto_configure_integration_defaults_true(self):
    self.assertTrue(helpers.get_settings(reader=lambda: {})["auto_configure_integration"])

def test_auto_configure_integration_can_be_disabled(self):
    raw = {"auto_configure_integration": "false"}
    self.assertFalse(helpers.get_settings(reader=lambda: raw)["auto_configure_integration"])
```

- [ ] **Step 2: Run the tests and confirm `KeyError`**

Run: `python3 -m unittest tests.test_core.TestHelpers -v`

- [ ] **Step 3: Add `_DEFAULTS` key, boolean normalization, settings toggle, and strings 32300–32301**
- [ ] **Step 4: Re-run affected and full tests**

Run: `python3 tests/test_core.py`

---

### Task 2: Implement Kodi-free integration state machine

**Files:**
- Create: `service.advancedproxy/src/proxy_integration.py`
- Create: `tests/test_proxy_integration.py`

**Interfaces:**
- Produces:

```python
class IntegrationManager:
    def __init__(self, backup_path, read_kodi, write_kodi,
                 addon_available, read_addon, write_addon,
                 logger=None, notify=None): ...
    def ensure_configured(self, host, port): ...
    def validate(self, host, port): ...
    def restore_previous(self): ...
    def backup_exists(self): ...
```

- [ ] **Step 1: Create fake reader/writer adapters and failing tests**

Cover: no-op when matching; effective-port correction; YouTube source 0/2→1; missing YouTube; backup-before-write; backup survival across restart; restore; user-change protection; stale-backup restore; failed backup persistence; partial-apply rollback.

- [ ] **Step 2: Run tests and confirm import failure**

Run: `python3 -m unittest tests.test_proxy_integration -v`

- [ ] **Step 3: Implement schema-v1 backup**

Backup shape:

```json
{
  "schema": 1,
  "kodi": {"setting.id": {"previous": null, "applied": null}},
  "addons": {"plugin.video.youtube": {"setting": "requests.proxy.source", "previous": 0, "applied": 1}},
  "applied_port": 1080
}
```

- [ ] **Step 4: Implement compare-and-set, rollback, and guarded restore**
- [ ] **Step 5: Run focused and full suites**

Run: `python3 -m unittest tests.test_proxy_integration tests.test_core -v`

---

### Task 3: Add Kodi adapters

**Files:**
- Modify: `service.advancedproxy/src/helpers.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Produces:

```python
def read_kodi_proxy_setting(setting_id): ...
def write_kodi_proxy_setting(setting_id, value): ...
def addon_available(addon_id): ...
def read_addon_setting(addon_id, setting_id): ...
def write_addon_setting(addon_id, setting_id, value): ...
def integration_backup_path(): ...
```

- [ ] **Step 1: Add structural failing tests for all adapter names and API calls**
- [ ] **Step 2: Confirm failure**
- [ ] **Step 3: Implement lazy imports and JSON-RPC response validation**
- [ ] **Step 4: Use typed integer accessors for YouTube `requests.proxy.source`**
- [ ] **Step 5: Verify bare-host import remains Kodi-free**

Run: `python3 -c "import sys; sys.path.insert(0,'service.advancedproxy/src'); import helpers"`

---

### Task 4: Wire lifecycle into service

**Files:**
- Modify: `service.advancedproxy/main.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `IntegrationManager`, helper adapters, `sup.effective_port`.

- [ ] **Step 1: Add failing source-structure tests**

Require construction, ensure after successful start/reconfigure, stale restore in no-start branches, toggle-off restore, and shutdown restore.

- [ ] **Step 2: Run tests and confirm missing wiring**
- [ ] **Step 3: Implement non-fatal lifecycle wiring**
- [ ] **Step 4: Verify full suite**

Run: `python3 -m unittest tests.test_core tests.test_proxy_integration -v`

---

### Task 5: LibreELEC acceptance

**Files:**
- Create: `docs/manual-qa-libreelec.md`

- [ ] Install updated ZIP on `root@192.168.31.133` through Kodi UI.
- [ ] Start with Kodi proxy disabled and YouTube proxy source 0; confirm automatic correction.
- [ ] Confirm YouTube traffic appears in `engine.log`.
- [ ] Occupy 1080; restart and confirm Kodi follows fallback port.
- [ ] Disable Advanced Proxy and confirm previous settings restore.

---

### Commit boundaries

1. `feat: add proxy integration setting`
2. `feat: add proxy integration state manager`
3. `feat: add Kodi proxy settings adapters`
4. `feat: wire proxy integration into service lifecycle`
