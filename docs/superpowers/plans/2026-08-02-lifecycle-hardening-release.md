# Lifecycle Hardening and Two-Repository Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Close the confirmed Kodi shutdown race, make engine start and stop observable and bounded, keep Kodi integration behind listener readiness, preserve stable effective-port behavior, and retarget publication to the two public repositories approved in design commit `2c91911`:

* Source monorepo: `maratdob118/kodi-advanced-proxy`.
* Generated Kodi repository and GitHub Pages site: `maratdob118/kodi-addons`.

The implementation must be executable task-by-task by subagents without inventing new public APIs, constructors, helpers, or test modules. Work only in `/home/random/dev/kodi-advanced-proxy/.worktrees/proxy-integration-release`. This plan is implementation guidance, not permission to touch the private `/home/random/dev/bigping` or the GitHub repository `maratdob118/bigping`.

## Architecture

Keep the current Kodi-free module split under `service.advancedproxy/src/`.

* `binary_manager.BinaryManager` remains constructed with the current signature `__init__(addon_dir, work_dir, engine="sing-box", platform_override="auto", logger=None, custom_path="")`. Harden its existing lifecycle methods, with the approved public signatures:
  * `start(config_path, port=None, ready_timeout=10.0)`.
  * `stop(port=None, term_timeout=5.0, kill_timeout=5.0, release_timeout=5.0) -> bool`.
  * `restart(config_path, port=None, ready_timeout=10.0, term_timeout=5.0, kill_timeout=5.0, release_timeout=5.0)` or an equivalent forwarding shape that preserves the required `config_path` call and forwards the effective port and readiness/stop bounds.
* `supervisor.ProxySupervisor` keeps its current constructor signature `__init__(settings, addon_dir, work_dir, logger=None, notify=None)` and its existing public methods `start`, `stop`, `restart`, `reconfigure_engine`, and `tick`. Add the approved optional injected `should_stop` without inventing a separate constructor or service abstraction. The supervisor tracks the explicit states `stopped`, `starting`, `running`, `restart_pending`, and `shutting_down` through existing attributes or a minimal internal representation.
* `ProxySupervisor.start()` resolves the effective port once, writes and validates the config, calls `BinaryManager.start(config_path)` through the readiness-aware path with the effective port, and returns `True` only when readiness is confirmed. `reconfigure_engine()` resolves once for the new engine/config and calls an internal already-resolved start path instead of calling the public resolver twice. `tick()` never resolves a port or rewrites the config.
* `main.py` creates `xbmc.Monitor` before constructing the supervisor, injects `monitor.abortRequested` as `should_stop`, calls `begin_shutdown()` before breaking out of `monitor.waitForAbort(3)`, calls it again after the loop for idempotence, then preserves `integration.shutdown()` before `sup.stop()`.
* `build_singbox.build_config()` continues to emit the existing `urltest` and manual chooser shapes. `urltest` always emits `interrupt_exist_connections: false`; the manual selector continues to honor `settings["interrupt_connections"]`. Make no Xray connection-preservation claim.
* Publication keeps the current generator, publisher, release planner, Pages workflow template, and managed-file boundary. Only constants, URLs, token naming, identity, labels, and related documentation are retargeted unless a focused occurrence search during execution proves another reference must change.

## Tech Stack

* Python 3 standard library and `python3 -m unittest`.
* Kodi 20+/21 Python APIs, including `xbmc.Monitor`.
* sing-box `1.13.14` and Xray-core `25.8.3`, with no engine binary changes.
* Bash build and release tooling. The supported ARM64 build command is `./build.sh linux_arm64`.
* GitHub Actions, GitHub Releases, GitHub Pages, and GitHub CLI (`gh`) for the publication runbook.

## Global Constraints

