# Deprecated speech facade audit

The concrete speech backends still expose payload-oriented `prepare()`, `play()`,
`speak()` and mutable `last_*` values as a deprecated external compatibility
facade. The package is currently version `0.1.0`; there has been no major-release
boundary or documented external migration window. Repository search cannot
prove the absence of downstream Python callers. Removing the facade now would
therefore violate the published compatibility gate in `synthesis-rendering.md`.

The audit was repeated on 2026-08-31 across production and test Python callers for
`.prepare(`, `.play(` and the concrete mutable metric names. Controller live
routing uses `prepare_playback()`/`play_prepared()` or typed route decisions;
replay uses typed routes; benchmarks and authoring use `render()` results.
Generated-audio routing never had this facade. UI media-player `.play()` calls
and the lower-level XTTS audio engine are different APIs. Tests intentionally
exercise both typed behavior and the retained compatibility surface.

One internal MOSS warm-up call still used `prepare()` even though it needed the
typed request payload. It was migrated to `SynthesisRequest` plus the same
internal prepared-request path, without opening an output device or changing the
warm-up cache/generation limit. The remaining live preview and fallback helper
calls were migrated from `speak()` to `prepare_playback()` plus
`play_prepared()`. No application orchestration now consumes the deprecated
concrete-backend facade or reads its mutable fields for correctness.
`tests/test_speech_backend_api_boundaries.py` keeps the runtime protocol and
controller helpers on that typed boundary.

Removal remains blocked until a future major release has all of the following:

1. an announced migration window for external Python users;
2. a release-specific downstream usage/API audit beyond this repository;
3. migration documentation for `render()` and
   `prepare_playback()`/`play_prepared()`;
4. compatibility tests deleted or replaced in that same major change.

Until then the facade remains deliberately tested and is not dead code.
