# Release speech runtime policy

This document records the packaging boundary for the default Pocket TTS runtime.
It is an engineering policy, not legal advice. Recheck the pinned upstream
revision and every redistributed artifact before each release.

## Upstream terms are artifact-specific

- Pocket TTS source code uses the MIT license. A package may redistribute the
  pinned code only with the required copyright and license notice.
- The `kyutai/pocket-tts` model repository identifies the model as CC BY 4.0,
  but access to its files is gated by acceptance of additional use conditions
  and sharing contact information. The project must not infer permission to
  redistribute gated files merely because a developer account can download
  them.
- `kyutai/tts-voices` is a mixed-provenance catalog. Its own inventory includes
  CC0, CC BY 4.0 and non-commercial sources, plus entries whose provenance needs
  individual review. A license conclusion for one voice never applies to the
  whole repository.

Primary sources:

- [Pocket TTS code license](https://github.com/kyutai-labs/pocket-tts/blob/main/LICENSE)
- [Pocket TTS model card and access conditions](https://huggingface.co/kyutai/pocket-tts)
- [Pocket TTS voice provenance](https://huggingface.co/kyutai/tts-voices)

## Packaging decision

Until a release owner explicitly approves redistribution of the exact pinned
model files, the distributable application must use a first-run download flow:

1. Explain that model access is gated and show the upstream terms before opening
   or starting the authenticated download.
2. Download into the application-owned model cache, never into the immutable app
   bundle, and retain the upstream repository, revision and file checksums.
3. Fail with a direct remediation when the model is unavailable. Do not advertise
   an offline bundled model that the package does not contain.
4. Package only allowlisted voices with exact per-file provenance. Include the
   required attribution for CC BY assets and exclude non-commercial or unclear
   assets from a generally distributed build.

If redistribution is later approved, retain the approval scope, exact model and
voice checksums, license texts, attribution and prohibited-use notice in the
release evidence. An approval for one revision does not automatically cover a
new upstream model or voice.

## Technical release gate

The runtime code and Python dependencies may be staged independently of model
weights. A release is complete only when a clean macOS and Windows package can:

- resolve its interpreter and Python modules from the extracted package;
- resolve model and voice files from the application-owned cache or an approved
  bundled inventory;
- initialize the effective default backend and render non-empty PCM;
- do so without `uv`, a source checkout, backend environment overrides or a
  pre-existing developer cache.

The package self-test must record interpreter, module, model and voice origins so
a developer machine cannot make a broken bundle appear healthy.
