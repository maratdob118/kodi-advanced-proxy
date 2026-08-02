# Licensing and Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** License the addon under GPL-3.0-or-later, preserve engine notices, remove binaries from Git, and keep every distributed ZIP — per-platform and universal — self-contained.

**Architecture:** License sources are committed as small text files; engines are downloaded by `build.sh` and bundled with notices into per-platform ZIPs, which are then aggregated into the universal ZIP served by the Kodi repository. Git ignores all engine binaries in the source monorepo, and the target repository `maratdob118/bigping.repository` stays text-only.

**Tech Stack:** Bash, ZIP, upstream pinned release assets.

## Global Constraints

- Addon license: GPL-3.0-or-later.
- Addon id stays `service.advancedproxy`.
- sing-box: GPL-3.0-or-later plus name/association restriction and JA3 BSD-3-Clause notice.
- Xray-core: MPL-2.0, unmodified separate executable.
- Engine binaries never enter new Git commits but must exist inside installable ZIPs.
- Two ZIP shapes must both be license-complete: the eight per-platform ZIPs attached to the source GitHub Release, and the single universal ZIP (~235 MB, all eight platforms) served by the Kodi repository.
- No ZIP is ever committed to Git in either repository; the universal ZIP exceeds the 100 MB Git blob limit and reaches users as a release asset and then a GitHub Pages artifact.
- No history rewrite.

---

### Task 1: Add licensing material

**Files:**
- Create: `LICENSE`
- Create: `THIRD_PARTY_NOTICES.md`
- Create: `service.advancedproxy/resources/licenses/sing-box/LICENSE`
- Create: `service.advancedproxy/resources/licenses/sing-box/NOTICE`
- Create: `service.advancedproxy/resources/licenses/xray/LICENSE`
- Modify: `service.advancedproxy/addon.xml`
- Modify: `README.md`

- [ ] Fetch exact texts from pinned upstream tags and verify provenance.
- [ ] Add full GPLv3 text and set addon metadata to `GPL-3.0-or-later`.
- [ ] Document pinned versions, source URLs, and redistribution notices.
- [ ] Add README licensing and bundled-engine sections.
- [ ] Validate required strings with grep and XML parsing.

---

### Task 2: Stop tracking binaries

**Files:**
- Modify: `.gitignore`
- Remove from Git index: `service.advancedproxy/resources/bin/**`

- [ ] Add `service.advancedproxy/resources/bin/` to `.gitignore`.
- [ ] Run explicit `git rm --cached` for currently tracked binary/version files.
- [ ] Confirm files remain on disk and are ignored.

Run:

```bash
git check-ignore service.advancedproxy/resources/bin/linux_x64/sing-box
git ls-files service.advancedproxy/resources/bin
```

Expected: first succeeds, second produces no output after commit.

---

### Task 3: Make build version-driven and license-complete

**Files:**
- Modify: `build.sh`
- Create: `scripts/verify_zip.sh`

**Interfaces:**
- `./build.sh --print-version` prints the version parsed from `addon.xml` and exits.
- `scripts/verify_zip.sh ZIP PLATFORM VERSION` exits non-zero on incomplete/mixed ZIPs.

- [ ] Write a failing ZIP verification against a deliberately incomplete fixture.
- [ ] Derive default addon version from `addon.xml`; preserve `--addon-version` override.
- [ ] Copy pinned license/notice files beside downloaded engine binaries.
- [ ] Ensure root `LICENSE` and `THIRD_PARTY_NOTICES.md` are inside every ZIP.
- [ ] Verify one-platform-only invariant, executable/version stamps, and notices.

Run:

```bash
./build.sh linux_arm64
scripts/verify_zip.sh dist/service.advancedproxy-$(./build.sh --print-version).linux_arm64.zip linux_arm64 "$(./build.sh --print-version)"
```

---

### Task 4: Keep the universal ZIP license-complete

**Files:**
- Modify: `scripts/verify_zip.sh`
- Test: `tests/test_packaging.py`

**Interfaces:**
- `scripts/verify_zip.sh ZIP universal VERSION` exits non-zero when any platform directory or notice is missing.

The universal ZIP is the only artifact the Kodi repository can serve, because a Kodi repository resolves each addon version to exactly one path, `<datadir>/<addon.id>/<addon.id>-<version>.zip`, with no OS/arch negotiation. It therefore ships every platform's binaries and must carry every platform's notices.

- [ ] Write a failing verification for a universal ZIP whose engine licenses were dropped during aggregation.
- [ ] Assert root `LICENSE` and `THIRD_PARTY_NOTICES.md` are present in the universal ZIP.
- [ ] Assert pinned sing-box and Xray license/notice files sit beside the binaries in all eight platform directories.
- [ ] Assert the one-platform-only invariant is inverted here: all eight platform directories must be present and version-stamped.

Run:

```bash
scripts/build_universal.sh --version "$(./build.sh --print-version)" --dist-dir dist
scripts/verify_zip.sh dist/service.advancedproxy-$(./build.sh --print-version).zip universal "$(./build.sh --print-version)"
```

---

### Commit boundaries

1. `docs: license addon under GPL-3.0-or-later`
2. `chore: stop tracking bundled engine binaries`
3. `build: bundle engine licenses in platform ZIPs`
4. `build: verify licenses in universal ZIP`
