# Subscription Groups — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Add subscription groups to Advanced Proxy per the approved design `docs/superpowers/specs/2026-08-03-subscriptions-design.md`: paste detection (profile vs subscription URL), plain/base64 subscription parsing, mirror sync, cascade delete, shared refresh interval, protocol toggles, availability check at activation, copy-link action, and a Subscriptions settings tab.

Work only in `/home/random/dev/kodi-advanced-proxy/.worktrees/proxy-integration-release`.

## Architecture

Keep the Kodi-free module split. New module `src/subscriptions.py` mirrors `profiles.py` style. `parsers.py` gains protocol filtering and subscription-URL detection. `profiles.py` gains the `subscription` field, bulk add, and cascade remove. `supervisor.tick()` gains refresh scheduling. `helpers.py` gains the new settings and a clipboard helper. `default.py` + `settings.xml` + `strings.po` gain the Subscriptions tab and actions. All behavior changes are TDD-driven in `tests/test_core.py` first.

## Global Constraints

* Strict TDD for every behavior change: write the smallest focused test first, run it with `python3 -m unittest`, observe and record the expected failure, then make the minimal implementation change and rerun. Do not mark a task complete from a test that was never observed failing.
* Use the actual existing test classes and fakes in `tests/test_core.py` (`TestBinaryManager`, `TestSupervisorTick`, `TestMainLifecycleWiring`, `_FakeClock`, `_LogRecorder`, `_run_main`, patching style). Add focused test methods or a new behavior-named class in an existing test module; never create invented test modules or fixture architectures.
* Do not invent constructors, helper APIs, or compatibility layers. Preserve `ProfileStore(path)`, `parsers.parse_uri(line)`, `ProxySupervisor(...)`, and `helpers.get_settings()` contracts; extend them with optional arguments only.
* No cross-instance locks, PID files, automatic addon restarts, or changes to engine binaries, publication pipeline, or the pre-existing untracked `bootstrap/README.md`.
* Preserve the existing watchdog semantics (exit-driven backoff 2,4,8... cap 60, recovery reset, give-up after 11), restore-before-stop, readiness-gated integration, and single-pass port resolution.
* Subscriptions must never break existing profiles on a failed fetch or malformed body: failure records `last_error` and leaves profiles untouched.
* Manual profiles always win on URI de-duplication; disabled-protocol links are skipped at parse time, never stored.
* English PLAIN commit messages only, no conventional prefixes. Prefix every git command with `GIT_MASTER=1`. Do not force push.

## Task 1: Subscription decode + store (RED → GREEN)

- [ ] RED: in `tests/test_core.py` add focused tests for `decode_subscription` in the new `subscriptions.py` behavior class: plain text with one vless per line decodes; standard base64 decodes; URL-safe base64 with newlines decodes; garbage does not decode; a body with zero profile lines after decoding is an error; a >1MB body is refused. Observe the failures (module missing / function missing).
- [ ] RED: `SubscriptionStore` tests: `add(url)` with an injected fetcher records the group and its profiles into a temp `profiles.json`-style store; `remove(group_id)` cascade-deletes only that group's profiles and re-picks the active profile when it was removed; `due(now, interval_hours)` returns only groups past `last_updated + N*3600`; a group with `interval_hours == 0` (never) is never due.
- [ ] GREEN: create `service.advancedproxy/src/subscriptions.py` implementing `decode_subscription(body, max_bytes=1<<20)`, `fetch(url, timeout=10, max_bytes=1<<20)` (urllib-based, injectable), and `SubscriptionStore(path)` with `load/save/add/remove/refresh/due/groups/get`. Use `json` and `base64` standard library only. `refresh` performs mirror sync: parse links, drop disabled protocols via `parsers`, add new, remove missing, keep active when present, set `last_updated`/`last_error`.
- [ ] Run the focused new tests, then `TestBinaryManager` and `TestProfileStore` regressions. Record RED and GREEN evidence.

## Task 2: parsers + profiles integration (RED → GREEN)

