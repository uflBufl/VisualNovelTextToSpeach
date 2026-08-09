#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != "Darwin" ]]; then
    echo "The macOS bundle must be verified on macOS." >&2
    exit 1
fi

app_path=${1:-}
report_path=${2:-}
require_developer_id=${3:-false}
if [[ -z $app_path || -z $report_path ]]; then
    echo "Usage: $0 APP_PATH REPORT_PATH [REQUIRE_DEVELOPER_ID]" >&2
    exit 1
fi
if [[ ! -d $app_path ]]; then
    echo "Application bundle is missing: $app_path" >&2
    exit 1
fi

bundle_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app_path/Contents/Info.plist")
if [[ $bundle_id != "io.github.visualnoveltexttospeech.app" ]]; then
    echo "Unexpected bundle identifier: $bundle_id" >&2
    exit 1
fi

codesign --verify --all-architectures --deep --strict "$app_path"
if [[ $require_developer_id == "true" ]]; then
    if ! codesign --display --verbose=4 "$app_path" 2>&1 | grep -q 'Authority=Developer ID Application:'; then
        echo "The application is not signed with a Developer ID Application certificate." >&2
        exit 1
    fi
fi

while IFS= read -r -d '' candidate; do
    if ! file "$candidate" | grep -q 'Mach-O'; then
        continue
    fi
    if otool -L "$candidate" | grep -Eq '/opt/homebrew|/usr/local/Cellar'; then
        echo "Bundle contains a Homebrew library reference: $candidate" >&2
        otool -L "$candidate" >&2
        exit 1
    fi
done < <(find "$app_path" -type f -print0)

executable="$app_path/Contents/MacOS/VisualNovelTextToSpeech"
if [[ ! -x $executable ]]; then
    echo "Application executable is missing: $executable" >&2
    exit 1
fi
"$executable" \
    --package-self-test \
    --package-self-test-report "$report_path"
codesign --verify --all-architectures --deep --strict "$app_path"

echo "Verified macOS application: $app_path"
echo "Self-test report: $report_path"
