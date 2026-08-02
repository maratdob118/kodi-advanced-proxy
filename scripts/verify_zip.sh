#!/bin/bash
# verify_zip.sh [options] ZIP PLATFORM VERSION
# verify_zip.sh --universal [options] ZIP VERSION
#
# Release gate for a built Advanced Proxy platform ZIP.  Rejects
# incomplete and mixed-platform packages before they reach GitHub.
#
# Checks, against the pinned engine versions in build.sh:
#   * the archive is a valid zip
#   * the filename matches service.advancedproxy-<X.Y.Z>.<platform>.zip
#     and carries exactly that one platform's resources/bin/<platform>
#     directory (mixed zips bundling more than one platform are rejected)
#   * sing-box is always bundled; Xray is required except on platforms
#     with no pinned Xray asset (e.g. android_arm64)
#   * version / xray_version stamps match the pinned sing-box / Xray
#     versions, and no stamp claims an engine that is not bundled
#   * addon.xml inside the zip carries the expected addon version
#   * notices are bundled: root LICENSE and THIRD_PARTY_NOTICES.md,
#     the canonical resources/licenses/* copies, and the unambiguous
#     sing-box-LICENSE / sing-box-NOTICE / xray-LICENSE files that
#     build.sh places beside the engine binaries
#
# In --universal mode the very same per-platform checks run for every
# platform build.sh declares, and additionally:
#   * the filename must be service.advancedproxy-<X.Y.Z>.zip
#   * the zip must carry exactly all declared platform bin dirs, no more
#   * no entry may be a nested platform zip
#   * no entry name may appear twice
#
# Options:
#   --universal             verify a merged all-platform zip (ZIP VERSION)
#   --repo DIR              repo root holding build.sh / addon.xml
#                           (default: parent of scripts/)
#
# Exit 0 on success, 1 when any check fails, 2 on usage errors.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd "$HERE/.." && pwd)}"
ADDON="service.advancedproxy"
EXPECTED_ADDON_VERSION=""
EXPECTED_SINGBOX_VERSION=""
EXPECTED_XRAY_VERSION=""
UNIVERSAL=0
POSITIONAL=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --universal) UNIVERSAL=1; shift;;
    --addon-version) [[ $# -ge 2 ]] || { echo "verify_zip: --addon-version requires a value" >&2; exit 2; }; EXPECTED_ADDON_VERSION="$2"; shift 2;;
    --singbox-version) [[ $# -ge 2 ]] || { echo "verify_zip: --singbox-version requires a value" >&2; exit 2; }; EXPECTED_SINGBOX_VERSION="$2"; shift 2;;
    --xray-version) [[ $# -ge 2 ]] || { echo "verify_zip: --xray-version requires a value" >&2; exit 2; }; EXPECTED_XRAY_VERSION="$2"; shift 2;;
    --repo) [[ $# -ge 2 ]] || { echo "verify_zip: --repo requires a value" >&2; exit 2; }; REPO="$2"; shift 2;;
    -h|--help) sed -n '1,35p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    -*) echo "verify_zip: unknown option: $1" >&2; exit 2;;
    *) POSITIONAL+=("$1"); shift;;
  esac
done

if (( UNIVERSAL )); then
  [[ ${#POSITIONAL[@]} -eq 2 ]] || { echo "verify_zip: expected --universal ZIP VERSION (see --help)" >&2; exit 2; }
  ZIP="${POSITIONAL[0]}"
  EXPECTED_PLATFORM=""
  POSITIONAL_VERSION="${POSITIONAL[1]}"
else
  [[ ${#POSITIONAL[@]} -eq 3 ]] || { echo "verify_zip: expected ZIP PLATFORM VERSION (see --help)" >&2; exit 2; }
  ZIP="${POSITIONAL[0]}"
  EXPECTED_PLATFORM="${POSITIONAL[1]}"
  POSITIONAL_VERSION="${POSITIONAL[2]}"
fi
[[ -f "$ZIP" ]] || { echo "verify_zip: no such file: $ZIP" >&2; exit 2; }
[[ -d "$REPO" ]] || { echo "verify_zip: repo dir not found: $REPO" >&2; exit 2; }
(( UNIVERSAL )) || [[ "$EXPECTED_PLATFORM" =~ ^[a-z0-9_]+$ ]] || { echo "verify_zip: invalid platform: $EXPECTED_PLATFORM" >&2; exit 2; }
[[ "$POSITIONAL_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "verify_zip: invalid version: $POSITIONAL_VERSION" >&2; exit 2; }
if [[ -n "$EXPECTED_ADDON_VERSION" && "$EXPECTED_ADDON_VERSION" != "$POSITIONAL_VERSION" ]]; then
  echo "verify_zip: --addon-version differs from positional VERSION" >&2
  exit 2
fi

BUILD_SH="$REPO/build.sh"
fail=0
die() { echo "FAIL: $*"; fail=$((fail + 1)); }

# ---- helpers --------------------------------------------------------------
extract() {  # $1 varname from build.sh  (any spacing around '=')
  sed -n -E "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$BUILD_SH" | head -n1
}

# ---- archive integrity + file list ---------------------------------------
if ! unzip -tq "$ZIP" >/dev/null 2>&1; then
  echo "FAIL: not a valid zip archive: $ZIP"
  exit 1
fi
ENTRIES="$(unzip -Z1 "$ZIP")"

has_entry() { printf '%s\n' "$ENTRIES" | grep -Fxq "$1"; }

entry_count() { printf '%s\n' "$ENTRIES" | grep -Fxc "$1" || true; }

entry_nonempty() {  # $1 entry: must exist and carry at least one byte
  local count size
  count="$(entry_count "$1")"
  if [[ "$count" == "1" ]]; then
    size="$(unzip -p "$ZIP" "$1" 2>/dev/null | wc -c)"
    [[ "${size:-0}" -gt 0 ]] || die "$1 is empty"
  elif [[ "$count" -gt 1 ]]; then
    die "duplicate archive entry: $1"
  else
    die "missing required file: $1"
  fi
}

entry_matches_file() {
  local entry="$1" source="$2" count
  count="$(entry_count "$entry")"
  if [[ "$count" == "0" ]]; then
    die "missing required file: $entry"
  elif [[ "$count" -gt 1 ]]; then
    die "duplicate archive entry: $entry"
  elif [[ ! -f "$source" ]]; then
    die "missing canonical source: $source"
  elif ! cmp -s <(unzip -p "$ZIP" "$entry" 2>/dev/null) "$source"; then
    die "$entry differs from canonical source $source"
  fi
}

stamp_eq() {  # $1 entry, $2 expected version string
  local got count
  count="$(entry_count "$1")"
  [[ "$count" == "1" ]] || return 1
  got="$(unzip -p "$ZIP" "$1" 2>/dev/null | tr -d '[:space:]')"
  [[ "${got:-}" == "$2" ]]
}

# ---- expected identity from the filename ---------------------------------
base="$(basename "$ZIP")"
if (( UNIVERSAL )); then
  filename_version="$(printf '%s' "$base" | sed -n -E "s/^$ADDON-([0-9]+\.[0-9]+\.[0-9]+)\.zip$/\1/p")"
  if [[ -z "$filename_version" ]]; then
    echo "FAIL: filename does not match $ADDON-<X.Y.Z>.zip: $base"
    exit 1
  fi
else
  filename_version="$(printf '%s' "$base" | sed -n -E "s/^$ADDON-([0-9]+\.[0-9]+\.[0-9]+)\.[a-z0-9_]+\.zip$/\1/p")"
  filename_platform="$(printf '%s' "$base" | sed -n -E "s/^$ADDON-[0-9]+\.[0-9]+\.[0-9]+\.([a-z0-9_]+)\.zip$/\1/p")"
  if [[ -z "$filename_version" || -z "$filename_platform" ]]; then
    echo "FAIL: filename does not match $ADDON-<X.Y.Z>.<platform>.zip: $base"
    exit 1
  fi
  [[ "$filename_platform" == "$EXPECTED_PLATFORM" ]] \
    || die "filename platform $filename_platform != expected $EXPECTED_PLATFORM"
fi
[[ "$filename_version" == "$POSITIONAL_VERSION" ]] \
  || die "filename version $filename_version != expected $POSITIONAL_VERSION"

# ---- pinned engine versions from build.sh --------------------------------
SB_VERSION="${EXPECTED_SINGBOX_VERSION:-$(extract SINGBOX_VERSION)}"
XR_VERSION="${EXPECTED_XRAY_VERSION:-$(extract XRAY_VERSION)}"
[[ -n "$SB_VERSION" ]] || die "build.sh missing SINGBOX_VERSION ($BUILD_SH)"
[[ -n "$XR_VERSION" ]] || die "build.sh missing XRAY_VERSION ($BUILD_SH)"
supported_platforms="$(sed -n -E 's/^PLATFORMS=\((.*)\)$/\1/p' "$BUILD_SH" | tr ' ' '\n' | sort -u)"
[[ -n "$supported_platforms" ]] || die "build.sh declares no PLATFORMS ($BUILD_SH)"
(( UNIVERSAL )) || printf '%s\n' "$supported_platforms" | grep -Fxq "$EXPECTED_PLATFORM" \
  || die "unsupported platform: $EXPECTED_PLATFORM"
xray_platforms="$(grep -oE '^[[:space:]]*\[[a-z0-9_]+\]="Xray' "$BUILD_SH" \
  | sed -n -E 's/^[[:space:]]*\[([a-z0-9_]+)\].*/\1/p' | sort -u)"
has_xray_asset() { printf '%s\n' "$xray_platforms" | grep -Fxq "$1"; }

# ---- which platform bin dirs may be bundled ------------------------------
platforms="$(printf '%s\n' "$ENTRIES" \
  | sed -n -E "s|^$ADDON/resources/bin/([^/]+)/.*|\1|p" | sort -u)"
oneline() { printf '%s' "$1" | tr '\n' ' '; }
if (( UNIVERSAL )); then
  [[ "$platforms" == "$supported_platforms" ]] \
    || die "universal zip bin dirs [$(oneline "$platforms")] != build.sh platforms [$(oneline "$supported_platforms")]"
  CHECK_PLATFORMS="$supported_platforms"
else
  if [[ -z "$platforms" ]]; then
    die "no resources/bin/<platform> directory in zip"
  elif [[ "$platforms" != "$filename_platform" ]]; then
    die "zip mixes platforms: bin dirs [$platforms] vs filename '$filename_platform'"
  fi
  platform="$EXPECTED_PLATFORM"
  CHECK_PLATFORMS="$EXPECTED_PLATFORM"
fi

# ---- a universal zip carries no nested archive and no repeated name ------
if (( UNIVERSAL )); then
  nested="$(printf '%s\n' "$ENTRIES" | grep -E '\.zip$' || true)"
  [[ -z "$nested" ]] || die "universal zip nests archives: $(oneline "$nested")"
  repeated="$(printf '%s\n' "$ENTRIES" | sort | uniq -d)"
  [[ -z "$repeated" ]] || die "duplicate archive entries: $(oneline "$repeated")"
fi

# ---- per-platform engines and stamps -------------------------------------
LAST_XRAY=""
check_platform_engines() {
  local platform="$1"
  local sb_bin sb_alternate xr_expected xr_alternate sb_entry sb_count
  local xr_entry xr_count xr_bin
  if [[ "$platform" == windows_* ]]; then
    sb_bin="sing-box.exe"; sb_alternate="sing-box"
    xr_expected="xray.exe"; xr_alternate="xray"
  else
    sb_bin="sing-box"; sb_alternate="sing-box.exe"
    xr_expected="xray"; xr_alternate="xray.exe"
  fi
  sb_entry="$ADDON/resources/bin/$platform/$sb_bin"
  sb_count="$(entry_count "$sb_entry")"
  [[ "$sb_count" == "1" ]] || die "expected exactly one $sb_bin in resources/bin/$platform"
  [[ "$sb_count" != "1" ]] || entry_nonempty "$sb_entry"
  has_entry "$ADDON/resources/bin/$platform/$sb_alternate" \
    && die "unexpected alternate executable $sb_alternate for $platform"
  xr_entry="$ADDON/resources/bin/$platform/$xr_expected"
  xr_count="$(entry_count "$xr_entry")"
  xr_bin=""
  if has_xray_asset "$platform"; then
    [[ "$xr_count" == "1" ]] \
      || die "expected exactly one $xr_expected in resources/bin/$platform (Xray asset is pinned)"
    [[ "$xr_count" != "1" ]] || entry_nonempty "$xr_entry"
    xr_bin="$xr_expected"
    has_entry "$ADDON/resources/bin/$platform/$xr_alternate" \
      && die "unexpected alternate executable $xr_alternate for $platform"
  else
    if [[ "$xr_count" != "0" ]] || has_entry "$ADDON/resources/bin/$platform/$xr_alternate"; then
      die "unexpected xray binary for $platform (build.sh pins no Xray asset there)"
    fi
    echo "note: no Xray expected for $platform (no pinned asset)"
  fi

  has_entry "$ADDON/resources/bin/$platform/version" \
    && stamp_eq "$ADDON/resources/bin/$platform/version" "$SB_VERSION" \
    || die "version stamp for $platform missing or != $SB_VERSION"
  if [[ -n "$xr_bin" ]]; then
    has_entry "$ADDON/resources/bin/$platform/xray_version" \
      && stamp_eq "$ADDON/resources/bin/$platform/xray_version" "$XR_VERSION" \
      || die "xray_version stamp missing or != $XR_VERSION"
  elif has_entry "$ADDON/resources/bin/$platform/xray_version"; then
    die "xray_version stamp present but no xray binary bundled"
  fi
  LAST_XRAY="$xr_bin"
}

for bundled_platform in $CHECK_PLATFORMS; do
  check_platform_engines "$bundled_platform"
done

# ---- addon version (filename == zip addon.xml == repo addon.xml) ---------
expected="$POSITIONAL_VERSION"
addon_xml_count="$(entry_count "$ADDON/addon.xml")"
[[ "$addon_xml_count" == "1" ]] || die "expected exactly one $ADDON/addon.xml"
zip_addon_version="$(unzip -p "$ZIP" "$ADDON/addon.xml" 2>/dev/null \
  | sed -n -E 's/.*<addon[^>]*version="([^"]+)".*/\1/p' | head -n1)"
[[ "$filename_version" == "$expected" ]] \
  || die "filename version $filename_version != expected $expected"
[[ "$zip_addon_version" == "$expected" ]] \
  || die "addon.xml inside zip says $zip_addon_version, expected $expected"

# ---- notices --------------------------------------------------------------
entry_matches_file "$ADDON/LICENSE" "$REPO/LICENSE"
entry_matches_file "$ADDON/THIRD_PARTY_NOTICES.md" "$REPO/THIRD_PARTY_NOTICES.md"
entry_matches_file "$ADDON/resources/licenses/sing-box/LICENSE" "$REPO/$ADDON/resources/licenses/sing-box/LICENSE"
entry_matches_file "$ADDON/resources/licenses/sing-box/NOTICE" "$REPO/$ADDON/resources/licenses/sing-box/NOTICE"
entry_matches_file "$ADDON/resources/licenses/xray/LICENSE" "$REPO/$ADDON/resources/licenses/xray/LICENSE"

check_platform_notices() {
  local platform="$1"
  entry_matches_file "$ADDON/resources/bin/$platform/sing-box-LICENSE" "$REPO/$ADDON/resources/licenses/sing-box/LICENSE"
  entry_matches_file "$ADDON/resources/bin/$platform/sing-box-NOTICE" "$REPO/$ADDON/resources/licenses/sing-box/NOTICE"
  if has_xray_asset "$platform"; then
    entry_matches_file "$ADDON/resources/bin/$platform/xray-LICENSE" "$REPO/$ADDON/resources/licenses/xray/LICENSE"
  elif has_entry "$ADDON/resources/bin/$platform/xray-LICENSE"; then
    die "xray-LICENSE present but no xray binary bundled"
  fi
}

for bundled_platform in $CHECK_PLATFORMS; do
  check_platform_notices "$bundled_platform"
done

# ---- verdict --------------------------------------------------------------
if (( fail )); then
  echo "verify_zip: FAILED ($fail problem(s)) in $ZIP"
  exit 1
fi
if (( UNIVERSAL )); then
  echo "verify_zip: OK $ZIP (universal platforms=[$(oneline "$CHECK_PLATFORMS")] addon=$expected sing-box=$SB_VERSION xray=$XR_VERSION)"
else
  echo "verify_zip: OK $ZIP (platform=$platform addon=$expected sing-box=$SB_VERSION xray=${LAST_XRAY:-absent})"
fi
