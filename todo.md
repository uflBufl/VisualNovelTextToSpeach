# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements in agent memory and completed-work history in Git, not here.

## P0 - Reduce OpenMOSS generation latency

- [ ] Run the qualified GPU/8 profile beside Reverse: 1999 and confirm game-time
      VRAM headroom and responsiveness before making it the accelerated default.
      Retain CPU/4 as the fallback; extra CPU workers did not improve warm renders
      consistently on the qualified host.
- [ ] Evaluate multiple native servers only on hosts with multiple GPUs or
      measured RAM/VRAM headroom. Keep one serialized server as the default.

## P1 - Qualify remaining Python and speech runtimes

- [ ] Qualify the Python 3.14 MOSS Delay candidate on Windows CUDA with its atomic
      Torch/TorchAudio/TorchCodec stack and a supported shared FFmpeg build.
      Require CUDA import, one checksum-bound reference-conditioned render,
      finite audio, clean shutdown and recorded RAM/VRAM/latency. CPU-only hosts
      must return a clear unsupported-backend result instead of loading the 8B
      model.
- [ ] Qualify Python 3.14 MOSS SoundEffect v2 on Linux CUDA with one fixed-seed
      render and clean shutdown. If the model stack fails under NumPy 2, retain
      Python 3.12 for this isolated runtime and record the exact incompatibility.
- [ ] After those real-render gates pass, make runtime preparation select only a
      qualified stack from detected hardware. Exercise absent runtime,
      interrupted download, retry and restart on clean macOS and Windows. Users
      must not choose Python or CUDA wheels manually.

## P1 - Complete distributable packages

- [ ] Make the macOS and Windows portable packages start and render with their
      recommended backend without uv, a checkout, environment variables or an
      existing model cache. Keep model/license consent separate from dependency
      setup and verify every downloaded runtime artifact by checksum.
- [ ] Complete a Developer ID signed/notarized macOS package and signed Windows
      executable. The Windows build must complete from an ordinary account
      without Developer Mode. Retain checksum-bound startup/render reports for
      both platforms. Add a release-promotion gate that accepts only the signed
      archive after every required Windows hardware profile passes the existing
      matrix validator without `--allow-unsigned`.

## P1 - Preserve diagnostics across crashes

- [ ] Persist the bounded, sanitized OpenMOSS native event log and reload it on
      the next launch so an exported support bundle can explain a crash or forced
      exit. Keep audio, dialogue text, local paths and unbounded logs excluded.

## P2 - Qualify the real desktop experience

- [ ] On clean macOS and Windows installs, auto-detect the game (with folder
      fallback), select a game narrator, preview and save it, restart, then verify
      that live fallback and offline preparation both retain the choice.
- [ ] Exercise the main player journeys on real displays: fullscreen and
      multi-monitor use, 100%/150%/200% scaling, VoiceOver/Windows Narrator,
      focus loss, rapid advancement and shutdown during each pipeline stage.
      Check Stories, Voices, Reading, setup and Settings for visible primary
      actions at small sizes, exact clipboard copying, keyboard navigation and
      contrast when switching system light/dark themes while the app is open.
- [ ] Run a 30-minute macOS and Windows soak covering CPU/GPU speech and animated
      scenes. Require no buzzing, underruns, stale speech or stale auto-advance;
      record hardware and timing evidence instead of relying on subjective status.
- [ ] Restore reliable macOS-native global controls for pause, skip, replay and
      emergency stop, or make the compact controls the explicit gameplay handoff
      until those shortcuts are available.

## P2 - Handle in-game choices without mode babysitting

- [ ] Detect a Reverse: 1999 choice/manual boundary outside the dialogue OCR
      region, pause auto advance without losing the story cursor, and resume
      ordinary reading after the player chooses. Do not require the player to
      predict a choice and toggle live reading manually.

## P2 - Optional model experiments

- [ ] On a CUDA host, compare MOSS Delay 8B with MOSS Local 4B on the existing
      checksum-bound 46-line corpus. Preserve group identities, WAV hashes,
      timing/RTF, quality signals and hardware/model provenance; do not repeat the
      completed Local-4B/XTTS comparison.
- [ ] Evaluate typed non-verbal events with official MOSS SoundEffect v2 using a
      small fixed corpus and multiple checksum-bound seeds. Require technical and
      blinded perceptual approval before adding a provider; unsupported effects
      remain explicit omissions.
