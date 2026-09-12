#!/usr/bin/env bash
# cli/hotkey/build.sh — release build of the pet-talk-hotkey daemon.
#
# Usage: build.sh <output-dir>
#
# Builds every Swift source in this directory with `swiftc -O` into
# <output-dir>/pet-talk-hotkey. No absolute paths: everything is resolved
# relative to this script's own location, so it works from any checkout.
#
# Per the fan rule, this is meant to run on a build host (`ssh air` /
# `ssh lenovo`), never on the primary MacBook.

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <output-dir>" >&2
    exit 1
fi

OUT_DIR="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_NAME="pet-talk-hotkey"

mkdir -p "$OUT_DIR"

echo "swiftc version:"
swiftc --version

SOURCES=("$SCRIPT_DIR"/*.swift)
echo "Building ${#SOURCES[@]} source file(s) -> $OUT_DIR/$BIN_NAME (release, -O)"

# Some build hosts end up with a swift-frontend patch version newer than the
# SDK's precompiled .swiftinterface (e.g. Command Line Tools updated the
# compiler but not the SDK bundle) — swiftc then refuses with "this SDK is
# not supported by the compiler". Try the default SDK first; on that specific
# failure, retry against each installed SDK under the active toolchain until
# one actually builds, rather than failing the whole release build.
#
# PET_TALK_BUILD_SDK skips straight to a known-good SDK path (set it once
# you've found one on a given host) instead of re-probing every time — each
# probe attempt on a slow host can cost several minutes of -O compile time.
BUILD_LOG="$(mktemp)"
trap 'rm -f "$BUILD_LOG"' EXIT

if [ -n "${PET_TALK_BUILD_SDK:-}" ]; then
    echo "Using PET_TALK_BUILD_SDK=$PET_TALK_BUILD_SDK"
    if swiftc -O -sdk "$PET_TALK_BUILD_SDK" -o "$OUT_DIR/$BIN_NAME" "${SOURCES[@]}" >"$BUILD_LOG" 2>&1; then
        cat "$BUILD_LOG"
        echo "Built: $OUT_DIR/$BIN_NAME"
        exit 0
    fi
    cat "$BUILD_LOG" >&2
    exit 1
fi

if swiftc -O -o "$OUT_DIR/$BIN_NAME" "${SOURCES[@]}" >"$BUILD_LOG" 2>&1; then
    cat "$BUILD_LOG"
    echo "Built: $OUT_DIR/$BIN_NAME"
    exit 0
fi

if ! grep -q "is not supported by the compiler" "$BUILD_LOG"; then
    cat "$BUILD_LOG" >&2
    exit 1
fi

echo "Default SDK is not supported by the installed swift-frontend; probing other installed SDKs..." >&2
DEFAULT_SDK="$(xcrun --sdk macosx --show-sdk-path)"
TOOLCHAIN_SDKS_DIR="$(dirname "$DEFAULT_SDK")"
FOUND=0
# Try numbered (non-default-symlink) SDKs, oldest first — the default SDK's
# real target (via readlink) is excluded since we already just tried it.
DEFAULT_SDK_REAL="$(cd "$DEFAULT_SDK" && pwd -P)"
for sdk in $(ls -d "$TOOLCHAIN_SDKS_DIR"/MacOSX*.sdk 2>/dev/null | sort -V); do
    [ -e "$sdk" ] || continue
    sdk_real="$(cd "$sdk" && pwd -P)"
    [ "$sdk_real" = "$DEFAULT_SDK_REAL" ] && continue
    echo "Trying SDK: $sdk" >&2
    if swiftc -O -sdk "$sdk" -o "$OUT_DIR/$BIN_NAME" "${SOURCES[@]}" >"$BUILD_LOG" 2>&1; then
        FOUND=1
        echo "Built with fallback SDK $sdk -> $OUT_DIR/$BIN_NAME"
        break
    fi
    echo "SDK $sdk failed too, trying next..." >&2
done

if [ "$FOUND" -ne 1 ]; then
    echo "No installed SDK could build this source (last attempt's output follows):" >&2
    cat "$BUILD_LOG" >&2
    exit 1
fi
