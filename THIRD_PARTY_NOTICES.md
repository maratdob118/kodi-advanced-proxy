# Third-Party Notices

This addon is distributed under the **GNU General Public License, version 3 or
later** (`GPL-3.0-or-later`). The full license text is in the root
[`LICENSE`](LICENSE) file and also at
<https://www.gnu.org/licenses/gpl-3.0.txt>.

The addon bundles two separate third-party executables. They are unmodified
official release binaries, distributed as separate executables next to this
addon's own Python code. Each engine keeps its own license, reproduced in full
under `service.advancedproxy/resources/licenses/`. These files are copied
beside the binaries in every release package.

## Bundled engines

| Engine | Version | License | Source (pinned tag) |
| --- | --- | --- | --- |
| sing-box | v1.13.14 | GPL-3.0-or-later with name-association restriction; JA3 component BSD-3-Clause | <https://github.com/SagerNet/sing-box/releases/tag/v1.13.14> |
| Xray-core | v25.8.3 | MPL-2.0 | <https://github.com/XTLS/Xray-core/releases/tag/v25.8.3> |

## sing-box (v1.13.14)

- Project: <https://github.com/SagerNet/sing-box>
- Version tag: <https://github.com/SagerNet/sing-box/releases/tag/v1.13.14>
- Full license text: `service.advancedproxy/resources/licenses/sing-box/LICENSE`
- Upstream license file: <https://github.com/SagerNet/sing-box/blob/v1.13.14/LICENSE>

sing-box is free software under the GNU General Public License, version 3 or
later. Its license file adds one restriction beyond stock GPLv3: **no
derivative work may use the name "sing-box" or imply association with the
project without prior consent.** The complete license text, including this
clause, is reproduced verbatim in
`service.advancedproxy/resources/licenses/sing-box/LICENSE`.

### JA3 (BSD 3-Clause)

sing-box includes a JA3 TLS fingerprint implementation. It is licensed under
the BSD 3-Clause License (Copyright (c) 2018, Open Systems AG), reproduced
verbatim from <https://github.com/SagerNet/sing-box/blob/v1.13.14/common/ja3/LICENSE>
in `service.advancedproxy/resources/licenses/sing-box/NOTICE`.

## Xray-core (v25.8.3)

- Project: <https://github.com/XTLS/Xray-core>
- Version tag: <https://github.com/XTLS/Xray-core/releases/tag/v25.8.3>
- Full license text: `service.advancedproxy/resources/licenses/xray/LICENSE`
- Upstream license file: <https://github.com/XTLS/Xray-core/blob/v25.8.3/LICENSE>

Xray-core is distributed under the Mozilla Public License 2.0 (MPL-2.0), which
permits redistribution of unmodified binaries provided this notice and the
license text are included. The complete MPL-2.0 text is reproduced verbatim in
`service.advancedproxy/resources/licenses/xray/LICENSE`. See
<https://www.mozilla.org/en-US/MPL/2.0/> for an annotated copy of the license.

## Source code

Under GPL-3.0-or-later and MPL-2.0 you may request the corresponding source
code for any of the engines. It is available directly from the pinned release
tags linked above; no proprietary modifications are distributed here.
