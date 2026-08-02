# Advanced Proxy: Lifecycle Hardening and Two-Repository Publication

Date: 2026-08-02
Status: Approved

## Scope

This design hardens the engine lifecycle around a confirmed runtime race and settles the two-repository publication target. The decisions are already approved. This document specifies them; it does not implement them, does not modify existing source, tests, or plans, and does not create commits.

## Confirmed Runtime Race

During Kodi shutdown the engine exited cleanly before the service loop observed the abort flag. The watchdog treated the exit as a crash, armed a restart with the 2-second backoff, fired it, and the shutdown path then stopped the replacement. Two defects made this possible:

- `tick()` treats any observed exit as a crash and arms the watchdog with no awareness of shutdown.
- `main.py` enters its shutdown path only after the loop breaks, leaving the watchdog a multi-second window in which to fire.

Two further gaps were confirmed in the current code:

- `BinaryManager.stop()` sends SIGTERM, waits 5 seconds, escalates to SIGKILL only on exception, and drops the `proc` handle unconditionally. It never verifies the process exited, never waits after SIGKILL, and never verifies the listener was released.
- `main.py` applies the Kodi proxy integration immediately after `Popen` returns, without confirming the engine is listening on the effective port.

## Architecture

### Lifecycle states

The supervisor gains an explicit shutdown state. The five states:

| State | Meaning |
| --- | --- |
| stopped | No process, no pending restart. Initial state. |
| starting | Process spawned, readiness not yet confirmed. |
| running | Process alive and listener confirmed on the effective port. |
| restart_pending | Process exited unexpectedly, watchdog armed. |
| shutting_down | Abort observed or stop requested. Restarts are impossible. |

Transitions:

| From | To | Trigger |
| --- | --- | --- |
| stopped | starting | `start()` or a watchdog fire. |
| starting | running | Readiness confirmed on the effective port. |
| starting | stopped | Readiness timeout or failed config. |
| running | restart_pending | `tick()` observes an exit while abort is not requested. |
| restart_pending | running | Watchdog fires, abort not requested, start succeeds. |
| restart_pending | stopped | `begin_shutdown()` cancels the pending restart. |
| any | shutting_down | `begin_shutdown()`. |

`begin_shutdown()` sets `_shutting_down`, clears `_restart_at`, and is idempotent. Once set, `start()`, `restart()`, and the watchdog can never bring the engine up.

### Shutdown state is set before monitor exit

`main.py` restructures the loop so the shutdown state is entered before leaving the monitor:

1. When `monitor.waitForAbort(3)` returns True, call `sup.begin_shutdown()` before breaking.
2. Call `sup.begin_shutdown()` again after the loop (idempotent), then `integration.shutdown()`, then `sup.stop()`.

`stop()` also calls `begin_shutdown()` internally, so a bare `stop()` is always safe.

### Pending watchdog cancellation

`begin_shutdown()` clears `_restart_at`. A restart that was armed but has not fired is cancelled the moment shutdown is observed. No code path re-arms it afterwards.

### No restart after shutdown

When `_shutting_down` is set, `tick()` only clears an exited process handle and returns. It never notifies "proxy stopped", never increments the failure counter, and never arms or fires a restart.

### Abort-aware watchdog

The supervisor receives a `should_stop` callable at construction, wired to `monitor.abortRequested` by `main.py`. The watchdog consults it at both decision points:

- Exit detection: if `should_stop()` is True when an exit is observed, classify it as shutdown, not a crash. Call `begin_shutdown()` and do not arm the watchdog.
- Fire time: before starting a replacement, re-check `should_stop()`. If True, cancel the pending restart instead of firing it.

This closes the observed race. An exit observed while Kodi is aborting is never armed, and a restart armed moments before abort can only fire if the flag is still clear at fire time, in which case the loop observes abort within one wait period and the hardened stop tears any replacement down.

### Hardened stop

`BinaryManager.stop()` becomes:

```
stop(port=None, term_timeout=5.0, kill_timeout=5.0, release_timeout=5.0) -> bool
```

1. If `proc` is None, return True.
2. If running, send SIGTERM and `wait(term_timeout)`.
3. If still running, log the escalation and send SIGKILL, then `wait(kill_timeout)`.
4. Retain `self.proc` until `poll()` confirms exit, then set it to None. If the process is still alive after both waits, keep the handle, log, and return False. The caller proceeds with shutdown; the retained handle is evidence for diagnosis, not silently dropped.
5. When `port` is given and the process is confirmed dead, poll `port_utils.port_in_use(port)` in 100 ms steps until the listener is released or `release_timeout` elapses. A timeout is logged but is non-fatal: some other process may legitimately own the port.

`BinaryManager.start()` gains readiness:

```
start(config_path, port=None, ready_timeout=10.0)
```