* Follow strict TDD for every behavior change: write the smallest focused test first, run it with `python3 -m unittest` and record the expected failure, make the minimal implementation change, rerun the focused test, then run relevant regressions. Do not mark a task complete from a test that was never observed failing before the implementation.
* Use the actual existing test classes `TestBinaryManager`, `TestBuildSingbox`, `TestSupervisorReconfigureEngine`, `TestSupervisorPortFallback`, `TestMainLifecycleWiring`, and `TestSupervisorTick`. Add focused test methods to the existing modules and classes where they belong. If a lifecycle grouping needs a new class, use only a class in an existing test module and name it from the behavior, never create an invented test module or fixture architecture.
* Use the actual existing fake objects and patching style in `tests/test_core.py` where possible: `_FakeBin`, `_FakeBinaryManager`, `_FakeProcess`, `_FakeClock`, `_FakeIntegrationManager`, `_FakeSupervisor`, `_run_main`, `_patched`, `_kodi_module`, `_LogRecorder`, and `_NotifyRecorder`. Extend an existing fake only when the new behavior requires it, and keep the extension minimal.
* Do not invent constructors, helper APIs, or compatibility layers. In particular, do not replace `BinaryManager.start(config_path)`, `stop()`, or `restart(config_path)` with unrelated abstractions. Preserve current callers and update them only to pass the approved effective-port/readiness arguments.
* Do not add a cross-instance lock, PID file, lock file, port ownership marker, periodic restart timer, automatic YouTube installation/enabling, automatic YouTube restart, or any Xray preservation claim. Stale YouTube sessions after a port drift are documented as a known limitation; automatic addon restart remains out of scope.
* Preserve the existing one-shot `port_utils.find_free_port` fallback. The selected `effective_port` stays stable for the session. A busy configured port may resolve to the next free port at public start or reconfigure, but `tick()` must not re-resolve it.
* Preserve the existing restore-before-engine-stop ordering. Integration failures remain non-fatal to the engine. A failed or readiness-timed-out start must use the existing `IntegrationLifecycle.sync(..., running=False, ...)` restore path and must never call `ensure_configured`.
* Preserve the existing watchdog semantics for non-shutdown crashes: exit-driven restarts only, exponential backoff `2, 4, 8, ...` capped at 60 seconds, recovery reset, and give-up after 11 consecutive failures. Shutdown must never notify a crash, increment failures, arm a restart, or fire a restart.
* Do not run shell, Git, tests, SSH, network, or deployment commands while authoring this plan. The commands in later execution and verification checkboxes are for the implementing agent.
* The worktree already has a large pre-existing dirty release diff and staged tracked-binary deletions. Do not discard, restore, or rewrite those deletions. Do not alter unrelated files. Do not commit source implementation as part of plan authoring.
* English PLAIN commit messages only, with no conventional prefixes. Git and commit planning happen only after the final diff has been inspected. Do not force push.
* Never print secret values. Secret checks must use a safe, non-printing pattern or a script that reports only file names and pass/fail results, never raw matches or environment values.

## 1. Establish the RED baseline without disturbing the release diff

- [ ] Read the approved spec `docs/superpowers/specs/2026-08-02-lifecycle-hardening-design.md`, the current source and test files named above, `build.sh`, `.github/workflows/release.yml`, `scripts/generate_repo.py`, `scripts/publish_repo.py`, the Pages template under `bootstrap/bigping.repository/`, `repository.bigping/addon.xml`, and `.debug-journal.md`; record current signatures and the exact files that will be touched.
- [ ] Before any implementation edit, run the focused current baseline with `python3 -m unittest` for the relevant existing classes or modules. Confirm the pre-change suite status and preserve the known 485+ passing baseline as the regression target.
- [ ] For each behavior below, add only the test first, run it with `python3 -m unittest`, and observe the expected failure before editing implementation. Keep each RED assertion specific enough to identify the missing behavior rather than relying on a broad integration failure.

## Task 1: Harden `BinaryManager.stop()` and startup readiness

### 2.1 RED tests in `tests/test_core.py`

