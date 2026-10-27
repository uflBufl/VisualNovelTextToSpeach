#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != Darwin ]]; then
    echo 'Build the macOS capture helper on macOS.' >&2
    exit 1
fi
project_root=$(cd "$(dirname "$0")/.." && pwd)
architecture=$(uname -m)
output_directory="$project_root/native/capture-worker/macos/build"
mkdir -p "$output_directory"
xcrun swiftc -parse-as-library -swift-version 5 -O \
    -target "$architecture-apple-macosx14.0" \
    -module-cache-path "$output_directory/module-cache" \
    "$project_root/native/capture-worker/macos/main.swift" \
    -o "$output_directory/vntts-capture-worker"
codesign --force --sign - "$output_directory/vntts-capture-worker"