- [ ] RED: `parsers.parse_uri(line, disabled_protocols=())` returns None for a line whose protocol is in `disabled_protocols` (vless/trojan/hysteria2 cases); `parsers.is_subscription_url(line)` is True for `https://example.com/sub` and False for `vless://...` and junk. `parse_lines` reports disabled-protocol lines as skipped, not errors.
- [ ] RED: `ProfileStore.add_uri(uri, subscription=None)` persists the `subscription` field; `add_subscription_profiles(parsed, group_id)` de-dups by URI keeping the manual profile and skipping the subscription copy; `remove_by_subscription(group_id)` removes exactly that group's profiles and re-picks the active profile per existing semantics.
- [ ] GREEN: extend `parsers.py` (`disabled_protocols` optional param, `is_subscription_url`, skip reporting) and `profiles.py` (`subscription` field, `add_subscription_profiles`, `remove_by_subscription`) with minimal changes; keep all existing callers working.
- [ ] Run focused parser/profile tests, then full `test_core` regressions. Record RED and GREEN.

## Task 3: supervisor refresh scheduling + availability check (RED → GREEN)

- [ ] RED: `TestSupervisorTick`-style tests for refresh: with `subscription_interval_hours=N` and a due group, one `tick()` triggers exactly one refresh via an injected refresher; a refresh already in flight is not re-entered; after refresh removes the active profile, the supervisor re-picks another enabled profile; settings change re-arms the scheduler. Also: an availability check at activation — selecting an unreachable profile warns and offers the next reachable enabled profile (inject a prober).
- [ ] GREEN: `supervisor.py` gains a `_maybe_refresh_subscriptions()` called from `tick()` honoring the shared interval and a re-entrancy guard; activation helpers in `main.py`/`default.py` path gain the TCP availability check using `helpers.measure_latencies` prober semantics. Add the new settings to `helpers.get_settings()` (`subscription_interval_hours`, `disable_proto_*`).
- [ ] Run focused supervisor tests + `TestMainLifecycleWiring` + `TestSupervisorPortFallback` + watchdog regressions. Record RED and GREEN.

## Task 4: UI wiring — settings tab + actions (RED → GREEN)

- [ ] RED: extend the `_run_main`/`_FakeSupervisor`/listing harness in `tests/test_core.py` so subscription actions (`sub_add`, `sub_refresh&id=`, `sub_remove&id=`, `copy&tag=`) are dispatched by `default.py` `main()` routing; a `sub_add` with a subscription URL calls the store's add and notifies; `sub_remove` cascades. Settings XML contract: the new category contains `subscription_url`, `subscription_interval_hours`, and the three `disable_proto_*` toggles (assert via `validate_addon.py`-style checks or direct XML parse).
- [ ] GREEN: wire `default.py` actions, add the settings category to `settings.xml`, add label ids to `strings.po`. Keep the profile listing and context-menu copy-link behavior.
- [ ] Run the focused wiring tests, `tests/test_packaging` (settings/strings validation), and the full suite. Record RED and GREEN.

## Task 5: Verification (local + live)

- [ ] Run the full suite with `python3 -m unittest discover -s tests -v`; require all tests to pass.
- [ ] Run `bash scripts/check_versions.sh .` and `python3 scripts/validate_addon.py .`; require both to pass (settings.xml and strings.po must validate).
- [ ] Build a fresh armv7 ZIP (`./build.sh linux_armv7`) and deploy to `192.168.31.174` (`/storage/downloads/`, LibreELEC, root/libreelec).
- [ ] Live QA on device: add a real subscription URL via the addon UI or direct store manipulation; confirm profiles appear; refresh and confirm mirror sync (added/removed); delete the group and confirm profiles disappear and the active profile re-picks; confirm a disabled-protocol link from a subscription is skipped; confirm activation skips an unreachable profile. Record results.

## Verification checklist

- [ ] Full suite passes.
- [ ] `check_versions.sh` and `validate_addon.py` pass.
- [ ] Live QA on 192.168.31.174 passes (add/refresh/delete/cascade/protocol-skip/availability-skip).
- [ ] No regression in lifecycle, watchdog, port fallback, or publication tests.
