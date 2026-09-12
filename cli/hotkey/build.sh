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

swiftc -O \
    -o "$OUT_DIR/$BIN_NAME" \
    "${SOURCES[@]}"

echo "Built: $OUT_DIR/$BIN_NAME"
