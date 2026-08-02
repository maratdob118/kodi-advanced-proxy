# Advanced Proxy (`service.advancedproxy`)

Kodi service addon that runs a bundled **sing-box** or **Xray** binary as a local
mixed SOCKS5/HTTP proxy, builds its config from profile links or a subscription
URL, and switches outbound either manually or automatically via `urltest`
(latency-based, with tolerance).

The addon id is **`service.advancedproxy`** and does not change.

## What it does

- Ships official sing-box and Xray binaries per platform in `resources/bin/<platform>/`
- On Kodi startup (service extension) it:
  1. detects the platform (`osarch.py`)
  2. copies the matching engine binary into the writable profile dir
  3. loads profiles / fetches the subscription, parses `vless://`, `hy2://` and
     `trojan://` links, and builds an engine config
  4. validates the config with the engine's own check command
  5. launches the engine on a local mixed SOCKS5+HTTP port (default `1080`,
     with free-port fallback when the port is taken)
- Watches the process and restarts it on crash with exponential backoff
- Re-pulls the subscription and reloads config periodically
- Reacts to settings changes live
- Points Kodi's own proxy settings (and supported addons such as
  `plugin.video.youtube`) at the **effective** port, and restores the previous
  values on a clean stop — see `docs/superpowers/specs/`

## Distribution: two repositories

Source and distribution live in two separate GitHub repositories.

| Repository | Role | Contents |
| --- | --- | --- |
| `maratdob118/kodi-advanced-proxy` | Source monorepo (this repo) | Addon source, build/test tooling, CI. Publishes a GitHub Release per addon version. |
| `maratdob118/kodi-addons` | Generated Kodi repository | Text-only in Git (`addons.xml`, `addons.xml.md5`, repository addon metadata). Binary payload is served from GitHub Pages. |

The Kodi repository addon is a second, separate addon: **`repository.bigping`**.
Installing it once lets Kodi discover and auto-update Advanced Proxy.

### Two artifact shapes, on purpose

- **Eight per-platform ZIPs** — `service.advancedproxy-<version>.<platform>.zip`
  for `linux_x64`, `linux_x86`, `linux_armv7`, `linux_arm64`, `android_arm64`,
  `windows_x64`, `darwin_x64`, `darwin_arm64`. These stay on the **GitHub
  Release** of the source repo for manual/offline "Install from zip" use. Each
  contains exactly one platform's binaries, so it stays small.
- **One universal ZIP** — `service.advancedproxy-<version>.zip`, containing all
  eight platform binary directories (~235 MB). This is the artifact the **Kodi
  repository** serves, and the addon selects the right binary at runtime.

A Kodi repository has exactly **one canonical ZIP path per addon version**:

```
<datadir>/<addon.id>/<addon.id>-<version>.zip
```

`addons.xml` declares a single `<addon id="service.advancedproxy" version="X.Y.Z">`
entry, and Kodi resolves it to that one path. There is no OS/arch negotiation in
the repository protocol — no per-platform variant selection, no fallback list.
So the ZIP behind that single path must work on every supported platform, which
is why the universal ZIP exists and why per-platform ZIPs cannot be used for the
repository install path.

### Why GitHub Pages hosts the ZIPs

The universal ZIP is roughly 235 MB. GitHub rejects any single file over
**100 MB** pushed into a Git repository, so the universal ZIP can never be a Git
blob in `kodi-addons` (and `raw.githubusercontent.com` only serves Git
blobs, so it is not an option either).

Instead:

- `kodi-addons` stays **text-only in Git** — nothing large is ever
  committed.
- The binary payload is published as a **GitHub Pages deployment artifact**,
  which is not a Git commit and is not subject to the blob limit.
- Pages serves everything over **HTTPS**, which Kodi 20+ expects for a
  repository `datadir`.
- A deployment replaces the whole site, so Pages carries only the **current**
  version: one ~235 MB payload, well under the 1 GB artifact ceiling. Nothing is
  pruned, because nothing old is uploaded. Every historic asset stays on the
  source repo's releases, which is where a rollback or an offline install comes
  from.
- Each published ZIP is served with a `<zip>.sha256` sidecar. Kodi reads a
  `content-sha256` response header first and falls back to that sidecar; Pages
  cannot set response headers, so the sidecar is the only digest channel
  available and is mandatory.

Planned repository layout as served by Pages:

```
https://maratdob118.github.io/kodi-addons/
├── addons.xml                 # all addons + versions offered by this repo
├── addons.xml.md5             # md5 of addons.xml; Kodi polls this for changes
├── repository.bigping/
│   ├── repository.bigping-<version>.zip
│   └── repository.bigping-<version>.zip.sha256
└── service.advancedproxy/
    ├── service.advancedproxy-<version>.zip     # universal, all platforms
    ├── service.advancedproxy-<version>.zip.sha256
    └── resources/             # icon/fanart addons.xml resolves, from the payload
```

`repository.bigping` uses the Kodi 20+ (Nexus and later) repository form, where
`<info>`, `<checksum>` and `<datadir>` are wrapped in a `<dir>` element rather
than placed directly under the extension point:

```xml
<extension point="xbmc.addon.repository" name="BigPing">
  <dir minversion="20.0.0">
    <info compressed="false">https://maratdob118.github.io/kodi-addons/addons.xml</info>
    <checksum verify="md5">https://maratdob118.github.io/kodi-addons/addons.xml.md5</checksum>
    <datadir zip="true">https://maratdob118.github.io/kodi-addons/</datadir>
  </dir>
</extension>
```

### Publishing flow

1. **Source release flow** (`maratdob118/kodi-advanced-proxy`): on a push to
   `main`, CI runs
   tests, builds the eight per-platform ZIPs plus the universal ZIP, and — if
   the version in `addon.xml` has no release yet — publishes GitHub Release
   `vX.Y.Z` with all ZIPs and checksums. Uses the repository-scoped
   `GITHUB_TOKEN`.
2. **Target Pages flow** (`maratdob118/kodi-addons`): the source repo
   commits regenerated `addons.xml` / `addons.xml.md5` / `manifest.json` /
   repository metadata to the target repo using a **fine-grained** PAT stored as
   `KODI_ADDONS_TOKEN`, scoped to that one repository with the single
   permission **Contents: write**. That commit triggers the target repo's own
   Pages workflow, which downloads the universal ZIP from the source Release,
   checks its bytes against the SHA256 the manifest measured at build time, and
   deploys it under the canonical path with its digest sidecar. A classic PAT is
   not used.

The target repo's Pages workflow and its site builder are **bootstrapped by
hand**, once, from the template in `bootstrap/bigping.repository/`. They are
deliberately not part of the tree the publisher writes: a token that could
create or edit `.github/workflows/` would need the `workflows` permission, so
keeping the workflow outside the published set is what holds the PAT down to
`Contents: write`. Later releases only push generated metadata over it.

Both flows are keyed by addon version and are idempotent: re-running for an
already-released version is a no-op, and concurrency is non-cancelling so two
pushes can never race a release.

## Installing

**From the Kodi repository (recommended, gives auto-updates):** install
`repository.bigping-<version>.zip` once via **Add-ons → Install from zip file**,
then **Add-ons → Install from repository → BigPing → Services → Advanced
Proxy**.

**Manually:** download the ZIP matching your platform from the source repo's
GitHub Release and install it via **Install from zip file**. No auto-updates.

> Status: the repository and Pages site described above are the designed target
> and are not published yet. The URLs are the intended ones, not live endpoints.

## Layout

```
service.advancedproxy/
├── addon.xml                 # xbmc.service extension (start=startup)
├── main.py                   # Kodi service entry (xbmc.Monitor loop)
├── default.py                # addon menu entry point
├── resources/
│   ├── settings.xml          # profiles, subscription url, ports, urltest params
│   ├── language/.../strings.po
│   ├── licenses/             # pinned upstream engine licenses/notices
│   └── bin/<platform>/       # engine binaries (git-ignored, fetched by build.sh)
└── src/
    ├── osarch.py             # platform detection -> linux_x64/armv7/...
    ├── binary_manager.py     # binary locate/download/launch/stop (Kodi-free)
    ├── parsers.py            # proxy link parsing (Kodi-free)
    ├── profiles.py           # profile storage/selection (Kodi-free)
    ├── build_singbox.py      # profiles -> sing-box config (Kodi-free)
    ├── build_xray.py         # profiles -> Xray config (Kodi-free)
    ├── port_utils.py         # free-port fallback (Kodi-free)
    ├── supervisor.py         # keep-alive + reload orchestration (Kodi-free)
    ├── proxy_integration.py  # Kodi/addon proxy settings sync (Kodi-free)
    └── helpers.py            # the ONLY xbmc* consumer (settings/paths/JSON-RPC)

build.sh                      # build addon zips into dist/
dev_run.py                    # run the supervisor WITHOUT Kodi (dev harness)
scripts/                      # version/addon/ZIP validation and release helpers
bootstrap/bigping.repository/ # template bootstrapped by hand into the Kodi repo
tests/                        # unittest suites (Kodi-free)
docs/superpowers/             # design spec and implementation plans
.github/workflows/            # CI, build matrix, release, repository publish
```