- [ ] Extend `TestBinaryManager` with a focused `stop()` test using a fake process whose `terminate()` causes a later `poll()`/`wait()` exit. Assert SIGTERM happens first, the handle remains available until exit is confirmed, and `self.proc` becomes `None` only after confirmed exit.
- [ ] Add the escalation case to the same binary-manager test area. Make `wait(term_timeout)` time out, assert SIGKILL follows SIGTERM, and assert `wait(kill_timeout)` is called after SIGKILL.
- [ ] Add the refusal case. Make both waits time out, assert `stop(...)` returns `False`, retains the process handle, and logs the unresolved process instead of silently clearing `self.proc`.
- [ ] Add bounded listener-release coverage by patching the existing `port_utils.port_in_use` dependency and the module sleep/time boundary used by the implementation. Assert polling continues until the listener is free. Add the busy-listener case and assert it logs after `release_timeout` while still returning `True` once process death was confirmed.
- [ ] Add a focused startup readiness test group in `tests/test_core.py`, reusing the existing fake process style. Assert `BinaryManager.start(config_path, port=...)` returns the process only after the listener becomes available and the process remains alive.
- [ ] Add readiness-timeout and process-exit-during-readiness tests. Assert the spawned process is stopped through the hardened path and startup raises or otherwise reports failure according to the existing `BinaryManager` error contract, without leaving a live process handle falsely classified as ready.
- [ ] Add a restart forwarding test that proves `BinaryManager.restart(config_path, port=...)` uses the hardened stop and readiness-aware start, and that the effective port reaches both phases.

### 2.2 Minimal GREEN implementation

- [ ] Change only `service.advancedproxy/src/binary_manager.py` for the binary lifecycle. Implement SIGTERM plus bounded wait, SIGKILL plus bounded wait, post-kill wait, handle retention until `poll()` confirms exit, and bounded listener-release polling in `stop(...)`.
- [ ] Add the `port` and `ready_timeout` path to `start(config_path, ...)`. After `Popen`, poll `port_utils.port_in_use(port)` in 100 ms steps while checking that the process stays alive. On timeout or early exit, stop the spawned process through the hardened stop and raise the existing start failure shape.
- [ ] Update `restart(config_path, ...)` to delegate to the hardened stop and readiness-aware start, forwarding the effective port and bounds without changing unrelated engine preparation or validation behavior.
- [ ] Run focused `TestBinaryManager` and startup readiness tests with `python3 -m unittest`, then run the relevant existing `TestBinaryManager` and packaging/core regressions. Do not proceed until RED and GREEN evidence is recorded.

## Task 2: Make `ProxySupervisor` shutdown-aware and single-pass for ports

### 3.1 RED tests in `tests/test_core.py`

- [ ] Add shutdown-state coverage to `TestSupervisorTick`: after a clean process exit, call `begin_shutdown()` before `tick()` and assert no restart is armed, no restart is started, no failure counter increments, and no error notification is emitted.
- [ ] Add a pending-watchdog cancellation test. Set `_restart_at`, call `begin_shutdown()`, and assert `_restart_at` is cleared. Call `stop()` during shutdown and assert the watchdog remains cancelled and the state file reports `running: false`.
- [ ] Add the optional injected `should_stop` coverage to the supervisor tests. Assert an exit observed while `should_stop()` is true is classified as shutdown, and assert a restart that is about to fire is cancelled when `should_stop()` becomes true before the fire decision.
- [ ] Add no-restart-after-shutdown assertions that call public `start()` and `restart()` after shutdown and verify neither starts an engine. Keep `stop()` idempotent and safe when no process exists.
- [ ] Extend `TestSupervisorReconfigureEngine` and `TestSupervisorPortFallback` with a resolver call counter. Assert public `start()` resolves exactly once, public `reconfigure_engine()` resolves exactly once, the resolved port is used in the generated config and engine start/restart, and `tick()` neither resolves nor rewrites the config.
- [ ] Preserve and explicitly assert the existing old-manager-stop-before-new-manager-start ordering in `TestSupervisorReconfigureEngine`, now with the resolved effective port passed through the internal already-resolved start path.
- [ ] Add or extend `TestSupervisorTick` regressions for healthy engines never restarting from elapsed time, non-shutdown crash backoff, recovery reset, and give-up after 11 failures. These are behavior locks, not permission to add a timer.