After `Popen`, when `port` is given, poll `port_utils.port_in_use(port)` in 100 ms steps and require the process to stay alive. When the listener is confirmed up and the process is alive, return the process. When readiness is not confirmed within the bound, stop the spawned process with the hardened stop and raise.

`BinaryManager.restart()` delegates to the hardened stop and the readiness-checked start, forwarding the effective port.

`ProxySupervisor.start()` resolves the effective port exactly once, builds and validates the config, starts with the readiness bound, and returns True only when the engine is confirmed listening. Its return value now means "ready", not merely "spawned".

### One port-resolution pass per start and reconfigure

`_resolve_effective_port()` runs exactly once per public `start()` or `reconfigure_engine()` operation. Reconfiguration resolves the port itself and passes that resolved value into an internal start path that does not resolve it again. The chosen port is kept for the whole session so Kodi's system proxy stays stable. `tick()` never re-resolves the port and never rewrites the config. The existing `find_free_port` fallback remains the mechanism for external conflicts at start and reconfigure time.

### Kodi integration only after readiness

`main.py` already keys the sync call on the value returned by `sup.start()` and `sup.reconfigure_engine()`. Because that value now means readiness, `ensure_configured` can only run after the listener is verified up on `sup.effective_port`. A failed start or readiness timeout returns False, and the existing stale-backup restore path (`sync` with `running=False`) applies unchanged.

The existing restore-before-engine-stop ordering is preserved: `integration.shutdown()` always runs before `sup.stop()`.

### sing-box urltest keeps existing connections

In `build_singbox.py`, the urltest group (auto mode) is emitted with `interrupt_exist_connections: false` regardless of the `interrupt_connections` setting. Auto-switching between profiles must not drop live connections. The manual selector keeps honoring the user's `interrupt_connections` setting. Xray has no equivalent field, so this design makes no claim of connection preservation for Xray.

### Cross-instance locking is deferred

No PID file, no lock file, and no ownership marker on port 1080 in this phase. Two Kodi instances, or an external process holding the port, are handled only by the existing one-shot `find_free_port` fallback at start and reconfigure. Cross-instance locking and ownership claims are explicitly deferred to a later phase.

### No periodic restart timer

The watchdog is purely exit-driven with the existing exponential backoff (2, 4, 8, ..., capped at 60 seconds, give up after 11 consecutive failures). The removed 180-second timer from the stale local addon copy is not reintroduced. Elapsed time alone never tears down or restarts a healthy engine, and the existing regression tests for this stay in force.

## Failure Behavior

| Failure | Behavior |
| --- | --- |
| Engine exits while running, abort not requested | Watchdog arms with backoff and restarts. Preserved. |
| Engine exits while abort requested or `_shutting_down` | Logged, not restarted, no error notification. |
| Pending restart when shutdown begins | Cancelled by `begin_shutdown()`. |
| SIGTERM ignored | SIGKILL after `term_timeout`, then wait. |
| Process refuses to die | Handle retained, `stop()` returns False, shutdown proceeds. |
| Listener not released after death | Logged after `release_timeout`; non-fatal. |
| Startup readiness timeout | Spawned process stopped, start returns False, stale backup restored. |
| External process owns the port | Existing `find_free_port` fallback at start/reconfigure. |
| Integration failure | Non-fatal to the engine, as today. |

## Testing

All new tests are written RED-first: each test is added and observed failing before the implementation change that makes it pass. The existing suite baseline is 485 passing tests; the full suite must remain green.

New test groups:

- `TestBinaryManagerStopEscalation`: terminate-then-exit clears the handle only after confirmed exit; terminate-timeout escalates to kill; kill-timeout retains the handle and returns False; the order is always terminate before kill; the port-release wait polls until the listener frees; a busy listener after death logs and still returns True.
- `TestStartupReadiness`: a listener confirmed within the bound returns the process; a readiness timeout stops the spawned process and raises; the process exiting during the readiness wait is a failed start.
- `TestSupervisorShutdownRace`: a clean exit after `begin_shutdown()` does not arm the watchdog, does not restart, and does not notify; `begin_shutdown()` clears a pending `_restart_at`; an exit observed while `should_stop()` is True is never armed; a restart about to fire is cancelled when `should_stop()` turns True.
- `TestSupervisorStopDuringShutdown`: `stop()` cancels the watchdog, stops the process with the hardened path, and writes `state.json` with `running: false`.
- `TestMainLifecycleWiring`: the shutdown state is set before the monitor exits; `ensure_configured` appears only after a readiness-confirmed start; a readiness failure restores the stale backup instead of configuring; the restore-before-stop ordering assertion stays.
- `TestOnePortResolutionPass`: `start()` and `reconfigure_engine()` each resolve the port exactly once; `tick()` never resolves or rewrites the config.
- `TestBuildSingboxInterruptConnections`: urltest emits `interrupt_exist_connections: false` for both `interrupt_connections` values; manual mode keeps the setting.
- Existing watchdog and timer tests stay: healthy engines are never restarted by elapsed time, backoff grows exponentially, recovery resets the counter, and give-up after 11 failures.

