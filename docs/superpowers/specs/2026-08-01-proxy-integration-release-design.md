# Advanced Proxy: Kodi Integration, Licensing, CI, and Releases

Date: 2026-08-01
Status: Proposed

## Goals

1. Keep Kodi's system proxy synchronized with the local proxy actually started by Advanced Proxy, including dynamic port fallback.
2. Configure supported addons, initially `plugin.video.youtube`, to consume Kodi's system proxy.
3. Restore the user's previous proxy settings when Advanced Proxy stops cleanly or is disabled.
4. License the addon source under `GPL-3.0-or-later` and preserve all licenses required by bundled engines.
5. Publish a public source monorepo with automated tests, per-platform ZIP builds, and one release per new addon version.
6. Publish a separate, generated Kodi repository so users install and auto-update Advanced Proxy from inside Kodi.

## Non-goals

- Enabling or installing YouTube automatically.
- Reconfiguring arbitrary third-party addons without an explicit integration definition.
- Continuously overriding user changes every few seconds.
- Committing engine binaries or addon ZIPs to Git.
- Modifying sing-box or Xray binaries.
- Per-platform variant selection inside the Kodi repository. The repository protocol has no OS/arch negotiation, so this is not implementable, not merely undesirable.
- Serving the universal ZIP from `raw.githubusercontent.com`. It serves Git blobs, which the artifact is too large to be.
- Using a classic PAT for cross-repository publishing.

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

## Two-Repository Architecture

Distribution uses two public repositories with distinct roles.

| Repository | Role | Git contents |
| --- | --- | --- |
| `maratdob118/bigping` | Source monorepo | Addon source, build/test tooling, CI workflows. |
| `maratdob118/bigping.repository` | Generated Kodi repository | Text only: `addons.xml`, `addons.xml.md5`, `repository.bigping` metadata, README. |

Addon ids:

- `service.advancedproxy` — the proxy service addon. Unchanged.
- `repository.bigping` — the Kodi repository (bootstrap) addon. New, and separate.

### Source repository policy

- Ignore `service.advancedproxy/resources/bin/` in Git.
- Remove currently tracked engine binaries from the Git index, without rewriting existing history.
- Per-platform release ZIPs still contain a platform-specific bundled engine directory and are self-contained/offline-installable.
- `addon.xml` is the source of truth for the addon version.
- `build.sh` derives its default addon version from `addon.xml` instead of duplicating it.
- Engine versions remain pinned; CI asserts build-time and runtime constants agree.

### Distribution artifacts

Two artifact shapes are produced per addon version:

1. **Eight per-platform ZIPs**, `service.advancedproxy-<version>.<platform>.zip`, one each for `linux_x64`, `linux_x86`, `linux_armv7`, `linux_arm64`, `android_arm64`, `windows_x64`, `darwin_x64`, `darwin_arm64`. Each carries exactly one platform's binaries. These remain attached to the source repo's GitHub Release for manual, offline "Install from zip" installation.
2. **One universal ZIP**, `service.advancedproxy-<version>.zip`, carrying all eight platform binary directories (~235 MB). This is the only artifact the Kodi repository serves.

### Why a universal ZIP is required

Kodi's repository protocol exposes exactly one canonical path per addon version:

```
<datadir>/<addon.id>/<addon.id>-<version>.zip
```

`addons.xml` declares a single `<addon id="service.advancedproxy" version="X.Y.Z">` entry, and Kodi resolves that entry to that one path. The protocol carries no OS or architecture information, offers no variant list, and provides no fallback path. `<platform>` in addon metadata is descriptive, not a selector.

Therefore the ZIP behind the canonical path must run on every supported platform. The universal ZIP satisfies this; `osarch.py` selects the correct binary directory at runtime. Per-platform ZIPs are retained only for the manual install path.

### Why GitHub Pages hosts the payload

The universal ZIP is roughly 235 MB. GitHub rejects any single file larger than 100 MB pushed to a Git repository, so the universal ZIP cannot exist as a Git blob in `bigping.repository`. `raw.githubusercontent.com` serves Git blobs and is therefore equally unavailable, besides being rate-limited and unsupported for this purpose.

The design consequences:

