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
set -ueo pipefail

cd "$(dirname "$0")"

ADDON="service.advancedproxy"
SINGBOX_VERSION="1.13.14"
XRAY_VERSION="25.8.3"
ADDON_VERSION="0.2.2"
DIST="dist"

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
    --version) SINGBOX_VERSION="$2"; shift 2;;
    --addon-version) ADDON_VERSION="$2"; shift 2;;
    *) SELECTED+=("$1"); shift;;
  esac
done
SELECTED=(${SELECTED[@]:-${PLATFORMS[@]}})

mkdir -p "$DIST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

_extract_one() {
  # $1 archive, $2 binary filename, $3 dest dir
  local archive="$1" inner="$2" dir="$3" found
  rm -rf "$TMP/x"; mkdir -p "$TMP/x"
  if [[ "$archive" == *.zip ]]; then
    unzip -qo "$archive" -d "$TMP/x"
  else
    tar xzf "$archive" -C "$TMP/x"
  fi
  found="$(find "$TMP/x" -name "$inner" | head -1)"
  [[ -n "$found" ]] || { echo "!! binary $inner not found"; return 1; }
  cp "$found" "$dir/$inner"
  chmod +x "$dir/$inner"
}

fetch_singbox() {
  local platform="$1" asset dir url archive inner
  asset="${ASSET[$platform]:-}"
  [[ -n "$asset" ]] || { echo "!! no sing-box asset for $platform"; return 1; }
  dir="$ADDON/resources/bin/$platform"
  mkdir -p "$dir"
  inner="sing-box"; [[ "$platform" == windows* ]] && inner="sing-box.exe"
  if [[ "$platform" == windows* ]]; then
    url="https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-${asset}.zip"
    archive="$TMP/sb.zip"
  else
    url="https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-${asset}.tar.gz"
    archive="$TMP/sb.tar.gz"
  fi
  echo ">> sing-box $platform"
  curl -fsSL --retry 3 -o "$archive" "$url" || return 1
  _extract_one "$archive" "$inner" "$dir" || return 1
  echo "$SINGBOX_VERSION" > "$dir/version"
}

fetch_xray() {
  local platform="$1" asset dir url archive inner
  asset="${XRAY_ASSET[$platform]:-}"
  [[ -n "$asset" ]] || { echo "!! no xray asset for $platform (skip)"; return 0; }
  dir="$ADDON/resources/bin/$platform"
  mkdir -p "$dir"
  inner="xray"; [[ "$platform" == windows* ]] && inner="xray.exe"
  url="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/${asset}"
  archive="$TMP/xr.zip"
  echo ">> xray    $platform"
  curl -fsSL --retry 3 -o "$archive" "$url" || return 0
  _extract_one "$archive" "$inner" "$dir" || return 0
  echo "$XRAY_VERSION" > "$dir/xray_version"
}

build_zip() {
  local platform="$1" zipfile
  zipfile="$DIST/${ADDON}-${ADDON_VERSION}.${platform}.zip"
  rm -f "$zipfile"
  (cd "$ADDON/.." && zip -qr "$OLDPWD/$zipfile" "$ADDON" \
      -x "$ADDON/resources/bin/*" \
      -x "*/__pycache__/*" -x "*.pyc" -x "*.pyo")
  (cd "$ADDON/.." && zip -qrg "$OLDPWD/$zipfile" "$ADDON/resources/bin/$platform")
  echo "   -> $zipfile ($(du -h "$zipfile" | cut -f1))"
}

for platform in "${SELECTED[@]}"; do
  fetch_singbox "$platform" || continue
  fetch_xray "$platform"
  build_zip "$platform"
done

echo "done. artifacts in $DIST/"
