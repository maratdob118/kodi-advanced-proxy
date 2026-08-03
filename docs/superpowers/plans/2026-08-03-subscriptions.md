# Subscription Groups — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Add subscription groups to Advanced Proxy per the approved design `docs/superpowers/specs/2026-08-03-subscriptions-design.md`: paste detection (profile vs subscription URL), plain/base64 subscription parsing, mirror sync, cascade delete, shared refresh interval, protocol toggles, availability check at activation, copy-link action, and a Subscriptions settings tab.

Work only in `/home/random/dev/kodi-advanced-proxy/.worktrees/proxy-integration-release`.

## Architecture

Keep the Kodi-free module split. New module `src/subscriptions.py` mirrors `profiles.py` style. `parsers.py` gains protocol filtering and subscription-URL detection. `profiles.py` gains the `subscription` field, bulk add, and cascade remove. `supervisor.tick()` gains refresh scheduling. `helpers.py` gains the new settings, `disabled_protocols()`, and `copy_to_clipboard()`. `default.py` + `settings.xml` + `strings.po` gain the Subscriptions tab and actions. All behavior changes are TDD-driven in `tests/test_core.py` first.

## Global Constraints

* Strict TDD for every behavior change: write the smallest focused test first, run it with `python3 -m unittest`, observe and record the expected failure, then make the minimal implementation change and rerun. Do not mark a task complete from a test that was never observed failing.
* During GREEN, run ONLY the newly written tests first to prove they flip; run the broader/full suite only after that GREEN observation is recorded.
* Use the actual existing test classes and fakes in `tests/test_core.py` (`TestBinaryManager`, `TestSupervisorTick`, `TestMainLifecycleWiring`, `_FakeClock`, `_LogRecorder`, `_run_main`, patching style). Add focused test methods or a new behavior-named class in an existing test module; never create invented test modules or fixture architectures.
* Do not invent constructors, helper APIs, or compatibility layers. Preserve `ProfileStore(path)`, `parsers.parse_uri(line)`, `ProxySupervisor(...)`, and `helpers.get_settings()` contracts; extend them with optional arguments only.
* No cross-instance locks, PID files, automatic addon restarts, or changes to engine binaries, publication pipeline, or the pre-existing untracked `bootstrap/README.md`.
* Preserve the existing watchdog semantics (exit-driven backoff 2,4,8... cap 60, recovery reset, give-up after 11), restore-before-stop, readiness-gated integration, and single-pass port resolution.
* Subscriptions must never break existing profiles on a failed fetch or malformed body: failure records `last_error` and leaves profiles untouched.
* Manual profiles always win on URI de-duplication; disabled-protocol links are skipped at parse time, never stored.
* **`skip_protocols` vs toggles:** the legacy `skip_protocols` comma-string setting is superseded by the per-protocol boolean toggles. `helpers.disabled_protocols()` returns the union of toggled protocols and the legacy string values (so existing saved values keep working). The settings UI exposes the toggles; the legacy text field is removed from `settings.xml` but its saved value is still honored through `disabled_protocols()`.
* **urltest mode note:** in urltest mode the active profile is chosen by latency, not `active_tag`. A subscription refresh that changes the profile set triggers a latency re-measurement and reconfiguration in both modes (manual: re-pick active if removed; urltest: re-run measurement).
* English PLAIN commit messages only, no conventional prefixes. Prefix every git command with `GIT_MASTER=1`. Do not force push.

## Task 1: Subscription decode + store (RED → GREEN)

- [ ] RED: in `tests/test_core.py` add focused tests for `decode_subscription` (new module `subscriptions.py`): plain text with one vless per line decodes; standard base64 decodes; URL-safe base64 WITH newlines AND WITHOUT `=` padding decodes; **fallback order: a body that is both valid UTF-8 text containing a profile-scheme line AND valid base64 decodes as text (text wins)**; garbage does not decode; a body with zero profile lines after decoding is an error; a >1MB body is refused. Observe failures (module missing).
- [ ] RED: `SubscriptionStore` tests matching the spec signatures:
  - `add(url, fetcher, profile_store)` records the group and adds its profiles via `profile_store.add_subscription_profiles` (injected fetcher returns a fixed body).
  - `remove(group_id, profile_store)` delegates the cascade to `profile_store.remove_by_subscription(group_id)` and deletes the group.
  - `refresh(group_id, fetch, parse, profile_store)` with injected `fetch`/`parse` performs mirror sync: returns `(added, removed, error)`; new links added, missing links removed, active profile kept when present, `last_updated`/`last_error` set; a failed fetch leaves profiles untouched and returns an error.
  - `due(now, interval_hours)` returns only groups past `last_updated + N*3600`; a group with `interval_hours == 0` (never) is never due.
- [ ] GREEN: create `service.advancedproxy/src/subscriptions.py` implementing `decode_subscription(body, max_bytes=1<<20)`, `fetch(url, timeout=10, max_bytes=1<<20)` (urllib-based, injectable), and `SubscriptionStore(path)` with `load/save/groups/get/add/remove/refresh/due`. Use `json` and `base64` standard library only. Mirror sync: parse links, drop disabled protocols via `parsers`, add new, remove missing, keep active when present.
- [ ] Run the focused new tests (GREEN evidence), then `TestProfileStore` regressions.