### 3.2 Minimal GREEN implementation

- [ ] Change only `service.advancedproxy/src/supervisor.py` for supervisor lifecycle behavior. Add the explicit shutdown guard and idempotent `begin_shutdown()` that clears `_restart_at`. Ensure shutdown is entered before process-exit classification and that `tick()` only clears an exited handle/state as needed without crash notification or watchdog activity.
- [ ] Add the optional `should_stop` injection to the existing `ProxySupervisor` constructor without changing existing positional call compatibility. Consult it both when an exit is detected and immediately before a pending restart fires.
- [ ] Make `stop()` call `begin_shutdown()` internally, pass the effective port to `BinaryManager.stop(...)`, and persist `state.json` with `running: false`. Ensure `start()` and `restart()` are no-ops after shutdown.
- [ ] Refactor the smallest possible internal path so public `start()` performs one `_resolve_effective_port()` call, while `reconfigure_engine()` resolves once and calls an internal already-resolved start path. Forward `self.effective_port` to the binary manager. Do not re-resolve in `tick()` and do not rebuild the config from `tick()`.
- [ ] Keep `ProxySupervisor.start()`'s success meaning as readiness confirmed, keep `reconfigure_engine()`'s result aligned with readiness, and preserve existing config validation, state writing, profile tracking, and failure logging.
- [ ] Run focused supervisor classes with `python3 -m unittest`, then run all existing lifecycle, port fallback, and watchdog regressions. Stop at the first successful focused verification before doing broader verification later in this plan.

## Task 3: Wire monitor shutdown and readiness-gated integration in `main.py`

### 4.1 RED tests in `tests/test_core.py`

- [ ] Extend `TestMainLifecycleWiring` to assert `xbmc.Monitor` is available before supervisor construction and that its `abortRequested` callable is injected as `should_stop`.
- [ ] Add an event-order assertion for an aborting monitor: `begin_shutdown()` must occur before the monitor loop exits, followed by the idempotent post-loop shutdown call, `integration.shutdown()`, and `sup.stop()`.
- [ ] Add a readiness-gate assertion that a start returning false does not call `ensure_configured` and does call the existing stale-backup restore path. Keep the existing successful effective-port configuration assertion.
- [ ] Add a reconfigure readiness assertion that `ensure_configured` receives `sup.effective_port` only after a readiness-confirmed `reconfigure_engine()` result. Preserve `restore` before `sup.stop` and the existing profile-change behavior.
- [ ] If the current `_FakeSupervisor` does not expose the new observable events, extend only that existing fake and its `_run_main` harness so the tests can observe the actual wiring. Do not create an alternate main harness.

### 4.2 Minimal GREEN implementation

- [ ] Change `service.advancedproxy/main.py` so `monitor = xbmc.Monitor()` is created before `ProxySupervisor`, and pass `should_stop=monitor.abortRequested` into the current supervisor constructor.
- [ ] On `monitor.waitForAbort(3)` returning true, call `sup.begin_shutdown()` before `break`. After the loop call `sup.begin_shutdown()` again, then `integration.shutdown()`, then `sup.stop()`. Preserve restore-before-stop exactly.
- [ ] Keep `_sync_integration` driven by the boolean result of `sup.start()` and `sup.reconfigure_engine()`, so `ensure_configured` runs only after readiness and uses `sup.effective_port`. On readiness failure, retain the existing `running=False` restore behavior.
- [ ] Run `TestMainLifecycleWiring` and the existing integration lifecycle tests with `python3 -m unittest`, then run the focused supervisor regressions.

