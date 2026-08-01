# Advanced Proxy: Kodi Integration, Licensing, CI, and Releases

Date: 2026-08-01
Status: Proposed

## Goals

1. Keep Kodi's system proxy synchronized with the local proxy actually started by Advanced Proxy, including dynamic port fallback.
2. Configure supported addons, initially `plugin.video.youtube`, to consume Kodi's system proxy.
3. Restore the user's previous proxy settings when Advanced Proxy stops cleanly or is disabled.
4. License the addon source under `GPL-3.0-or-later` and preserve all licenses required by bundled engines.
5. Publish a public GitHub repository with automated tests, per-platform ZIP builds, and one release per new addon version.

## Non-goals

- Enabling or installing YouTube automatically.
- Reconfiguring arbitrary third-party addons without an explicit integration definition.
- Continuously overriding user changes every few seconds.
- Committing engine binaries to Git.
- Modifying sing-box or Xray binaries.

## Runtime Integration Architecture

### Components

Add a Kodi-free core module, `src/proxy_integration.py`, responsible for comparison, backup, apply, and restore decisions. Kodi-specific reads and writes remain in `src/helpers.py` and are injected into the core module.

`main.py` owns lifecycle integration:

1. Construct the supervisor and integration manager.
2. Start the selected engine.
3. Only after a successful engine start, call `ensure_configured()` with `127.0.0.1` and `sup.effective_port`.
4. Call `ensure_configured()` again after engine or port reconfiguration.
5. Before a graceful engine shutdown, call `restore_previous()`.
6. If the engine cannot start, no profiles exist, or autostart is disabled, restore a stale backup left by an earlier unclean shutdown.

### Kodi System Proxy

Read and write Kodi core settings using the in-process JSON-RPC transport through `xbmc.executeJSONRPC`:

- `network.usehttpproxy = true`
- `network.httpproxytype = 0` (HTTP; compatible with the mixed sing-box inbound)
- `network.httpproxyserver = 127.0.0.1`
- `network.httpproxyport = effective_port`

Username and password settings are not modified.

Writes are compare-and-set: only mismatched values are written. Detailed changes go to `kodi.log`; one consolidated notification reports corrections.

### YouTube Integration

Use `xbmcaddon.Addon("plugin.video.youtube")` typed settings APIs:

- Expected setting: `requests.proxy.source = 1` (Kodi proxy).

If YouTube is absent, disabled, or lacks the setting, log a warning and continue. Advanced Proxy does not install or enable YouTube.

The integration registry is data-driven so additional addons can be added later without changing lifecycle code.

## Backup and Restore

Persist `integration_backup.json` in the addon profile directory before the first mutation. It contains:

- Previous values of the four Kodi system proxy settings.
- Previous YouTube proxy source.
- Values applied by Advanced Proxy, including the effective port.
- Schema version.

An existing backup is never overwritten during a later startup. This preserves the original settings across crashes or forced Kodi termination.

On graceful stop:

1. Read current settings.
2. Restore only when current values still match those applied by Advanced Proxy.
3. If the user changed any owned value, skip that component's restore and log the reason.
4. Delete the backup after all eligible values are restored or explicitly skipped due to external modification.

If startup finds a backup but no proxy will run, restore it immediately to avoid leaving Kodi pointed at a dead local port.

## User Setting

Add `auto_configure_integration`, default `true`:

> Automatically configure Kodi and supported addons

Turning it off while the service is running restores eligible previous settings and disables further mutations. Validation results continue to be logged.

## Error Handling

- Malformed JSON-RPC responses: log and notify once; keep the proxy process running.
- Unsupported/missing Kodi setting: fail only that setting; do not roll back successful independent checks.
- Missing or disabled YouTube addon: log informational warning; no popup loop.
- Failure to persist backup: do not mutate external settings.
- Failure during partial apply: restore already changed values from the in-memory backup when possible.
- All integration failures remain non-fatal to the proxy engine.

## Licensing

The addon source is licensed under `GPL-3.0-or-later`:

- Add the complete GPL-3.0 text as `LICENSE`.
- Change `addon.xml` metadata from MIT to `GPL-3.0-or-later`.
- Document licensing in README.

