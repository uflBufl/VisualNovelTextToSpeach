#!/usr/bin/env bash
set -euo pipefail

skip_tests=false
signing_identity=${VNTTS_CODESIGN_IDENTITY:-}
notary_profile=${VNTTS_NOTARY_KEYCHAIN_PROFILE:-}
target_arch=${VNTTS_MACOS_TARGET_ARCH:-$(uname -m)}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-tests)
            skip_tests=true
            shift
            ;;
        --signing-identity)
            signing_identity=$2
            shift 2
            ;;
        --notary-keychain-profile)
            notary_profile=$2
            shift 2
            ;;
        --target-arch)
            target_arch=$2
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [[ $(uname -s) != "Darwin" ]]; then
    echo "The macOS package must be built on macOS." >&2
    exit 1
fi
if [[ -n $notary_profile && -z $signing_identity ]]; then
    echo "Notarization requires a Developer ID signing identity." >&2
    exit 1
fi
if [[ $target_arch != "arm64" && $target_arch != "x86_64" ]]; then
    echo "Unsupported macOS architecture: $target_arch" >&2
    exit 1
fi

project_root=$(cd "$(dirname "$0")/.." && pwd)
tesseract_directory=${VNTTS_TESSERACT_DIR:-$(brew --prefix tesseract)}
espeak_directory=${VNTTS_ESPEAK_DIR:-$(brew --prefix espeak-ng)}
for required_path in \
    "$tesseract_directory/bin/tesseract" \
    "$tesseract_directory/share/tessdata/eng.traineddata" \
    "$espeak_directory/bin/espeak-ng" \
    "$espeak_directory/share/espeak-ng-data"; do
    if [[ ! -e $required_path ]]; then
        echo "Required macOS dependency is missing: $required_path" >&2
        exit 1
    fi
done

cd "$project_root"
uv sync --group dev --frozen
if [[ $skip_tests != "true" ]]; then
    uv run --frozen ruff format --check .
    uv run --frozen ruff check .
    uv run --frozen python -m unittest discover -s tests
fi

export VNTTS_TESSERACT_DIR="$tesseract_directory"
export VNTTS_ESPEAK_DIR="$espeak_directory"
export VNTTS_CODESIGN_IDENTITY="$signing_identity"
export VNTTS_MACOS_TARGET_ARCH="$target_arch"
export PYINSTALLER_STRICT_BUNDLE_CODESIGN_ERROR=1
export PYINSTALLER_VERIFY_BUNDLE_SIGNATURE=1

work_path="$project_root/build/macos/pyinstaller"
dist_path="$project_root/dist/macos"
uv run --frozen pyinstaller --noconfirm --clean \
    --workpath "$work_path" \
    --distpath "$dist_path" \
    packaging/macos/vntts.spec

app_path="$dist_path/Visual Novel Text to Speech.app"
report_path="$project_root/dist/VisualNovelTextToSpeech-macos-$target_arch-self-test.json"
scripts/verify-macos-bundle.sh \
    "$app_path" \
    "$report_path" \
    "$([[ -n $signing_identity ]] && echo true || echo false)"

staging_directory=$(mktemp -d "${TMPDIR:-/tmp}/vntts-dmg.XXXXXX")
mount_directory=$(mktemp -d "${TMPDIR:-/tmp}/vntts-mount.XXXXXX")
mounted=false
cleanup() {
    if [[ $mounted == "true" ]]; then
        hdiutil detach "$mount_directory" -quiet || true
    fi
    rm -rf "$staging_directory" "$mount_directory"
}
trap cleanup EXIT

ditto "$app_path" "$staging_directory/Visual Novel Text to Speech.app"
ln -s /Applications "$staging_directory/Applications"
dmg_path="$project_root/dist/VisualNovelTextToSpeech-macos-$target_arch.dmg"
hdiutil create \
    -volname "Visual Novel Text to Speech" \
    -srcfolder "$staging_directory" \
    -format UDZO \
    -ov \
    "$dmg_path"

if [[ -n $signing_identity ]]; then
    codesign --force --timestamp --sign "$signing_identity" "$dmg_path"
fi
if [[ -n $notary_profile ]]; then
    xcrun notarytool submit "$dmg_path" \
        --keychain-profile "$notary_profile" \
        --wait
    xcrun stapler staple "$dmg_path"
    xcrun stapler validate "$dmg_path"
fi

hdiutil attach "$dmg_path" \
    -readonly \
    -nobrowse \
    -mountpoint "$mount_directory" \
    -quiet
mounted=true
mounted_report="$project_root/build/macos/mounted-package-self-test.json"
scripts/verify-macos-bundle.sh \
    "$mount_directory/Visual Novel Text to Speech.app" \
    "$mounted_report" \
    "$([[ -n $signing_identity ]] && echo true || echo false)"
hdiutil detach "$mount_directory" -quiet
mounted=false

checksum_path="$dmg_path.sha256"
checksum=$(shasum -a 256 "$dmg_path" | awk '{print $1}')
printf '%s  %s\n' "$checksum" "$(basename "$dmg_path")" > "$checksum_path"

echo "macOS application: $app_path"
echo "macOS DMG: $dmg_path"
echo "SHA256 checksum: $checksum_path"
echo "Self-test report: $report_path"