## Task 4: Lock sing-box connection behavior

### 5.1 RED tests in `tests/test_core.py`

- [ ] Extend `TestBuildSingbox` with `interrupt_connections=True` and `False` cases for `mode="urltest"`, asserting the generated urltest outbound always has `interrupt_exist_connections: false`.
- [ ] Add manual-mode assertions for both setting values, asserting the selector's `interrupt_exist_connections` continues to equal the user setting.

### 5.2 Minimal GREEN implementation

- [ ] Change only the chooser construction in `service.advancedproxy/src/build_singbox.py`: force the urltest field to `False`, and leave the manual selector expression tied to `settings["interrupt_connections"]`.
- [ ] Run the focused `TestBuildSingbox` tests with `python3 -m unittest`, then run all core configuration regressions. Do not modify Xray config generation or claim Xray connection preservation.

## Task 5: Retarget generator, metadata, publisher, fixtures, and workflows

### 6.1 RED contract tests first

- [ ] Update the relevant current test constants and fixtures in `tests/test_repository_generation.py`, `tests/test_repository_publish.py`, `tests/test_workflow_contracts.py`, `tests/test_site_builder.py`, `tests/test_release_logic.py`, and `tests/test_packaging.py` only after adding assertions that fail against the old targets. Assert the new source repository, Pages base URL, target repository, token environment name, and bot identity. Keep secret fixture values synthetic and never print them.
- [ ] Add generator contract assertions that `scripts/generate_repo.py` uses `PAGES = "https://maratdob118.github.io/kodi-addons/"` and `SOURCE_REPO = "maratdob118/kodi-advanced-proxy"`, and that generated release asset URLs point to the new source repository.
- [ ] Add publisher contract assertions that `scripts/publish_repo.py` uses `DEFAULT_REPOSITORY = "maratdob118/kodi-addons"`, `TOKEN_ENV` or its current equivalent resolves to `KODI_ADDONS_TOKEN`, and the target commit identity is `kodi-addons-release-bot` with a GitHub noreply address. Assert the publisher still manages only the existing five generated paths, never `.github`, and never force-pushes.
- [ ] Add metadata assertions for `repository.bigping/addon.xml` URLs, including `info`, `checksum`, `datadir`, `website`, and `source`, all pointing at the new Pages/source locations with HTTPS.
- [ ] Add workflow contract assertions for the source workflow's repository labels and concurrency labels, the cross-repository token name and job scoping, and the Pages template's new repository comments/URLs. Keep the existing ordering of unittest, version, metadata, platform build, universal packaging, release, generation, and publication checks.
- [ ] Perform a focused, safe occurrence search during execution for the old source and target repository names, old Pages URL, old token name, and old bot identity. The search must report only paths and line context that contains no credential values. Update every relevant occurrence found in scripts, workflows, tests, fixtures, bootstrap docs, generated metadata, and comments. Confirm no workflow, script, or publish step references, clones, or writes to private `maratdob118/bigping`.

### 6.2 Minimal GREEN retargeting

- [ ] Change `scripts/generate_repo.py` constants `PAGES` and `SOURCE_REPO` to the new Pages URL and source repository. Keep `PAYLOAD`, `REPOSITORY`, manifest schema, canonical datadir layout, release asset construction, and deterministic output unchanged.
- [ ] Change `repository.bigping/addon.xml` `info`, `checksum`, `datadir`, `website`, and `source` URLs to `https://maratdob118.github.io/kodi-addons/` and `https://github.com/maratdob118/kodi-advanced-proxy` as appropriate. Preserve addon ids and `repository.bigping` display identity.
- [ ] Change `scripts/publish_repo.py` `DEFAULT_REPOSITORY`, token environment constant, bot name/email, and docstrings/comments to the new target. Keep `MANAGED`, idempotence, conflict refusal, retries, credential-helper redaction, and no-force-push behavior unchanged. Keep the commit message English PLAIN, with no conventional prefix.
- [ ] Retarget current test constants, fixtures, expected manifests, and workflow contract labels. Update only the expectations required by the new publication target and preserve tests for secret non-leakage without emitting synthetic token contents.
- [ ] Retarget the source workflow's labels and comments to `maratdob118/kodi-advanced-proxy`, `maratdob118/kodi-addons`, and `KODI_ADDONS_TOKEN`. Retarget the Pages bootstrap workflow and README comments to `kodi-addons`, while preserving the target workflow's own `GITHUB_TOKEN`, Pages permissions, concurrency, and text-only managed boundary.
- [ ] Run focused generator, publisher, workflow, site-builder, release, packaging, and metadata tests with `python3 -m unittest`, then run the complete repository test suite before any live publication step.

