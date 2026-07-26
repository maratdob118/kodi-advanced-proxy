#!/bin/bash
# Build per-platform addon zips for Advanced Proxy (sing-box).
#
# Downloads the sing-box release binary for each requested platform into
# resources/bin/<platform>/, then zips the addon. Mirrors the Elementum
# per-platform zip approach: identical Python code + one binary per zip.
#
# Usage:
#   ./build.sh                 # build for all known platforms
#   ./build.sh linux_armv7     # build only one platform
#   ./build.sh --version 1.13.14 linux_x64
set -ueo pipefail

cd "$(dirname "$0")"

ADDON="service.advancedproxy"
SINGBOX_VERSION="1.13.14"
ADDON_VERSION="0.1.0"
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

fetch_binary() {
  local platform="$1" asset dir url archive inner
  asset="${ASSET[$platform]:-}"
  if [[ -z "$asset" ]]; then
    echo "!! no asset mapping for $platform, skipping"
    return 1
  fi
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

  echo ">> downloading $platform ($asset)"
  curl -fsSL --retry 3 -o "$archive" "$url"
  rm -rf "$TMP/x"; mkdir -p "$TMP/x"
  if [[ "$archive" == *.zip ]]; then
    unzip -qo "$archive" -d "$TMP/x"
  else
    tar xzf "$archive" -C "$TMP/x"
  fi
  local found
  found="$(find "$TMP/x" -name "$inner" | head -1)"
  [[ -n "$found" ]] || { echo "!! binary $inner not found in archive"; return 1; }
  cp "$found" "$dir/$inner"
  chmod +x "$dir/$inner"
  echo "$SINGBOX_VERSION" > "$dir/version"
}

build_zip() {
  local platform="$1" zipfile
  zipfile="$DIST/${ADDON}-${ADDON_VERSION}.${platform}.zip"
  rm -f "$zipfile"
  # include everything except other platforms' binaries and python caches
  (cd "$ADDON/.." && zip -qr "$OLDPWD/$zipfile" "$ADDON" \
      -x "$ADDON/resources/bin/*" \
      -x "*/__pycache__/*" -x "*.pyc" -x "*.pyo")
  (cd "$ADDON/.." && zip -qrg "$OLDPWD/$zipfile" "$ADDON/resources/bin/$platform")
  echo "   -> $zipfile ($(du -h "$zipfile" | cut -f1))"
}

for platform in "${SELECTED[@]}"; do
  fetch_binary "$platform" || continue
  build_zip "$platform"
done

echo "done. artifacts in $DIST/"
