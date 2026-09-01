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

Onboarding and Settings default Pocket to its public preset-only model and link
to the upstream access terms. A frozen Pocket worker strips inherited Hugging
Face tokens and disables implicit-token use unless the user explicitly enables
authenticated voice-cloning access. Enabling that option is restart-required and
permits only credentials already configured for the application process; the
project does not persist a token in its settings file. Source/development workers
retain their explicitly configured Hugging Face environment.

The package UI must offer only speech backends whose runtime is present. A
previously saved but unavailable backend may remain visible as a disabled item so
the state can be diagnosed, but it cannot pass validation or become the active
release configuration.

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

The frozen self-test performs a real Pocket render through the bundled isolated
worker. It creates a temporary Hugging Face cache below the application's data
directory, removes inherited token and cache overrides, requests the public
`alba` preset, and requires complete non-empty PCM. Its report inventories and
hashes the pinned public model, tokenizer and voice snapshot files. Any missing,
additional or revision-changed snapshot file fails the release gate. The
temporary model, voice-state and audio caches are removed after the probe.

`uv venv --relocatable` alone is not a self-contained runtime: the environment's
Python launcher can still refer to the developer's base Python installation.
Release staging therefore includes an adjacent uv-managed CPython distribution,
rewrites the POSIX environment launcher to a relative link, and rejects the
staging tree unless a copied-to-a-new-path probe imports every required module
from that copied tree. The same relocation probe is mandatory on Windows; a uv
or CPython change that breaks the portable launcher must fail the build rather
than ship a package that silently uses a machine-local Python.

Staging removes managed-CPython alias symlinks and its `include`, `share` and
`lib/pkgconfig` development trees because they are not needed to execute the
locked worker. The relocation/import probe runs after pruning, so a future
CPython layout that needs one of those files fails during staging rather than
producing a broken app.

Only the worker's `python` launcher and its managed interpreter are retained in
their executable directories. Installed CLI entry points and top-level hidden
package-manager staging metadata are not part of the runtime contract. On POSIX,
executable bits are also cleared from ordinary package and standard-library
data; Mach-O files are still individually signed, but scripts no longer appear
to macOS as unsigned nested code. The managed interpreter remains executable
and the post-prune relocation probe exercises it.

On macOS the staged runtime is intentionally not submitted to PyInstaller as a
`datas` tree. Embedded Python contains executable, binary, data and cross-tree
symlink entries; PyInstaller's BUNDLE reclassification otherwise splits it
between `Contents/Frameworks` and `Contents/Resources` and can make a parent path
both a directory and a symlink. The build copies the already relocation-tested
tree intact into `Contents/Resources/speech-runtimes`, adds the standard
`Contents/Frameworks` compatibility symlink used by PyInstaller's macOS layout,
signs every Mach-O file, and then reseals the outer app with the requested
identity. This keeps data out of the code-only Frameworks root while preserving
`sys._MEIPASS` lookup. Bundled Python probes and workers run with `-B`, so model
imports cannot add bytecode caches to the sealed application. The clean-bundle
verifier runs only after that injection and signature pass, and verifies the
seal again after inference.

## Verified unsigned macOS evidence

On 2026-08-31 the arm64 build completed both the direct-app and mounted-DMG
self-tests with an ad-hoc signature. Each clean-cache render produced 46,080
samples at 24 kHz and reported `complete`. The runtime resolved CPython 3.11.15
and all nine required modules from `Contents/Resources/speech-runtimes`; the
post-render strict code-signature check also passed.

The downloaded public preset inventory was exactly:

- model `be9c6b4876d3f30740a8225dfcaa2e43dc4aeb753c15272735bee16bbb4abb0a`;
- tokenizer `d461765ae179566678c93091c5fa6f2984c31bbe990bf1aa62d92c64d91bc3f6`;
- `alba` voice `69c32db63ca56843d994f81f343f62e0bf2d73f7e4c9bc73e44bb1110b1d8845`.

This evidence closes the unsigned arm64 packaging mechanics only. Developer ID
signing/notarization and the Windows portable/installer gates remain separate.

## Windows auto-advance qualification

The Windows compatibility fixture starts with the dialog `Compatibility capture
and speech are working.` and changes to `Auto advance acknowledged.` only after
its focused form receives Space, Enter, Right or Down. Until that transition it
keeps the fixture focused, so the release probe exercises the same focus guard
as ordinary live mode rather than bypassing it.

The installed application first captures, recognizes and speaks the initial
dialog. It then calls the production `AppController._auto_advance_dialog` path,
recaptures the selected window and requires confident OCR of the second dialog.
Dispatch without the visual acknowledgement fails the smoke test. Elevated-game
profiles launch both the fixture and installed smoke process elevated; normal
profiles launch both normally. This detects Windows cross-integrity input
failure instead of publishing successful OCR and legacy speech as false
auto-advance evidence.

Every Windows matrix report must now record matching smoke/game process levels,
`auto_advance_dispatched=true`, `auto_advance_acknowledged=true` and the exact
production controller name. The matrix validator rejects absent, legacy or
unacknowledged evidence. This implements the gate; real signed reports for every
GPU/display/DPI profile are still required before release qualification closes.