## Live Validation on the ARM64 Raspberry Pi

Run on the ARM64 Pi running Kodi (LibreELEC), with the addon installed and sing-box selected:

1. Repeated Kodi restarts, at least ten: after each restart exactly one engine PID exists and exactly one listener is bound to `127.0.0.1:1080` (the effective port when 1080 is free). After each shutdown no engine process remains and the log shows no restart after shutdown began.
2. Filmix: open the installed Filmix addon and play a stream while Kodi is configured to use Advanced Proxy. Confirm the request uses the current effective port and does not retain a stale fallback port.
3. Private direct routing: while the proxy is active, send private and LAN traffic through the local inbound and confirm `engine.log` routes it through the `ip_is_private` to `direct` outbound rather than a remote proxy profile.
4. YouTube: start playback and keep it running for more than 60 seconds. Confirm playback is uninterrupted across the 60-second mark, the engine is not restarted during playback, and Kodi's proxy plus the YouTube proxy source remain configured.
5. Graceful shutdown: stop Kodi normally and confirm no zombie engine, no replacement process, and no "proxy stopped" error notification during shutdown.

## Two-Repository Publication

Publication moves to two new public repositories. The private `maratdob118/bigping` is not a target: no workflow, script, or publish step in this design references, clones, or writes to it, and anything that previously targeted `bigping.repository` is retargeted.

| Repository | Role | Git contents |
| --- | --- | --- |
| `maratdob118/kodi-advanced-proxy` | Source monorepo | Addon source, build and test tooling, CI workflows, releases. |
| `maratdob118/kodi-addons` | Generated Kodi repository | Text only: `addons.xml`, `addons.xml.md5`, `manifest.json`, `repository.bigping` metadata, README. Serves the payload from GitHub Pages. |

Both repositories are public. Addon ids are unchanged: `service.advancedproxy` for the service and `repository.bigping` (display name BigPing) for the bootstrap repository addon.

Carried over from the approved 2026-08-01 design without change:

- One release per new addon version on the source repo, carrying eight per-platform ZIPs, the universal ZIP, and `SHA256SUMS`.
- The universal ZIP (~235 MB) is the only payload the Kodi repository serves, at the canonical path `<datadir>/service.advancedproxy/service.advancedproxy-<version>.zip`, because the repository protocol offers no per-platform selection.
- The target stays text-only in Git. The universal ZIP reaches users as a source release asset and then a Pages deployment artifact, never as a Git blob and never via `raw.githubusercontent.com`.
- `addons.xml` and `addons.xml.md5` are regenerated, never hand-edited. Every published ZIP gets a `<zip>.sha256` sidecar with its lowercase hex digest.
- `repository.bigping` metadata uses the Kodi 20+ `<dir>` form with HTTPS URLs only.
- The publisher manages only the generated set: `addons.xml`, `addons.xml.md5`, `manifest.json`, `README.md`, and `repository.bigping/addon.xml`. Everything else in the target, including its `.github` tree, is preserved untouched.
- Publishing is keyed by addon version and idempotent. An existing tag with identical content is a skip; an existing tag with different content aborts. No force push, no orphan tags.
- The target's own Pages workflow reads `manifest.json`, downloads the universal ZIP with a retrying fail-loud fetch, refuses to build unless the bytes match the recorded SHA256 and size, packs `repository.bigping` itself, lays out the canonical paths with `.sha256` sidecars, and deploys with the target's own `GITHUB_TOKEN`.

Adapted for the new repositories:

- Pages base URL: `https://maratdob118.github.io/kodi-addons/`.
- Cross-repo credential: a fine-grained PAT scoped to `maratdob118/kodi-addons` with Contents: write and nothing else. It is stored in the source repo as secret `KODI_ADDONS_TOKEN` and consumed only through the environment. It is never granted `workflows` or `pages` permissions.
- Commit identity for target commits: `kodi-addons-release-bot` with a noreply email.
- Source CI, release, and publish workflows move to `maratdob118/kodi-advanced-proxy` with default permissions `contents: read`; only the release job gets `contents: write`, and only the publish job gets the `KODI_ADDONS_TOKEN` secret.

## Non-goals

- Cross-instance locking and port ownership claims (deferred to a later phase).
- Enabling, installing, or updating YouTube automatically.
- Continuously overriding user changes.
- Any claim of connection preservation for Xray.
- Reintroducing a periodic restart timer.
