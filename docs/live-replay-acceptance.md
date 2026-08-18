# Device-free live replay acceptance

`vntts-replay-live` exercises the production split capture/OCR state machine,
incremental dialogue tracker, exact story resolver, generated-audio verifier,
route preparation/playback outcome, completion seal and auto-advance state.
Capture frames advance only after OCR consumes that exact image; a dropped or
replaced capture therefore cannot silently skip a fixture state. A route may
finish early, but its auto-advance callback waits until OCR has acknowledged
every declared frame for that dialogue. Final success additionally requires the
full corpus frame ledger to be consumed.

Corpus frame entries may remain a path string for real OCR replay, or use an
object with `path`, `sha256`, `observed_character` and `observed_text`. The object
form is a deterministic recognition fixture: bytes must match the declared
SHA-256, the production image fingerprint remains part of the key, and the
declared observation replaces OCR only after capture/fingerprint. Reports retain
the corpus digest, `fixture_kind`, every frame digest, and whether each consumed
observation came from OCR or a declared fixture value. An observation is invalid
without its exact frame digest. Serialized media paths are relative, contained
under the corpus directory and may not traverse symlinks. This is useful for
state/routing regression, but is not evidence of OCR quality on those pixels.
Each recognized event is mapped to its one-based dialogue/frame index, relative
path and digest. `frame_consumption` reports declared, consumed and skipped event
counts plus every frame's consumed state; skipped duplicate/stale recognitions
remain visible but cannot substitute for a missing declared frame.

An optional `generated_audio_manifest` object binds a contained relative `path`
and exact `sha256`. Every manifest audio path is independently contained and
bound by its declared WAV SHA-256. The runner copies the one verified manifest
and WAV byte snapshot into a private temporary bundle, then loads and decodes
that snapshot through the production manifest/index/library validators. A
manifest or WAV mutation after corpus loading fails instead of changing the
route. Generated playback uses a device-free output sink that consumes the exact
decoded float32 PCM and reports sample rate, sample count and PCM SHA-256. It
deliberately reports no device underrun claim.

The tracked `samples/rhiannon-live-replay-representative.json` covers:

- Rhiannon's short `I, erhm ...` through a declared original-game route;
- a Hotelier incomplete prefix which must wait for the exact complete line;
- Adar Llwch Gwin Fledgling through one checksum-verified generated entry and
  unique prefix expansion;
- Narrator through live synthesis fallback;
- exact observed dialogue order, route order, four completion-bound advances,
  complete per-frame consumption, no duplicate/stale/skipped dialogue, and
  generated PCM consumption.

The sample reuses saved project images and a checked-in WAV with explicit
fixture observations. It is representative software evidence only. The
following remain separate acceptance gates: a captured real-game Rhiannon corpus
with transient nameplate/background OCR noise, a 20-line gameplay run, voice and
pronunciation listening, reference contamination review, real audio-device
underrun/stop/prefill behavior, Windows capture/hotkeys and a long soak.

Narrator fallback selection and the separate force-live override are
controller/UI routing gates covered by focused tests. They do
not compare Centurion/Paper Heron perceptually. Exact generated-manifest lookup,
manual-override bypass, corrupt/missing/checksum/metadata fallback and reserved
prefix behavior are covered by generated-audio tests and the replay matrix.