## Task 2: parsers + profiles integration (RED → GREEN)

- [ ] RED: `parsers.parse_uri(line, disabled_protocols=())` returns None for a line whose protocol is in `disabled_protocols` (vless/trojan/hysteria2 cases); `parsers.is_subscription_url(line)` is True for `https://example.com/sub` and False for `vless://...` and junk; `parse_lines` reports disabled-protocol lines as skipped, not errors.
- [ ] RED: `ProfileStore.add_uri(uri, subscription=None)` persists the `subscription` field; `add_subscription_profiles(parsed, group_id)` de-dups by URI keeping the manual profile and skipping the subscription copy; `remove_by_subscription(group_id)` removes exactly that group's profiles and re-picks the active profile per existing semantics.
- [ ] GREEN: extend `parsers.py` (`disabled_protocols` optional param, `is_subscription_url`, skip reporting) and `profiles.py` (`subscription` field, `add_subscription_profiles`, `remove_by_subscription`) with minimal changes; implement `helpers.disabled_protocols()` (union of toggle settings + legacy `skip_protocols` values) and `helpers.copy_to_clipboard(text)` (Kodi clipboard API with a text-view dialog fallback); add the new settings to `helpers.get_settings()` (`subscription_interval_hours`, `disable_proto_vless`, `disable_proto_trojan`, `disable_proto_hysteria2`).
- [ ] Run the focused parser/profile/helper tests (GREEN evidence), then full `test_core` regressions.

## Task 3: supervisor refresh scheduling (RED → GREEN)

- [ ] RED: `TestSupervisorTick`-style tests for refresh scheduling:
  - with `subscription_interval_hours=N` and a due group, one `tick()` triggers exactly one refresh via an injected refresher; a refresh already in flight is not re-entered.
  - **watchdog interaction: a subscription refresh due during watchdog backoff does not delay or cancel the pending restart** (restart still fires on schedule).
  - after refresh removes the active profile in manual mode, the supervisor re-picks another enabled profile AND rebuilds the engine config / reconfigures (assert the config write + restart path, not just `active_tag`).
  - after refresh changes the profile set in urltest mode, latency re-measurement and reconfiguration happen.
- [ ] GREEN: `supervisor.py` gains `_maybe_refresh_subscriptions()` called from `tick()`, honoring the shared interval, a re-entrancy guard, and the mode-aware post-refresh reconfiguration (manual: re-pick active + reconfigure if removed; urltest: re-measure + reconfigure). Reuse the existing config write/start paths.
- [ ] Run the focused supervisor tests (GREEN evidence), then `TestSupervisorTick` + `TestSupervisorPortFallback` + watchdog regressions.

## Task 4: UI wiring — settings tab + actions (RED → GREEN)

- [ ] RED: extend the listing harness in `tests/test_core.py` so `default.py` `main()` dispatches `sub_add`, `sub_refresh&id=`, `sub_remove&id=`, `copy&tag=`; `sub_add` with a subscription URL calls the store's add and notifies; `sub_remove` cascades via the store. `helpers.build_directory_entries` emits **group rows** (url, status, refresh/remove URLs) in addition to profile rows, and profile rows carry a copy-link URL. Settings XML contract: the new `subscriptions` category contains `subscription_url`, `subscription_interval_hours`, the three `disable_proto_*` toggles, an **"Open subscriptions" action button**, and no legacy `skip_protocols` text field.
- [ ] RED: **availability check at activation** (spec places it in the activation path, not tick): `_action_activate` probes the selected profile's `server:port`; an unreachable profile warns and offers the next reachable enabled profile instead of activating it; a reachable profile activates normally (inject a prober).
- [ ] GREEN: wire `default.py` actions (`sub_add`, `sub_refresh`, `sub_remove`, `copy`), extend `helpers.build_directory_entries` with group rows and copy-link entries, add the `subscriptions` category to `settings.xml` (with the "Open subscriptions" button, toggles, interval, URL field; remove the legacy text field), add label ids to `strings.po`, and implement the availability probe in `_action_activate`.
- [ ] Run the focused wiring tests (GREEN evidence), then `tests/test_packaging` (settings/strings validation) and the full suite.

## Task 5: Verification (local + live)

- [ ] Run the full suite with `python3 -m unittest discover -s tests -v`; require all tests to pass.
- [ ] Run `bash scripts/check_versions.sh .` and `python3 scripts/validate_addon.py .`; require both to pass (settings.xml and strings.po must validate).
- [ ] Build a fresh armv7 ZIP (`./build.sh linux_armv7`) and deploy to `192.168.31.174` (`/storage/downloads/`, LibreELEC, root/libreelec).
- [ ] Live QA on device: add a real subscription URL; confirm profiles appear; refresh and confirm mirror sync (added/removed); delete the group and confirm profiles disappear and the active profile re-picks; confirm a disabled-protocol link from a subscription is skipped; confirm activation skips an unreachable profile. Record results.

## Verification checklist

- [ ] Full suite passes.
- [ ] `check_versions.sh` and `validate_addon.py` pass.
- [ ] Live QA on 192.168.31.174 passes (add/refresh/delete/cascade/protocol-skip/availability-skip).
- [ ] No regression in lifecycle, watchdog, port fallback, or publication tests.
