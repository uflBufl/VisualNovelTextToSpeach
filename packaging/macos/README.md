# macOS package

On macOS 14 or newer, install Xcode command line tools (including Swift),
`uv`, Tesseract, and eSpeak-NG, then build the application and DMG:

```sh
xcode-select --install
brew install tesseract espeak-ng
scripts/build-macos.sh
```

Artifacts and their SHA-256 checksum are written to `dist/`. The application
contains Python, Qt, the ScreenCaptureKit capture helper, Tesseract English
data, eSpeak-NG data, and the runtime
Python packages. Build on the target architecture; cross-architecture
`--target-arch` is rejected because these native dependencies come from the
build host.

For distribution, import a Developer ID Application certificate and build with:

```sh
scripts/build-macos.sh \
  --signing-identity 'Developer ID Application: Example (TEAMID)' \
  --notary-keychain-profile vntts-notary
```

Create the notary profile beforehand with `xcrun notarytool store-credentials`.
Signed builds use hardened runtime, are submitted with `notarytool`, and have
the accepted tickets stapled to both the application and DMG. The manual
`macos-release.yml` workflow performs the same release with repository secrets
for the Developer ID certificate and App Store Connect notary credentials.

The builder compiles and self-tests the capture helper before PyInstaller
collects and signs it. The packaged self-test checks helper provenance and IPC
without requesting Screen Recording permission or capturing the desktop.