- `bigping.repository` stays text-only in Git. No large object is ever committed, so the repository stays clonable and its history stays small.
- The payload is published as a GitHub Pages deployment artifact. A Pages artifact is not a Git commit and is not subject to the blob limit; its size ceiling is far above the payload.
- Pages serves over HTTPS, which Kodi 20+ expects for a repository `datadir`.
- A deployment replaces the whole site, so it carries only the current version: one ~235 MB payload, far below the 1 GB artifact ceiling. There is nothing to prune, because nothing older is uploaded. Historic assets are retained by the source repository's releases, which is where a rollback or an offline install is served from.
- Every published ZIP is accompanied by a `<zip>.sha256` sidecar holding its lowercase hex digest. Kodi resolves a ZIP's digest from a `content-sha256` response header first and falls back to that sidecar. Pages cannot set response headers, so the sidecar is the only mechanism available and is mandatory.

Served layout:

```
https://maratdob118.github.io/bigping.repository/
├── addons.xml
├── addons.xml.md5
├── repository.bigping/
│   ├── repository.bigping-<version>.zip
│   └── repository.bigping-<version>.zip.sha256
└── service.advancedproxy/
    ├── service.advancedproxy-<version>.zip
    ├── service.advancedproxy-<version>.zip.sha256
    └── resources/          # icon/fanart, extracted from the payload ZIP
```

`addons.xml` is the concatenation of every offered addon's `<addon>` element inside a single `<addons>` root; `addons.xml.md5` holds its MD5 digest and is what Kodi polls to detect updates. Both are regenerated, never hand-edited.

### Repository addon metadata

`repository.bigping` must use the Kodi 20+ (Nexus and later) form, in which `<info>`, `<checksum>` and `<datadir>` are wrapped in a `<dir>` element instead of sitting directly under the extension point:

```xml
<extension point="xbmc.addon.repository" name="BigPing">
  <dir minversion="20.0.0">
    <info compressed="false">https://maratdob118.github.io/bigping.repository/addons.xml</info>
    <checksum verify="md5">https://maratdob118.github.io/bigping.repository/addons.xml.md5</checksum>
    <datadir zip="true">https://maratdob118.github.io/bigping.repository/</datadir>
  </dir>
</extension>
```

All three URLs are HTTPS. Plain HTTP is not acceptable to current Kodi versions and is not offered by Pages.

### Cross-repository credential

The source repo's `GITHUB_TOKEN` cannot write to another repository, so publishing to the target requires an explicit credential:

- A **fine-grained** personal access token, stored in the source repo as secret `BIGPING_REPOSITORY_TOKEN`.
- Resource scope: only `maratdob118/bigping.repository`.
- Permissions: **Contents: write**, and nothing else. It is not granted `pages: write`; the target repo's own workflow deploys Pages with its own `GITHUB_TOKEN`.
- Classic PATs are not used; their scopes cannot be limited to a single repository.
- Fine-grained tokens expire. The expiry date is recorded with the secret, and rotation is a scheduled maintenance step — an expired token makes publishing fail while the source release still succeeds.

## GitHub Actions

Add one workflow in the source repo, triggered by:

- Every push to `main`.
- Every pull request targeting `main`.
- Manual `workflow_dispatch`.

Default permissions are `contents: read`. Only the release job receives `contents: write`. Only the publish job receives the `BIGPING_REPOSITORY_TOKEN` secret.

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

### Universal ZIP Assembly

After the matrix completes, one job aggregates all eight platform artifacts into the single universal ZIP `service.advancedproxy-<version>.zip`, whose `service.advancedproxy/resources/bin/` contains all eight platform directories. It verifies that every expected platform directory is present and correctly version-stamped, and that the addon's Python tree is byte-identical to the per-platform ZIPs. A missing platform fails the job rather than shipping a ZIP that is broken on that platform.

### Source Release Flow

On a push to `main`, after tests and all builds pass:

1. Parse version `X.Y.Z` from `addon.xml`.
2. Check whether tag/release `vX.Y.Z` already exists.
3. If it exists, skip release creation while retaining CI artifacts.
4. If it does not exist, aggregate the eight platform ZIPs plus the universal ZIP.
5. Create draft release `vX.Y.Z` targeting the current commit.
6. Upload all nine ZIPs and generated checksums.
7. Publish the release only after every upload succeeds.

This job uses the repository-scoped `GITHUB_TOKEN`. Release assets are not Git blobs, so the universal ZIP is acceptable here; the 100 MB blob limit does not apply to release assets.

### Target Pages Flow

After the source release is published, a publish job updates the Kodi repository:

