# GitHub CI and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Test and build all supported ZIPs on every push/PR and publish exactly one GitHub Release for each new addon version pushed to master.

**Architecture:** Read-only test/build jobs produce per-platform artifacts. A master-only release job with `contents: write` aggregates artifacts, checks `v<addon-version>`, creates a draft release, uploads ZIPs/checksums, then publishes.

**Tech Stack:** GitHub Actions, Python unittest, Bash, GitHub CLI.

## Global Constraints

- Public repository: `maratdob118/kodi-advanced-proxy`.
- `addon.xml` is the addon-version source of truth.
- Tests/builds run on push to `master`, PR to `master`, and workflow_dispatch.
- Release only when tag/release `vX.Y.Z` does not exist.
- Only release job gets `contents: write`.
- Build matrix: linux_x64, linux_x86, linux_armv7, linux_arm64, android_arm64, windows_x64, darwin_x64, darwin_arm64.

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

### Task 2: Add release planner

**Files:**
- Create: `scripts/release.py`
- Test: `tests/test_release_logic.py`

**Interfaces:**

```bash
python3 scripts/release.py --version X.Y.Z --assets-dir DIR --sha SHA
python3 scripts/release.py --version X.Y.Z --assets-dir DIR --dry-run
```

- [ ] Write failing pure tests for create-vs-skip and asset validation.
- [ ] Implement SHA256SUMS generation.
- [ ] Skip cleanly when `gh release view vX.Y.Z` succeeds.
- [ ] Otherwise create draft, upload all ZIPs/checksum, then publish.
- [ ] Abort without publication when any asset/upload fails.

---

### Task 3: Add GitHub Actions workflow

**Files:**
- Create: `.github/workflows/release.yml`

- [ ] Add triggers for push/PR master and workflow_dispatch.
- [ ] Set default `permissions: contents: read`.
- [ ] Add test job: unittest + version/license/addon checks.
- [ ] Add 8-platform Ubuntu matrix with `build.sh`, `verify_zip.sh`, and artifact upload.
- [ ] Add master-only release job with `contents: write`, artifact aggregation, and `release.py`.
- [ ] Add non-cancelling concurrency for release runs.
- [ ] Validate YAML and inspect workflow permissions.

---

### Task 4: Version bump and local release rehearsal

**Files:**
- Modify: `service.advancedproxy/addon.xml`
- Modify: `README.md` if release notes/version references require it.

- [ ] Bump addon version to `0.3.0`.
- [ ] Run full Python suite.
- [ ] Run validation scripts.
- [ ] Build and verify all eight platform ZIPs.
- [ ] Run release planner in dry-run mode and verify eight assets plus SHA256SUMS.

---

### Task 5: Create and publish repository

**Required skill:** `git-master`.

- [ ] Inspect status, diff, recent log, and commit style.
- [ ] Create atomic commits matching implementation boundaries.
- [ ] Create public repo: `gh repo create maratdob118/kodi-advanced-proxy --public --source=. --remote=origin`.
- [ ] Push `master` without force.
- [ ] Watch Actions: `gh run watch`.
- [ ] Verify release: `gh release view v0.3.0` and confirm all eight ZIPs plus SHA256SUMS.
- [ ] Return repository and release URLs.

---

### Commit boundaries

1. `test: add addon and version validation`
2. `build: add version-driven release helper`
3. `ci: build and release platform ZIPs`
4. `release: bump addon to 0.3.0`
