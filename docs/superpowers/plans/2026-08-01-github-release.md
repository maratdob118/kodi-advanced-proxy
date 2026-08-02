# GitHub CI, Release, and Kodi Repository Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Test and build all supported ZIPs on every push/PR, publish exactly one GitHub Release per new addon version from the source monorepo, and publish a generated Kodi repository on GitHub Pages so users install and auto-update from inside Kodi.

**Architecture:** Two repositories. `maratdob118/bigping` holds source and CI; it releases eight per-platform ZIPs plus one universal ZIP. `maratdob118/bigping.repository` holds only generated text in Git and serves the binary payload from a GitHub Pages deployment.

**Tech Stack:** GitHub Actions, GitHub Pages, Python unittest, Bash, GitHub CLI.

## Global Constraints

- Source monorepo: `maratdob118/bigping`. Target Kodi repository: `maratdob118/bigping.repository`. Both public.
- Addon ids: `service.advancedproxy` (unchanged) and `repository.bigping` (new bootstrap addon).
- `addon.xml` is the addon-version source of truth.
- Tests/builds run on push to `main`, PR to `main`, and workflow_dispatch.
- Release only when tag/release `vX.Y.Z` does not exist.
- Only the release job gets `contents: write`; only the publish job gets `BIGPING_REPOSITORY_TOKEN`.
- Build matrix: linux_x64, linux_x86, linux_armv7, linux_arm64, android_arm64, windows_x64, darwin_x64, darwin_arm64.
- The Kodi repository serves exactly one universal ZIP per version at `<datadir>/<addon.id>/<addon.id>-<version>.zip`. No per-platform selection exists in the repository protocol.
- Nothing over 100 MB is ever committed to Git in either repository. The ~235 MB universal ZIP moves from release asset to Pages artifact over HTTPS, never as a Git blob, and never via `raw.githubusercontent.com`.
- Retention: the Pages deployment carries the current version only (one ~235 MB payload, far below the 1 GB artifact ceiling); the source repo's releases retain every historic asset. Every published ZIP gets a `<zip>.sha256` sidecar, since Pages cannot set the `content-sha256` header Kodi prefers.
- Cross-repo auth is a fine-grained PAT scoped to the target repo with `Contents: write` only. Not a classic PAT.

---

### Task 1: Add validation scripts

**Files:**
- Create: `scripts/check_versions.sh`
- Create: `scripts/validate_addon.py`
- Create: `tests/test_release_logic.py`

**Interfaces:**
- `scripts/check_versions.sh [repo-root]`
- `python3 scripts/validate_addon.py [repo-root]`

- [ ] Add failing tests for addon/build/runtime version drift and missing license metadata.
- [ ] Implement version extraction and consistency checks.
- [ ] Validate addon XML, extensions, GPL metadata, notices, and language IDs.
- [ ] Run all tests and scripts locally.

---

### Task 2: Add universal ZIP assembly

**Files:**
- Modify: `build.sh`
- Create: `scripts/build_universal.sh`
- Modify: `scripts/verify_zip.sh`
- Test: `tests/test_packaging.py`

**Interfaces:**

```bash
scripts/build_universal.sh --version X.Y.Z --dist-dir dist
scripts/verify_zip.sh ZIP universal VERSION
```

- [ ] Write failing tests for a universal ZIP missing a platform directory and for a mixed-version stamp.
- [ ] Assemble `dist/service.advancedproxy-<version>.zip` containing all eight platform directories under `service.advancedproxy/resources/bin/`.
- [ ] Assert the Python tree is byte-identical to the per-platform ZIPs and that licenses/notices are present.
- [ ] Extend `verify_zip.sh` with a `universal` mode asserting all eight platforms are present and version-stamped.
- [ ] Fail loudly on a missing platform rather than shipping a ZIP broken on that platform.

---

### Task 3: Add release planner

**Files:**
- Create: `scripts/release.py`
- Test: `tests/test_release_logic.py`

**Interfaces:**

```bash
python3 scripts/release.py --version X.Y.Z --assets-dir DIR --sha SHA
python3 scripts/release.py --version X.Y.Z --assets-dir DIR --dry-run
```

- [ ] Write failing pure tests for create-vs-skip and asset validation, expecting nine ZIPs (eight platform + one universal).
- [ ] Implement SHA256SUMS generation.
- [ ] Skip cleanly when `gh release view vX.Y.Z` succeeds.
- [ ] Otherwise create draft, upload all ZIPs/checksum, then publish.
- [ ] Abort without publication when any asset/upload fails.

---

### Task 4: Add Kodi repository generator

**Files:**
- Create: `scripts/build_repository.py`
- Create: `repository.bigping/addon.xml`
- Test: `tests/test_repository_generation.py`

**Interfaces:**

```bash
python3 scripts/build_repository.py --out DIR --keep 2
```

- [ ] Write failing tests for `addons.xml` structure, md5 agreement, retention, canonical ZIP paths, and deterministic output.
- [ ] Author `repository.bigping/addon.xml` using the Kodi 20+ `<dir minversion="20.0.0">` form wrapping `<info>`, `<checksum verify="md5">`, and `<datadir zip="true">`, all HTTPS, pointing at `https://maratdob118.github.io/bigping.repository/`.
- [ ] Generate `addons.xml` with a single `<addons>` root containing one `<addon>` element per offered addon version.
- [ ] Write `addons.xml.md5` as the digest of the generated `addons.xml`.
- [ ] Emit canonical paths `<addon.id>/<addon.id>-<version>.zip` only.
- [ ] Apply latest-two-version retention and confirm regeneration from unchanged input is byte-identical.

