#!/usr/bin/env bash
# Installs a pinned Luau release (luau, luau-analyze, luau-compile) into .bin/.
# Uses the prebuilt release on x86_64 Linux and macOS; builds from source
# elsewhere (e.g. arm64 Linux), which needs cmake and a C++17 compiler.
set -euo pipefail

LUAU_VERSION="${LUAU_VERSION:-0.740}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/.bin"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$BIN"

ASSET=""
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) ASSET="luau-ubuntu.zip" ;;
  Darwin-*) ASSET="luau-macos.zip" ;;
esac

if [[ -n "$ASSET" ]]; then
  curl -fsSL -o "$TMP/luau.zip" \
    "https://github.com/luau-lang/luau/releases/download/${LUAU_VERSION}/${ASSET}"
  unzip -o -q "$TMP/luau.zip" -d "$BIN"
else
  echo "No prebuilt Luau for $(uname -s)-$(uname -m); building ${LUAU_VERSION} from source…"
  curl -fsSL -o "$TMP/src.tar.gz" \
    "https://github.com/luau-lang/luau/archive/refs/tags/${LUAU_VERSION}.tar.gz"
  tar -xzf "$TMP/src.tar.gz" -C "$TMP"
  cmake -S "$TMP/luau-${LUAU_VERSION}" -B "$TMP/build" \
    -DCMAKE_BUILD_TYPE=Release -DLUAU_BUILD_TESTS=OFF >/dev/null
  cmake --build "$TMP/build" --parallel "${JOBS:-2}" \
    --target Luau.Repl.CLI Luau.Analyze.CLI Luau.Compile.CLI
  cp "$TMP/build/luau" "$TMP/build/luau-analyze" "$TMP/build/luau-compile" "$BIN/"
fi

chmod +x "$BIN"/luau*
echo "$LUAU_VERSION" > "$BIN/VERSION"
echo "Luau $LUAU_VERSION installed in $BIN"
