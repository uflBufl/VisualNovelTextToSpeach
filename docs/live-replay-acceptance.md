# Device-free live replay acceptance

`vntts-replay-live` exercises the production split capture/OCR state machine,
incremental dialogue tracker, exact story resolver, generated-audio verifier,
route preparation/playback outcome, completion seal and auto-advance state.
Capture frames advance only after OCR consumes that exact image; a dropped or
replaced capture therefore cannot silently skip a fixture state.

Corpus frame entries may remain a path string for real OCR replay, or use an
object with `path`, `sha256`, `observed_character` and `observed_text`. The object
form is a deterministic recognition fixture: bytes must match the declared
SHA-256, the production image fingerprint remains part of the key, and the
declared observation replaces OCR only after capture/fingerprint. Reports retain
every frame digest and consumed observation. This is useful for state/routing
regression, but is not evidence of OCR quality on those pixels.

An optional `generated_audio_manifest` is resolved relative to the corpus and
loaded through the production manifest/index/library validators. If it is
declared but missing or invalid, the run fails. Generated playback uses a
device-free output sink that consumes the exact decoded float32 PCM and reports
sample rate, sample count and PCM SHA-256. It deliberately reports no device
underrun claim.

The tracked `samples/rhiannon-live-replay-representative.json` covers:

- Rhiannon's short `I, erhm ...` through a declared original-game route;
- a Hotelier incomplete prefix which must wait for the exact complete line;
- Adar Llwch Gwin Fledgling through one checksum-verified generated entry and
  unique prefix expansion;
- Narrator through live synthesis fallback;
- exact observed dialogue order, route order, four completion-bound advances,
  no duplicate/stale/skipped dialogue, and generated PCM consumption.

The sample reuses saved project images and a checked-in WAV with explicit
fixture observations. It is representative software evidence only. The
following remain separate acceptance gates: a captured real-game Rhiannon corpus
with transient nameplate/background OCR noise, a 20-line gameplay run, voice and
pronunciation listening, reference contamination review, real audio-device
underrun/stop/prefill behavior, Windows capture/hotkeys and a long soak.

Narrator live override and `Use pregenerated narrator tracks when available`
restoration are controller/UI routing gates covered by focused tests. They do
not compare Centurion/Paper Heron perceptually. Exact generated-manifest lookup,
manual-override bypass, corrupt/missing/checksum/metadata fallback and reserved
prefix behavior are covered by generated-audio tests and the replay matrix.
