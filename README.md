# kodi-advanced-proxy

Kodi service addon that runs a bundled **sing-box** binary as a local mixed
SOCKS5/HTTP proxy, builds its config from a subscription URL, and auto-switches
outbound via `urltest` (latency-based, with tolerance).

## What it does

- Ships official sing-box binaries per platform in `resources/bin/<platform>/`
- On Kodi startup (service extension) it:
  1. detects the platform (`osarch.py`)
  2. copies the matching sing-box into the writable profile dir
  3. fetches the subscription, parses `vless://` / `hy2://` (`trojan`/`xhttp`
     skipped by default), builds a sing-box config with a `urltest` group
  4. validates it with `sing-box check`
  5. launches sing-box listening on `127.0.0.1:1080` (mixed SOCKS5+HTTP)
- Watches the process and restarts it on crash with exponential backoff
- Re-pulls the subscription and reloads config every 3 minutes
- Reacts to settings changes live

You then point Kodi **Settings → Services → Proxy** at `127.0.0.1:1080`.

## Layout

```
service.advancedproxy/
├── addon.xml                 # xbmc.service extension (start=startup)
├── main.py                   # Kodi service entry (xbmc.Monitor loop)
├── resources/
│   ├── settings.xml          # subscription url, ports, urltest params
│   ├── language/.../strings.po
│   └── bin/<platform>/       # sing-box + version per platform
└── src/
    ├── osarch.py             # platform detection -> linux_x64/armv7/...
    ├── binary_manager.py     # binary locate/download/launch/stop (Kodi-free)
    ├── config_gen.py         # subscription -> sing-box config (Kodi-free)
    ├── supervisor.py         # keep-alive + reload orchestration (Kodi-free)
    └── helpers.py            # the ONLY xbmc* consumer (settings/paths)

dev_run.py                    # run the supervisor WITHOUT Kodi (dev harness)
tests/test_core.py            # unittest suite (Kodi-free)
build.sh                      # build per-platform addon zips into dist/
_template_shadowsocks/        # reference calque (conwnet/shadowsocks-kodi)
_vendor/kodistubs/            # xbmc API stubs for IDEs
```

## Design notes

- **Kodi-free core.** `osarch`, `binary_manager`, `config_gen`, `supervisor`
  never import `xbmc*`; only `helpers.py` (settings/paths) and `main.py`
  (monitor loop) do. This makes the logic testable on any machine.
- **Binary lifecycle** follows the Elementum pattern: bundled
  `resources/bin/<platform>/sing-box` is copied to the writable profile dir and
  `chmod +x`; if absent it is downloaded from the official SagerNet release for
  the detected platform. A `mixed` inbound serves both SOCKS5 and HTTP on one
  port, which is exactly what Kodi's proxy settings expect.
- **urltest** uses `interval`, `tolerance` (switch only when a node beats the
  current one by N ms) and `interrupt_exist_connections` from settings.

## Local development (no Kodi)

```bash
# generate + validate config only
python3 dev_run.py --no-run

# run the proxy locally for N seconds (uses 127.0.0.1:1080)
python3 dev_run.py --seconds 30 &
curl --proxy http://127.0.0.1:1080 https://ifconfig.me

# unit tests
python3 tests/test_core.py
```

## Build addon zips

```bash
./build.sh                 # all platforms -> dist/
./build.sh linux_armv7     # single platform
```

Each `dist/service.advancedproxy-<ver>.<platform>.zip` contains identical
Python + one binary, installable via Kodi "Install from zip".

## Status

Local x86_64 flow verified end-to-end: platform detect, config gen (22
outbounds / 12 skipped), `sing-box check` passes, proxy answers on 1080 for both
HTTP and SOCKS5, watchdog restart on `kill -9` works, 17/17 unit tests pass.

Porting to armv7 (Raspberry Pi / LibreELEC) and other arches is the next phase
( binaries already downloadable via `build.sh <platform>`).