## Task 6: Build and release verification before GitHub operations

- [ ] Run the full suite with `python3 -m unittest discover -s tests -v` and require all 485+ tests, including the new lifecycle and retargeting tests, to pass.
- [ ] Run `bash scripts/check_versions.sh` against the worktree root and require addon, build, runtime, and stamp versions to agree.
- [ ] Run `python3 scripts/validate_addon.py` against the worktree root and require XML, extension, GPL metadata, license files, and string references to validate.
- [ ] Run the packaging tests with `python3 -m unittest tests.test_packaging -v` and the relevant repository/site/workflow tests. Keep staged tracked-binary deletions intact while tests use their existing fixtures or build inputs.
- [ ] Produce a fresh ARM64 artifact with `./build.sh linux_arm64`. Verify that platform ZIP with `scripts/verify_zip.sh` using the version resolved from `./build.sh --print-version`. If the release workflow requires the universal ZIP, assemble it through the existing `scripts/make_universal.py` path after the per-platform artifact checks and verify the universal ZIP with `scripts/verify_zip.sh --universal`.
- [ ] Run the packaging tests again against the fresh artifact and record the exact artifact path, version, SHA256, and size without committing the ZIP or engine binaries.

## Task 7: Live ARM64 Kodi QA

- [ ] Discover and use the existing approved deployment method for the ARM64 Raspberry Pi during execution. Do not invent a new transport or write to `/home/random/dev/bigping`; deploy only the built addon artifact to the existing Kodi target.
- [ ] Perform ten Kodi restart cycles. After each startup confirm exactly one Advanced Proxy engine PID and exactly one listener on `127.0.0.1:1080` when 1080 is free. If the preferred port is occupied, record the effective fallback port and confirm Kodi integration uses that same port for the session.
- [ ] After each normal shutdown confirm no engine process remains, no replacement process appears, no watchdog restart occurs after shutdown begins, and no shutdown-time “proxy stopped” error notification is emitted.
- [ ] Test Filmix playback through the installed addon and confirm requests use the current effective port rather than a stale fallback port.
- [ ] Test private direct routing through the local inbound and confirm `engine.log` shows the existing `ip_is_private` route to `direct`, not a remote profile.
- [ ] Test a private direct request separately from Filmix and confirm it remains direct while the proxy is active.
- [ ] Start the last YouTube video and keep playback running for more than 60 seconds. Confirm the engine is not restarted during playback, Kodi's proxy and the YouTube proxy source remain configured, and playback crosses the 60-second mark.
- [ ] Document the known stale YouTube session behavior after port drift. Do not add automatic YouTube restart or claim that the addon will repair an already-open stale session.

## Task 8: Publication runbook

