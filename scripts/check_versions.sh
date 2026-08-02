#!/bin/bash
# check_versions.sh [repo-root]
#
# Version drift guard for the Advanced Proxy addon.  addon.xml is the
# source of truth for the ADDON version; engine versions are pinned at
# build time (build.sh) and mirrored at runtime (src/binary_manager.py)
# and in per-platform stamp files (resources/bin/<platform>/version and
# .../xray_version).
#
# Exits non-zero on any mismatch or missing source file, so CI can gate
# builds and releases on consistency.
set -uo pipefail

REPO="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
echo "checking repo: $REPO"

ADDON_XML="$REPO/service.advancedproxy/addon.xml"
BUILD_SH="$REPO/build.sh"
BINARY_MANAGER="$REPO/service.advancedproxy/src/binary_manager.py"

fail=0
die() { echo "ERROR: $*" >&2; fail=1; }

# extract <file> <varname> -> value of `varname = "..."` (any spacing)
extract() {
  sed -n -E "s/^[[:space:]]*$2[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$1" | head -n1
}

# ---- sources must exist --------------------------------------------------
[[ -f "$ADDON_XML" ]]      || die "missing $ADDON_XML"
[[ -f "$BUILD_SH" ]]       || die "missing $BUILD_SH"
[[ -f "$BINARY_MANAGER" ]] || die "missing $BINARY_MANAGER"
if (( fail )); then echo "version check FAILED"; exit 1; fi

# ---- addon version (addon.xml is the source of truth) --------------------
addon_xml_version="$(sed -n -E 's/.*<addon[^>]*version="([^"]+)".*/\1/p' "$ADDON_XML" | head -n1)"
if [[ -z "$addon_xml_version" ]]; then
  die "could not read version attribute from $ADDON_XML"
elif [[ ! "$addon_xml_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  die "addon.xml version '$addon_xml_version' is not X.Y.Z"
fi

addon_build="$(extract "$BUILD_SH" 'ADDON_VERSION')"
if [[ -n "$addon_build" ]]; then
  [[ "$addon_build" == "$addon_xml_version" ]] \
    || die "addon version drift: addon.xml=$addon_xml_version build.sh=$addon_build"
  echo "addon version : $addon_xml_version (addon.xml == build.sh)"
else
  echo "addon version : $addon_xml_version (build.sh has no ADDON_VERSION pin)"
fi

# ---- engine versions: build.sh vs runtime pin ----------------------------
sb_build="$(extract "$BUILD_SH" 'SINGBOX_VERSION')"
xr_build="$(extract "$BUILD_SH" 'XRAY_VERSION')"
sb_runtime="$(extract "$BINARY_MANAGER" 'SINGBOX_VERSION')"
xr_runtime="$(extract "$BINARY_MANAGER" 'XRAY_VERSION')"

[[ -n "$sb_build" ]]   || die "build.sh missing SINGBOX_VERSION"
[[ -n "$xr_build" ]]   || die "build.sh missing XRAY_VERSION"
[[ -n "$sb_runtime" ]] || die "binary_manager.py missing SINGBOX_VERSION"
[[ -n "$xr_runtime" ]] || die "binary_manager.py missing XRAY_VERSION"

[[ "$sb_build" == "$sb_runtime" ]] \
  || die "sing-box drift: build.sh=$sb_build runtime=$sb_runtime"
[[ "$xr_build" == "$xr_runtime" ]] \
  || die "xray drift: build.sh=$xr_build runtime=$xr_runtime"
echo "sing-box      : build $sb_build == runtime $sb_runtime"
echo "xray          : build $xr_build == runtime $xr_runtime"

# ---- per-platform stamp files --------------------------------------------
stamp_dir="$REPO/service.advancedproxy/resources/bin"
stamp_count=0
if [[ -d "$stamp_dir" ]]; then
  for f in "$stamp_dir"/*/version "$stamp_dir"/*/xray_version; do
    [[ -f "$f" ]] || continue
    stamp_count=$((stamp_count + 1))
    kind="$(basename "$f")"
    want="$sb_build"; [[ "$kind" == "xray_version" ]] && want="$xr_build"
    got="$(tr -d '[:space:]' < "$f")"
    [[ "$got" == "$want" ]] || die "$f stamps '$got' but pinned $want"
  done
fi
if (( stamp_count == 0 )); then
  echo "stamps        : none in source checkout (artifact stamps verified separately)"
else
  echo "stamps        : $stamp_count version stamp file(s) consistent"
fi

if (( fail )); then
  echo "version check FAILED" >&2
  exit 1
fi
echo "version check OK"
