#!/bin/bash
# Build per-platform addon zips for Advanced Proxy (dual-engine).
#
# Downloads sing-box and Xray release binaries for each requested platform
# into resources/bin/<platform>/, then zips the addon (identical Python code
# + both engine binaries per zip).
#
# Usage:
#   ./build.sh                 # build for all known platforms
#   ./build.sh linux_armv7     # build only one platform
#   ./build.sh --version 1.13.14 linux_x64
#   ./build.sh --addon-version 0.2.2 linux_x64
#   ./build.sh --print-version # print addon version (from addon.xml) and exit
set -ueo pipefail

cd "$(dirname "$0")"

ADDON="service.advancedproxy"
SINGBOX_VERSION="1.13.14"
XRAY_VERSION="25.8.3"
DIST="dist"

# addon.xml is the source of truth; --addon-version may override per run.
if [[ ! -f "$ADDON/addon.xml" ]]; then
  echo "!! missing $ADDON/addon.xml" >&2
  exit 1
fi
_resolved_addon_version="$(sed -n -E 's/.*<addon[^>]*version="([^"]+)".*/\1/p' "$ADDON/addon.xml" | head -n1)"
if [[ ! "$_resolved_addon_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "!! could not parse X.Y.Z addon version from $ADDON/addon.xml" >&2
  exit 1
fi
ADDON_VERSION=$_resolved_addon_version

PLATFORMS=(linux_x64 linux_armv7 linux_arm64 linux_x86 android_arm64 windows_x64 darwin_arm64 darwin_x64)

declare -A ASSET=(
  [linux_x64]="linux-amd64"
  [linux_x86]="linux-386"
  [linux_arm64]="linux-arm64"
  [linux_armv7]="linux-armv7-glibc"
  [linux_armv6]="linux-armv6"
  [android_arm64]="android-arm64"
  [windows_x64]="windows-amd64"
  [windows_x86]="windows-386"
  [darwin_x64]="darwin-amd64"
  [darwin_arm64]="darwin-arm64"
)

# GitHub release API assets[].digest values for the pinned sing-box release.
# A version override is accepted only after its complete asset set is pinned here.
declare -A SINGBOX_SHA256=(
  ["1.13.14|linux-amd64.tar.gz"]="f48703461a15476951ac4967cdad339d986f4b8096b4eb3ff0829a500502d697"
  ["1.13.14|linux-386.tar.gz"]="4d1c66260dfcb2120fde6c1c5ad125ce0f94769843c34aab4eef53c8d3bf3ae9"
  ["1.13.14|linux-arm64.tar.gz"]="4742df6a4314e8ecc41736849fca6d73b8f9e91b6e8b06ee794ff17ba180579e"
  ["1.13.14|linux-armv7-glibc.tar.gz"]="1d2338a20fcc92a0df0e68787f143e30ca767496eafc19b808ffcf68f3e40e58"
  ["1.13.14|android-arm64.tar.gz"]="59a4d18a4108e2f2a1bd49ca547829112712123975092d4a4bf1f443b6f3d747"
  ["1.13.14|windows-amd64.zip"]="f580782c6dd10f7691c66cea1d7c421813c5fbf7e305d1ee7ce0c3a40d196341"
  ["1.13.14|darwin-amd64.tar.gz"]="5245d645e847f90bb708da74bc020ae078c28489690756419685c04f56b4e3bb"
  ["1.13.14|darwin-arm64.tar.gz"]="73e8967b0fc08e17bce4263ca56ebc394822401a16497a1c4e02316c888202ab"
)

declare -A XRAY_ASSET=(
  [linux_x64]="Xray-linux-64.zip"
  [linux_x86]="Xray-linux-32.zip"
  [linux_arm64]="Xray-linux-arm64-v8a.zip"
  [linux_armv7]="Xray-linux-arm32-v7a.zip"
  [linux_armv6]="Xray-linux-arm32-v6.zip"
  [windows_x64]="Xray-windows-64.zip"
  [windows_x86]="Xray-windows-32.zip"
  [darwin_x64]="Xray-macos-64.zip"
  [darwin_arm64]="Xray-macos-arm64-v8a.zip"
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) [[ $# -ge 2 ]] || { echo "!! --version requires a value" >&2; exit 2; }; SINGBOX_VERSION="$2"; shift 2;;
    --addon-version) [[ $# -ge 2 ]] || { echo "!! --addon-version requires a value" >&2; exit 2; }; ADDON_VERSION="$2"; shift 2;;
    --print-version) PRINT_VERSION=1; shift;;
    *) SELECTED+=("$1"); shift;;
  esac
done
SELECTED=("${SELECTED[@]:-${PLATFORMS[@]}}")

if [[ ! "$ADDON_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "!! addon version must be X.Y.Z: $ADDON_VERSION" >&2
  exit 2
fi

if [[ "${PRINT_VERSION:-0}" == "1" ]]; then
  echo "$ADDON_VERSION"
  exit 0
fi

_singbox_asset_file() {
  local platform="$1" asset="${ASSET[$1]:-}"
  [[ -n "$asset" ]] || return 1
  if [[ "$platform" == windows* ]]; then
    printf '%s.zip\n' "$asset"
  else
    printf '%s.tar.gz\n' "$asset"
  fi
}

for platform in "${SELECTED[@]}"; do
  known=0
  for candidate in "${PLATFORMS[@]}"; do
    if [[ "$platform" == "$candidate" ]]; then
      known=1
      break
    fi
  done
  if [[ "$known" != "1" ]]; then
    echo "!! unknown platform: $platform" >&2
    exit 2
  fi
  asset_file="$(_singbox_asset_file "$platform")" || {
    echo "!! no sing-box asset for $platform" >&2
    exit 2
  }
  if [[ -z "${SINGBOX_SHA256["$SINGBOX_VERSION|$asset_file"]:-}" ]]; then
    echo "!! no pinned sing-box checksum for version $SINGBOX_VERSION asset $asset_file" >&2
    exit 2
  fi
done

mkdir -p "$DIST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

_extract_one() {
  # $1 archive, $2 binary filename, $3 dest dir
  local archive="$1" inner="$2" dir="$3"
  local -a found=()
  rm -rf "$TMP/x" || return 1
  mkdir -p "$TMP/x" || return 1
  if [[ "$archive" == *.zip ]]; then
    unzip -qo "$archive" -d "$TMP/x" || return 1
  else
    tar xzf "$archive" -C "$TMP/x" || return 1
  fi
  mapfile -d '' -t found < <(find "$TMP/x" -type f -name "$inner" -print0)
  [[ "${#found[@]}" -eq 1 ]] || {
    echo "!! expected exactly one regular $inner, found ${#found[@]}" >&2
    return 1
  }
  [[ -f "${found[0]}" && ! -L "${found[0]}" ]] || {
    echo "!! extracted $inner is not a regular file" >&2
    return 1
  }
  install -m 755 -- "${found[0]}" "$dir/$inner" || return 1
}

_verify_sha256() {
  local file="$1" expected="$2" label="$3" actual
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || {
    echo "!! invalid pinned checksum for $label" >&2
    return 1
  }
  actual="$(sha256sum "$file")" || return 1
  actual="${actual%% *}"
  if [[ "$actual" != "$expected" ]]; then
    echo "!! $label checksum mismatch" >&2
    return 1
  fi
}

_read_xray_checksum() {
  local digest_file="$1" line checksum="" matches=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^(SHA2-256|SHA256)=[[:space:]]*([0-9A-Fa-f]{64})[[:space:]]*$ ]]; then
      checksum="${BASH_REMATCH[2],,}"
      matches=$((matches + 1))
    fi
  done < "$digest_file"
  if [[ "$matches" -ne 1 ]]; then
    echo "!! expected exactly one Xray SHA256 digest, found $matches" >&2
    return 1
  fi
  printf '%s\n' "$checksum"
}

fetch_singbox() {
  local platform="$1" asset asset_file expected dir url archive inner
  asset="${ASSET[$platform]:-}"
  [[ -n "$asset" ]] || { echo "!! no sing-box asset for $platform"; return 1; }
  dir="$ADDON/resources/bin/$platform"
  mkdir -p "$dir" || return 1
  inner="sing-box"; [[ "$platform" == windows* ]] && inner="sing-box.exe"
  asset_file="$(_singbox_asset_file "$platform")" || return 1
  expected="${SINGBOX_SHA256["$SINGBOX_VERSION|$asset_file"]:-}"
  [[ -n "$expected" ]] || {
    echo "!! no pinned sing-box checksum for version $SINGBOX_VERSION asset $asset_file" >&2
    return 1
  }
  if [[ "$platform" == windows* ]]; then
    url="https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-${asset}.zip"
    archive="$TMP/sb.zip"
  else
    url="https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-${asset}.tar.gz"
    archive="$TMP/sb.tar.gz"
  fi
  echo ">> sing-box $platform"
  curl -fsSL --retry 3 -o "$archive" "$url" || return 1
  _verify_sha256 "$archive" "$expected" "sing-box" || return 1
  _extract_one "$archive" "$inner" "$dir" || return 1
  echo "$SINGBOX_VERSION" > "$dir/version" || return 1
}

fetch_xray() {
  local platform="$1" asset dir url archive digest_file expected inner
  asset="${XRAY_ASSET[$platform]:-}"
  [[ -n "$asset" ]] || { echo "!! no xray asset for $platform (skip)"; return 0; }
  dir="$ADDON/resources/bin/$platform"
  mkdir -p "$dir" || return 1
  inner="xray"; [[ "$platform" == windows* ]] && inner="xray.exe"
  url="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/${asset}"
  archive="$TMP/xr.zip"
  digest_file="$TMP/xr.zip.dgst"
  echo ">> xray    $platform"
  curl -fsSL --retry 3 -o "$archive" "$url" || return 1
  curl -fsSL --retry 3 -o "$digest_file" "$url.dgst" || {
    echo "!! failed to download Xray checksum for $platform" >&2
    return 1
  }
  expected="$(_read_xray_checksum "$digest_file")" || return 1
  _verify_sha256 "$archive" "$expected" "Xray" || return 1
  _extract_one "$archive" "$inner" "$dir" || return 1
  echo "$XRAY_VERSION" > "$dir/xray_version" || return 1
}

_stage_notices() {
  # $1 platform: copy root LICENSE/THIRD_PARTY_NOTICES into the addon root
  # and engine licenses beside the binaries (unambiguous names) under
  # $TMP/stage, ready for the third zip pass in build_zip.
  local platform="$1" stage="$TMP/stage/$ADDON"
  local src_bin="$ADDON/resources/bin/$platform" stage_bin="resources/bin/$platform"
  rm -rf "$TMP/stage" || return 1
  mkdir -p "$stage/$stage_bin" || return 1
  for f in LICENSE THIRD_PARTY_NOTICES.md; do
    [[ -f "$f" ]] || { echo "!! missing root $f" >&2; return 1; }
    cp "$f" "$stage/" || return 1
  done
  if [[ -f "$src_bin/sing-box" || -f "$src_bin/sing-box.exe" ]]; then
    for f in LICENSE NOTICE; do
      [[ -f "$ADDON/resources/licenses/sing-box/$f" ]] || { echo "!! missing $ADDON/resources/licenses/sing-box/$f" >&2; return 1; }
      cp "$ADDON/resources/licenses/sing-box/$f" "$stage/$stage_bin/sing-box-$f" || return 1
    done
  fi
  if [[ -f "$src_bin/xray" || -f "$src_bin/xray.exe" ]]; then
    [[ -f "$ADDON/resources/licenses/xray/LICENSE" ]] || { echo "!! missing $ADDON/resources/licenses/xray/LICENSE" >&2; return 1; }
    cp "$ADDON/resources/licenses/xray/LICENSE" "$stage/$stage_bin/xray-LICENSE" || return 1
  fi
  if [[ "$ADDON_VERSION" != "$_resolved_addon_version" ]]; then
    sed -E "0,/(<addon[^>]*version=\")[^\"]+/s//\\1$ADDON_VERSION/" \
      "$ADDON/addon.xml" > "$stage/addon.xml" || return 1
  fi
}

build_zip() {
  local platform="$1" zipfile add=()
  zipfile="$DIST/${ADDON}-${ADDON_VERSION}.${platform}.zip"
  rm -f "$zipfile"
  (cd "$ADDON/.." && zip -qr "$OLDPWD/$zipfile" "$ADDON" \
      -x "$ADDON/resources/bin/*" \
      -x "*/__pycache__/*" -x "*.pyc" -x "*.pyo") || return 1
  (cd "$ADDON/.." && zip -qrg "$OLDPWD/$zipfile" "$ADDON/resources/bin/$platform") || return 1
  _stage_notices "$platform" || return 1
  add=("$ADDON/LICENSE" "$ADDON/THIRD_PARTY_NOTICES.md")
  [[ -f "$TMP/stage/$ADDON/resources/bin/$platform/sing-box-LICENSE" ]] \
    && add+=("$ADDON/resources/bin/$platform/sing-box-LICENSE")
  [[ -f "$TMP/stage/$ADDON/resources/bin/$platform/sing-box-NOTICE" ]] \
    && add+=("$ADDON/resources/bin/$platform/sing-box-NOTICE")
  [[ -f "$TMP/stage/$ADDON/resources/bin/$platform/xray-LICENSE" ]] \
    && add+=("$ADDON/resources/bin/$platform/xray-LICENSE")
  if [[ -f "$TMP/stage/$ADDON/addon.xml" ]]; then
    add+=("$ADDON/addon.xml")
  fi
  (cd "$TMP/stage" && zip -qrg "$OLDPWD/$zipfile" "${add[@]}") || return 1
  scripts/verify_zip.sh --repo "$PWD" \
    --singbox-version "$SINGBOX_VERSION" --xray-version "$XRAY_VERSION" \
    "$zipfile" "$platform" "$ADDON_VERSION" || return 1
  echo "   -> $zipfile ($(du -h "$zipfile" | cut -f1))"
}

for platform in "${SELECTED[@]}"; do
  # F2 fix: reject platform values not in the PLATFORMS allowlist before rm -rf
  _platform_ok=0
  for _p in "${PLATFORMS[@]}"; do [[ "$platform" == "$_p" ]] && { _platform_ok=1; break; }; done
  if [[ "$_platform_ok" != "1" ]]; then
    echo "!! unknown platform: $platform (must be one of: ${PLATFORMS[*]})" >&2
    exit 2
  fi
  rm -rf "$ADDON/resources/bin/$platform"
  if ! fetch_singbox "$platform"; then
    echo "!! failed to fetch sing-box for $platform" >&2
    exit 1
  fi
  if ! fetch_xray "$platform"; then
    echo "!! failed to fetch Xray for $platform" >&2
    exit 1
  fi
  if ! build_zip "$platform"; then
    rm -f "$DIST/${ADDON}-${ADDON_VERSION}.${platform}.zip"
    echo "!! failed to package or verify $platform" >&2
    exit 1
  fi
done

echo "done. artifacts in $DIST/"