- [ ] After the final implementation diff and verification results are inspected, perform a secret scan that reports only safe pass/fail information and file paths. Do not print secrets, token values, credential-helper output, or matching lines containing credentials.
- [ ] Run `gh auth status` without exposing credentials. Confirm the authenticated identity has the permissions needed for repository checks and publication, and record only the status summary.
- [ ] Check whether `maratdob118/kodi-advanced-proxy` and `maratdob118/kodi-addons` exist. If either is absent, create it as a public repository with the intended description and no private source content. Never inspect, create, modify, or push `maratdob118/bigping`.
- [ ] Push the source worktree to `maratdob118/kodi-advanced-proxy` using the approved non-force method. Preserve the existing branch's staged tracked-binary deletions and do not use a force push.
- [ ] Initialize the target Pages repository from `bootstrap/bigping.repository/` using the existing method, including `.github/workflows/pages.yml` and `scripts/build_site.py`, and push the bootstrap to `main`. Enable GitHub Pages with GitHub Actions as the source.
- [ ] Configure the fine-grained `KODI_ADDONS_TOKEN` secret in the source repository. Scope the token only to `maratdob118/kodi-addons` with Contents: write. Do not grant `workflows`, `pages`, or unrelated repository access. Do not print the token.
- [ ] Confirm the source workflow has default `contents: read`, that only the release job receives `contents: write`, and that only the publication job receives `KODI_ADDONS_TOKEN` through its environment. Confirm target Pages deployment uses only the target repository's `GITHUB_TOKEN` with Pages and OIDC permissions.
- [ ] Trigger the source release workflow for the verified addon version through the normal release path. Monitor the source test, version, metadata, per-platform build, universal package, release, generation, and publish jobs. Monitor the target Pages workflow separately after the generated tree push.
- [ ] Confirm the source GitHub Release contains the expected per-platform ZIPs, fresh universal ZIP, and `SHA256SUMS`, and that the generated target commit contains only the five managed generated paths plus preserved target files.
- [ ] Verify GitHub Pages serves the new `addons.xml`, `addons.xml.md5`, repository metadata, canonical `service.advancedproxy/service.advancedproxy-<version>.zip`, and lowercase `.sha256` sidecars over HTTPS. Verify the Kodi repository endpoint and addon metadata use `https://maratdob118.github.io/kodi-addons/`.
- [ ] Install or update `repository.bigping` from the new Kodi endpoint in a test Kodi instance and verify Advanced Proxy is discoverable and updateable through Kodi. Do not enable or install YouTube automatically.

## Task 9: Final diff inspection and Git planning, only after verification

- [ ] Inspect the final diff and status after all implementation, tests, packaging, QA, and publication preparation. Confirm only intended lifecycle, test, publication, metadata, workflow, and documentation files changed; preserve all staged tracked-binary deletions; remove no pre-existing user work.
- [ ] Confirm the final diff contains no private `maratdob118/bigping` publication target, no old `bigping.repository` Pages URL, no old token constant, no credential values, no force-push command, no automatic YouTube restart, no cross-instance lock, no periodic restart timer, and no Xray preservation claim.
- [ ] Plan English PLAIN commits only after this inspection. Split commits by independently revertible concern, keep each implementation change with its direct tests, and keep publication retargeting separate from lifecycle behavior where possible. Do not use conventional prefixes.
- [ ] Create commits only when explicitly requested by the supervising task. Use no force push. If publication requires a later push, use a normal fast-forward push and report any non-fast-forward condition instead of overwriting remote history.

## Verification checklist

- [ ] `python3 -m unittest discover -s tests -v` passes with the full 485+ suite.
- [ ] `bash scripts/check_versions.sh` passes.
- [ ] `python3 scripts/validate_addon.py` passes.
- [ ] Focused packaging tests pass.
- [ ] `./build.sh linux_arm64` produces a fresh ARM64 ZIP.
- [ ] `scripts/verify_zip.sh` validates the fresh ARM64 ZIP, and `--universal` validates the universal ZIP when assembled.
- [ ] Ten Kodi restarts show one engine PID/listener per run, no shutdown resurrection, Filmix works, private direct routing works, and YouTube playback passes 60 seconds.
- [ ] Stale YouTube sessions after port drift are documented, with no automatic addon restart added.
- [ ] Both public repositories, the source release, target Pages deployment, `KODI_ADDONS_TOKEN` scope, and Kodi endpoint are verified without printing secrets.