## Design notes

- **Kodi-free core.** Everything under `src/` except `helpers.py` avoids
  importing `xbmc*`; only `helpers.py` (settings/paths/JSON-RPC) and `main.py`
  (monitor loop) touch the Kodi API. This makes the logic testable on any
  machine.
- **Binary lifecycle** follows the Elementum pattern: the bundled
  `resources/bin/<platform>/` engine is copied to the writable profile dir and
  `chmod +x`; if absent it is downloaded from the official upstream release for
  the detected platform. A `mixed` inbound serves both SOCKS5 and HTTP on one
  port, which is exactly what Kodi's proxy settings expect.
- **urltest** uses `interval`, `tolerance` (switch only when a node beats the
  current one by N ms) and `interrupt_exist_connections` from settings.
- **Binaries are not tracked in Git.** `resources/bin/` is ignored; `build.sh`
  downloads pinned upstream release assets at build time.

## Local development (no Kodi)

```bash
# generate + validate config only
python3 dev_run.py --no-run

# run the proxy locally for N seconds
python3 dev_run.py --seconds 30 &
curl --proxy http://127.0.0.1:1080 https://ifconfig.me

# unit tests
python3 -m unittest discover -s tests -v

# metadata / version consistency checks
bash scripts/check_versions.sh .
python3 scripts/validate_addon.py .
```

## Build addon zips

```bash
./build.sh                 # all platforms -> dist/
./build.sh linux_armv7     # single platform
./build.sh --print-version # addon version from addon.xml
```

Each `dist/service.advancedproxy-<ver>.<platform>.zip` contains identical Python
code plus that platform's engine binaries, installable via Kodi "Install from
zip". The universal ZIP used by the Kodi repository carries all platforms'
binaries instead.

## Bundled engines

Each release zip bundles the official, unmodified release binaries of two
separate proxy engines next to the addon's own Python code:

| Engine | Version | License | Release |
| --- | --- | --- | --- |
| sing-box | v1.13.14 | GPL-3.0-or-later (with name-association restriction; JA3 component BSD-3-Clause) | <https://github.com/SagerNet/sing-box/releases/tag/v1.13.14> |
| Xray-core | v25.8.3 | MPL-2.0 | <https://github.com/XTLS/Xray-core/releases/tag/v25.8.3> |

The engines run as separate executables: the addon launches them as child
processes and never links against them. Their binaries are unmodified. The
exact license texts, including sing-box's JA3 BSD-3-Clause notice, are pinned
under `service.advancedproxy/resources/licenses/` and copied into every zip
beside the binaries.

## Licensing

The addon itself is licensed under the GNU General Public License, version 3
or later (`GPL-3.0-or-later`); see the root `LICENSE` and
`THIRD_PARTY_NOTICES.md` files, which are also included inside every release
zip. The bundled engines keep their own licenses:

- **sing-box v1.13.14** is GPL-3.0-or-later, with an additional term in its
  license that no derivative work may use the name "sing-box" or imply
  association with the project without prior consent. Its JA3 fingerprinting
  component is BSD-3-Clause (Copyright (c) 2018, Open Systems AG).
- **Xray-core v25.8.3** is MPL-2.0, reproduced in full under
  `service.advancedproxy/resources/licenses/xray/LICENSE`.

Source code for both engines is available from the pinned release tags linked
above.

## Status

Local x86_64 flow verified end-to-end: platform detection, config generation,
engine config check passes, the proxy answers on the local port for both HTTP
and SOCKS5, watchdog restart after `kill -9` works, and the unit test suite
passes.

Not yet done: publishing the two repositories, the Pages-hosted Kodi repository,
and acceptance testing of the live repository-install path on armv7
(Raspberry Pi / LibreELEC). Binaries for the other architectures are already
fetchable via `build.sh <platform>`.