---

### Task 5: Add source repository workflow

**Files:**
- Modify: `.github/workflows/release.yml`

- [ ] Retarget triggers from `master` to push/PR on `main`, plus workflow_dispatch.
- [ ] Set default `permissions: contents: read`.
- [ ] Add test job: unittest + version/license/addon checks.
- [ ] Add 8-platform Ubuntu matrix with `build.sh`, `verify_zip.sh`, and artifact upload.
- [ ] Add universal assembly job consuming all eight artifacts and verifying the result.
- [ ] Add main-only release job with `contents: write` publishing nine ZIPs plus SHA256SUMS via `release.py`.
- [ ] Add publish job that regenerates repository text and commits it to `maratdob118/bigping.repository` using `BIGPING_REPOSITORY_TOKEN`, committing nothing binary.
- [ ] Add non-cancelling concurrency keyed by addon version so pushes queue instead of racing.
- [ ] Verify idempotency: a re-run for an existing version skips the release and produces a no-op commit.
- [ ] Validate YAML and inspect job permissions.

---

### Task 6: Add target repository and Pages workflow

**Files:**
- Create: `bootstrap/bigping.repository/.github/workflows/pages.yml`
- Create: `bootstrap/bigping.repository/scripts/build_site.py`

The template is bootstrapped into `bigping.repository` by hand, once. It stays outside the set `publish_repo.py` manages, so the fine-grained PAT needs `Contents: write` and never the `workflows` permission; the target's own `.github` tree is preserved by every later publish. The target's `README.md` is generated, not bootstrapped.

- [ ] Trigger on push to `main` and workflow_dispatch.
- [ ] Grant `pages: write` and `id-token: write` to the deploy job; the target repo uses its own `GITHUB_TOKEN` and never needs the PAT.
- [ ] Read the release URL and digest from `manifest.json`, download the universal ZIP over HTTPS with a retrying, fail-loud `curl`, and refuse to build unless the bytes match.
- [ ] Pack the `repository.bigping` ZIP deterministically, rooted at `repository.bigping/`.
- [ ] Lay out `addons.xml`, `addons.xml.md5`, `<addon.id>/<addon.id>-<version>.zip`, a `.sha256` sidecar per ZIP, and the payload's art, for the current version.
- [ ] Upload the layout with `actions/upload-pages-artifact` and deploy with `actions/deploy-pages`.
- [ ] Confirm the Git tree stays text-only and the served site answers over HTTPS.

---

### Task 7: Version bump and local rehearsal

**Files:**
- Modify: `service.advancedproxy/addon.xml`
- Modify: `README.md` if release notes/version references require it.

- [ ] Bump addon version to `0.3.0`.
- [ ] Run full Python suite.
- [ ] Run validation scripts.
- [ ] Build and verify all eight platform ZIPs plus the universal ZIP.
- [ ] Run the repository generator and inspect `addons.xml`, `addons.xml.md5`, and the emitted paths.
- [ ] Run release planner in dry-run mode and verify nine assets plus SHA256SUMS.

---

### Task 8: Create repositories and publish

**Required skill:** `git-master`.

- [ ] Inspect status, diff, recent log, and commit style.
- [ ] Create atomic commits matching implementation boundaries.
- [ ] Create public source repo: `gh repo create maratdob118/bigping --public --source=. --remote=origin`.
- [ ] Create public target repo: `gh repo create maratdob118/bigping.repository --public`.
- [ ] Enable GitHub Pages on the target repo with GitHub Actions as the source.
- [ ] Bootstrap `bootstrap/bigping.repository/` into the target by hand and push it, before the first publish; see `bootstrap/README.md`.
- [ ] Create a fine-grained PAT scoped to `maratdob118/bigping.repository` with `Contents: write` only; record its expiry date and set a rotation reminder.
- [ ] Store it as secret `BIGPING_REPOSITORY_TOKEN` in the source repo.
- [ ] Push `main` without force.
- [ ] Watch Actions: `gh run watch`.
- [ ] Verify release: `gh release view v0.3.0` and confirm eight platform ZIPs, the universal ZIP, and SHA256SUMS.
- [ ] Verify the Pages site serves `addons.xml`, `addons.xml.md5`, and the canonical universal ZIP path over HTTPS.
- [ ] Return source repo, target repo, release, and Pages URLs.

---

### Task 9: Live repository-install acceptance

- [ ] Install `repository.bigping-<version>.zip` on a clean Kodi profile via "Install from zip file".
- [ ] Confirm Advanced Proxy appears under Install from repository → BigPing → Services with the expected version.
- [ ] Install it from the repository and confirm the download comes from the Pages `datadir`.
- [ ] Confirm the universal ZIP installs on armv7 (LibreELEC) and the correct binary directory is selected at runtime.
- [ ] Publish a newer version and confirm Kodi offers and applies the update after a repository refresh.
- [ ] Confirm no checksum-mismatch, unreachable-`datadir`, or unsupported-structure errors in the Kodi log.

---

### Commit boundaries

1. `test: add addon and version validation`
2. `build: assemble universal multi-platform ZIP`
3. `build: add version-driven release helper`
4. `build: generate Kodi repository metadata`
5. `ci: release ZIPs and publish Kodi repository`
6. `release: bump addon to 0.3.0`
