# macOS package

Install `uv`, Tesseract, and eSpeak-NG, then build the application and DMG:

```sh
brew install tesseract espeak-ng
scripts/build-macos.sh
```

Artifacts and their SHA-256 checksum are written to `dist/`. The application
contains Python, Qt, Tesseract English data, eSpeak-NG data, and the runtime
Python packages.

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