1. Regenerate `addons.xml` for the current version, recompute `addons.xml.md5`, and emit `manifest.json`: the download/publish plan naming the release asset, the SHA256 and size measured on the artifact that was just uploaded, the canonical publish paths, and the art the payload carries.
2. Commit the regenerated text files to `maratdob118/bigping.repository` on `main`, authenticating with `BIGPING_REPOSITORY_TOKEN`. Nothing binary is committed.
3. The target repo's own workflow reacts to that commit: it reads the URL and digest from `manifest.json`, downloads the universal ZIP with a retrying, fail-loud `curl`, refuses to continue unless the bytes hash to the recorded SHA256 and size, then packs the `repository.bigping` ZIP itself, lays everything out under the canonical `<addon.id>/<addon.id>-<version>.zip` paths with `.sha256` sidecars, extracts the payload's art, and deploys the result as a Pages artifact using the target repo's `GITHUB_TOKEN` with `pages: write` and `id-token: write`.

That workflow and its site builder are bootstrapped into the target by hand, once, from the `bootstrap/bigping.repository/` template in the source repo. They are deliberately excluded from the set the publisher manages: writing `.github/workflows/` would require the `workflows` permission on the token, and keeping them out of it is what holds the fine-grained PAT to `Contents: write`. Every later release pushes generated metadata only, and every file the target carries outside that set — including its `.github` tree — is preserved untouched.

The universal ZIP therefore travels from the source release to the Pages deployment over HTTPS, never through a Git object.

### Idempotency and Concurrency

Both flows are keyed by addon version:

- A re-run for an already-released version skips release creation.
- The regenerated `addons.xml`/`addons.xml.md5` are deterministic, so an unchanged version produces a no-op commit rather than an empty-but-new commit.
- A repeated Pages deployment for the same version publishes identical content.

Concurrency is non-cancelling, so simultaneous pushes queue instead of racing and cannot produce duplicate releases or interleaved repository state. Test, build and release are keyed by ref. The publish job is keyed by the target repository alone and not by ref: the target is one shared resource, so two different refs publishing at the same time would race no matter which refs they are.

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
- Each per-platform ZIP contains only its target platform binaries plus required licenses.
- The universal ZIP contains all eight platform directories, each version-stamped, plus required licenses.
- Existing version tag causes the release job to skip safely.

### Repository generation tests

- `addons.xml` parses, has a single `<addons>` root, and contains one `<addon>` element per offered addon version.
- `addons.xml.md5` matches the digest of the generated `addons.xml`.
- The deployment carries the current version only, and each published ZIP has a `.sha256` sidecar holding its lowercase hex digest.
- A payload whose bytes do not match the digest recorded in `manifest.json` is refused and no site is deployed.
- Generated ZIP paths match `<addon.id>/<addon.id>-<version>.zip` exactly.
- `repository.bigping` metadata uses the Kodi 20+ `<dir>` form and HTTPS URLs only.
- Regenerating from unchanged input produces byte-identical output (no-op commit).

### Manual acceptance on LibreELEC

Runtime integration:

1. Start with Kodi proxy disabled and YouTube proxy source `0`.
2. Start Advanced Proxy and confirm Kodi points to the actual effective port and YouTube source becomes `1`.
3. Confirm YouTube requests appear in `engine.log`.
4. Occupy port 1080; restart and confirm Kodi follows the fallback port.
5. Stop/disable Advanced Proxy and confirm previous Kodi/YouTube settings are restored.

Live repository install, against the deployed Pages site rather than a local file:

6. Install `repository.bigping-<version>.zip` via "Install from zip file" on a clean profile.
7. Browse Add-ons → Install from repository → BigPing → Services and confirm Advanced Proxy is listed with the expected version.
8. Install Advanced Proxy from the repository and confirm the download comes from the Pages `datadir`, that the universal ZIP installs on armv7, and that the correct binary directory is selected at runtime.
9. Publish a newer version, then confirm Kodi detects it after refreshing the repository and offers/performs the update.
10. Confirm no Kodi log errors about checksum mismatch, unreachable `datadir`, or an unsupported repository structure.

## Acceptance Criteria

- A successful engine startup leaves Kodi and supported addons correctly configured without manual steps.
- A failed or disabled engine does not leave Kodi pointed at a dead local proxy.
- User changes made after startup are not overwritten on shutdown.
- All tests pass, every per-platform ZIP is self-contained, and the universal ZIP works on every supported platform.
- A new `addon.xml` version pushed to `main` creates exactly one GitHub release carrying the eight platform ZIPs, the universal ZIP, and checksums.
- The same push updates `bigping.repository` with regenerated text only, and the Pages deployment serves the universal ZIP at the canonical Kodi path over HTTPS.
- No file over 100 MB is ever committed to either repository.
- A user who installs `repository.bigping` once receives Advanced Proxy and its later updates from inside Kodi.
- Re-running any flow for an already-published version changes nothing.
- Repository license metadata is GPL-3.0-or-later and all bundled third-party notices are present in both ZIP shapes.