Add `THIRD_PARTY_NOTICES.md` and preserve upstream license files in release ZIPs:

- sing-box v1.13.14: GPL-3.0-or-later, including its name/association restriction and bundled JA3 BSD-3-Clause notice.
- Xray-core v25.8.3: MPL-2.0, distributed unmodified as a separate executable.
- Include pinned source links for both versions.

The build must copy each engine's upstream license/notice files beside its binary. Release ZIPs must contain those notices even though binaries are downloaded during CI.

## Repository and Binary Policy

Create public repository `github.com/maratdob118/kodi-advanced-proxy`.

- Ignore `service.advancedproxy/resources/bin/` in Git.
- Remove currently tracked engine binaries from the Git index, without rewriting existing history.
- Release ZIPs still contain a platform-specific bundled engine directory and are self-contained/offline-installable.
- `addon.xml` is the source of truth for the addon version.
- `build.sh` derives its default addon version from `addon.xml` instead of duplicating it.
- Engine versions remain pinned; CI asserts build-time and runtime constants agree.

## GitHub Actions

Add one workflow triggered by:

- Every push to `master`.
- Every pull request targeting `master`.
- Manual `workflow_dispatch`.

Default permissions are `contents: read`. Only the release job receives `contents: write`.

### Test Job

- Ubuntu runner with Python.
- `python3 -m unittest tests.test_core`.
- Kodi addon metadata validation.
- Version consistency checks.
- License and third-party notice presence checks.

### Build Matrix

Build eight targets on Ubuntu because the workflow downloads prebuilt engines rather than cross-compiling:

- `linux_x64`
- `linux_x86`
- `linux_armv7`
- `linux_arm64`
- `android_arm64`
- `windows_x64`
- `darwin_x64`
- `darwin_arm64`

Each job runs `build.sh <platform>`, verifies ZIP structure and version stamps, then uploads one workflow artifact. Missing artifacts fail the job.

### Version-driven Release

On a push to `master`, after tests and all builds pass:

1. Parse version `X.Y.Z` from `addon.xml`.
2. Check whether tag/release `vX.Y.Z` already exists.
3. If it exists, skip release creation while retaining CI artifacts.
4. If it does not exist, aggregate all platform ZIPs.
5. Create draft release `vX.Y.Z` targeting the current commit.
6. Upload all ZIPs and generated checksums.
7. Publish the release only after every upload succeeds.

Use repository-scoped `GITHUB_TOKEN`; no PAT or external secret is required. Concurrency is keyed by addon version to prevent duplicate releases from simultaneous pushes.

## Testing

### Integration unit tests

- Matching settings produce no writes.
- Kodi mismatch is corrected to the actual effective port.
- Dynamic fallback port is propagated.
- YouTube source `0` or `2` becomes `1`.
- Missing/disabled YouTube is tolerated.
- Backup is created before writes and not overwritten after a crash.
- Graceful stop restores previous values.
- User-modified settings are not overwritten during restore.
- Stale backup is restored when no engine starts.
- Failed backup persistence prevents mutation.

### Workflow tests

- Shell/version validation runs locally.
- Every matrix target produces exactly one correctly named ZIP.
- ZIP contains only its target platform binaries plus required licenses.
- Existing version tag causes release job to skip safely.

### Manual acceptance on LibreELEC

1. Start with Kodi proxy disabled and YouTube proxy source `0`.
2. Start Advanced Proxy and confirm Kodi points to the actual effective port and YouTube source becomes `1`.
3. Confirm YouTube requests appear in `engine.log`.
4. Occupy port 1080; restart and confirm Kodi follows the fallback port.
5. Stop/disable Advanced Proxy and confirm previous Kodi/YouTube settings are restored.

## Acceptance Criteria

- A successful engine startup leaves Kodi and supported addons correctly configured without manual steps.
- A failed or disabled engine does not leave Kodi pointed at a dead local proxy.
- User changes made after startup are not overwritten on shutdown.
- All tests pass and every supported platform ZIP is self-contained.
- A new `addon.xml` version pushed to `master` creates exactly one GitHub release with all ZIPs and checksums.
- Repository license metadata is GPL-3.0-or-later and all bundled third-party notices are present.
