# Visual Novel Text to Speech

Captures a visual novel dialog box, recognizes its text with Tesseract, and
reads it aloud with Pocket TTS or another configured speech engine.

## Requirements

- Python 3.11 (tested; newer versions are not yet verified)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html) with English language data
- [eSpeak-NG](https://github.com/espeak-ng/espeak-ng/blob/master/docs/guide.md)
- A working audio output device

Put the Tesseract executable in `PATH`, or configure its location in code:

```py
pytesseract.pytesseract.tesseract_cmd = r'<path-to-tesseract>'
```

Install the external tools on macOS with:

```sh
brew install tesseract espeak-ng
```

On Debian or Ubuntu:

```sh
sudo apt install tesseract-ocr espeak-ng libportaudio2
```

On Windows, follow the linked Tesseract and eSpeak-NG installation guides and
ensure Tesseract is in `PATH`. PortAudio is bundled with `sounddevice` on
macOS and Windows; other Linux distributions may need an equivalent PortAudio
package. See the [`sounddevice` installation guide](https://python-sounddevice.readthedocs.io/en/0.5.3/installation.html).
A [CUDA](https://developer.nvidia.com/cuda-downloads)-compatible GPU is
optional; TTS runs on the CPU when CUDA is unavailable.

## Platform support and permissions

The application targets Windows 11 first and also supports running from source
on macOS. Linux support is limited to X11.

- macOS: grant the terminal or packaged application access under **Privacy &
  Security -> [Screen & System Audio Recording](https://support.apple.com/guide/mac-help/mchld6aa7d23/mac)**,
  then restart it. Grant
  **[Accessibility](https://support.apple.com/guide/mac-help/mh43185/mac)** only
  when using auto advance. Screen-region and selected-window capture are
  supported. Global hotkeys are unavailable in the current macOS build; keep
  the control window or compact floating controls open and use their buttons.
- Windows: run the application in an interactive desktop session. No extra
  permission is normally required. Select the game under **Settings -> Capture
  source** and use borderless-windowed mode; minimized and exclusive-fullscreen
  windows cannot be captured reliably.
- Linux: run under an X11 desktop session with `DISPLAY` set. Screen-region and
  selected-window capture are supported. Native Wayland, headless, and SSH
  sessions are rejected because reliable global capture and hotkeys are not
  available.

## Run

```sh
uv sync --no-dev
uv run vntts-app
```

The application opens a compact control window and can optionally keep running
in the system tray. Its settings are stored in the current user's application-data
directory. Settings, game profiles, OCR corrections, and OCR review metadata
use versioned JSON documents with shared compatibility checks, damaged-file
fallback, and atomic publication. On first launch, complete the setup
wizard to select the game, verify OCR and audio, calibrate the dialogue area,
and run an OCR-to-speech test. Use **Manage models and voices** in the app to
download or verify the speech model and import local character voice references.
After the speech engine is ready, use **Choose voices** to compare candidates
with the same sample text and assign the preferred voice to the narrator or any
character. The character field is editable, so an OCR name that is missing from
the imported manifest can be mapped manually as soon as it appears.
On Windows, models and imported voice packs are stored under
`%LOCALAPPDATA%\VisualNovelTextToSpeech\`. Run the terminal interface for
development or recovery:

```sh
uv run vntts
```

On Windows and Linux X11, press `Ctrl+Shift+H` to capture and read the current
dialog. Set
`VNTTS_HOTKEY` using [pynput hotkey syntax](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys)
to choose another shortcut:

```sh
VNTTS_HOTKEY='<ctrl>+<alt>+r' uv run -m vntts
```

On those platforms, press `Ctrl+Shift+L` to start or stop live reading while
text is appearing.
Set `VNTTS_LIVE_HOTKEY` to change this shortcut. Live mode recognizes the
dialog every 200 ms and queues stable sentences or phrases without waiting for
the whole dialog to finish. When the profile prefers generated or original game
audio, live mode waits for one stable complete dialogue instead; those artifacts
are indexed by the exact full line and must never be replayed once per sentence.
One-time reads are ignored while live mode is on.

Use the control window to start live reading, read once, pause, skip, replay,
or emergency-stop speech. Select **Compact controls** to replace it with a
small always-on-top strip for borderless/fullscreen play; **Full** restores the
main window, and the selected view is used on the next launch. On Windows,
`Ctrl+Shift+P` pauses or resumes,
`Ctrl+Shift+S` skips, `Ctrl+Shift+R` repeats, and `Ctrl+Shift+X` clears the
queue. Shortcuts can be changed in Settings.

Auto advance starts only after playback completes and the current dialogue is
stable. Sending the configured key creates a pending transition; it is reported
as successful only after OCR observes a new dialogue generation or an empty
dialogue transition. VNTTS sends at most one automatic key per dialogue
generation. If the change is not confirmed within the bounded timeout, it keeps
watching for the next line but does not retry: a second key could skip a line
that appeared while OCR was still collecting the two frames needed to accept
it. The compact/full status asks the player to advance manually. While
confirmation is pending, VNTTS runs OCR again even when the dialogue fingerprint
is unchanged, so a static or visually similar next screen can still confirm the
transition. Paused live reading or a temporary focus loss postpones dispatch or
confirmation without consuming the one permitted key.

Reverse: 1999 choices are rendered outside the configured dialogue capture
region, so VNTTS does not attempt to detect them from dialogue OCR. Pause live
reading or disable auto advance manually when approaching a rare choice.

Original game audio requires a trustworthy completion signal before VNTTS may
auto advance. A game pack can declare
`"source_audio_completion": "duration-seconds"` in story-index metadata and
provide a positive `source_audio_duration_seconds` for an exact line; VNTTS
then waits for that interval before the line is considered complete. Legacy
`display_seconds` is not treated as audio duration. When auto advance is
enabled and no completion value is available, that source route is rejected
before playback and the line falls through to generated or live speech. With
auto advance disabled, the source may pass through as unobserved: VNTTS seals
the exact OCR line to suppress duplicate suffixes, but does not treat the pass
through as evidence that game audio completed. A declared duration is a
conservative delay measured from route acceptance, not observation of the
game's audio device.

Each game profile has an explicit **Audio source policy** in Settings:

A manual named-character assignment wins before this policy. A saved Narrator
choice selects the live fallback voice without bypassing source or generated
Narrator tracks. The Narrator dialog has a separate force-live checkbox for the
deliberate override case.

- **Live TTS only** always uses the selected speech engine. This is the default,
  including for profiles created before the setting was introduced.
- **Prefer generated audio** uses a verified generated-audio manifest entry for
  an exact or uniquely normalized story-line match, then falls back to the
  selected live engine.
- **Prefer original game audio** first lets an exact or uniquely normalized
  installed source-audio line play in the game when any completion required by
  auto advance is available, then tries verified generated audio, then live
  synthesis.

Loading a story index does not change this policy. The full Dashboard shows the
configured engine and policy, whether the generated-audio manifest is available,
and the effective source of the latest line. For MOSS this source distinguishes
fresh generation, memory cache, and persistent cache. Each prepared live line
also writes one generation-scoped record to the runtime support log without
replacing the player-facing status. The record keeps the effective source,
stable line ID, exact/ambiguous story match, fallback reason, selected voice
reference identifier, and artifact preflight state together. In addition,
`generation-timelines.json` keeps one bounded timeline per generation with
capture, OCR, stable-text, route, voice, generation, first-PCM, playback,
key-dispatch, and terminal next-dialogue confirmation or timeout timestamps.
Every per-chunk stage is keyed by its privacy-safe chunk ID, so two chunks in
one OCR generation remain distinct while duplicate reports for the same
stage/chunk merge.
The first two seconds without a stable replacement are a nonfatal waiting
state; confirmation remains active for ten seconds and never dispatches a
second key for the same generation. The same privacy-safe
timeline is included in exported support bundles; dialogue text and screenshots
are excluded. Source audio is
reported as declared available rather than checksum-verified; generated audio
is reported as verified only after its manifest, checksum, WAV, and metadata
checks pass.

Tracker-owned speech chunks also carry a generation-local ordinal and a
privacy-safe hash. Repeated preparation of the same chunk is suppressed, while
an appended OCR suffix receives a new ordinal. Multiple route events in one
generation therefore remain distinguishable without retaining dialogue text.
Story matching may ignore punctuation-only OCR drift when the normalized
speaker/text pair identifies one unambiguous indexed line. Speaker-name OCR is
corrected only for a unique high-confidence story or voice match.
For a verified generated-audio entry, a stable prefix of at least 20 normalized
characters may start the full indexed line before the typewriter animation
finishes, but only when that speaker/prefix identifies exactly one eligible
manifest entry. Short, ambiguous, or corrupted prefixes keep waiting for the
ordinary exact route. Verification reads one WAV byte snapshot, hashes those
exact bytes, and decodes the same bytes. Prefix expansion reserves that verified
PCM and canonical line together; if the reservation later becomes invalid, the
expanded text cannot silently fall through to live TTS. After an exact generated
or original-audio route finishes, late OCR suffix chunks in that same dialogue
generation are suppressed; a real next dialogue receives a new generation and
remains eligible.
An idle OCR fragment that is still one unique prefix of a longer indexed line
is not spoken or auto-advanced. This protects typewriter pauses from turning
the first half of a line into a complete dialogue.

Use **Narrator voice** in the control window (or **Choose narrator voice...** in
the tray menu) to audition and assign the live fallback Narrator voice. With a
generated-first audio policy, verified pregenerated tracks keep priority and
the selected voice handles only live fallback. Enable the separate force-live
checkbox only when every Narrator line should bypass pregenerated tracks.

The optional **Speaker announcements** setting is disabled by default. When
enabled, the Narrator voice says the visible speaker name once before the first
spoken chunk after that name changes. The announcement is a separate typed live
route: it is never written into canonical story WAVs, never replaces the
dialogue route, and never creates its own auto-advance action. Repeated chunks
and consecutive lines by the same visible speaker are not announced again.
Declared original game-audio pass-through is not overlaid because that audio is
already playing when VNTTS observes the line. Exact `???` is announced as
Narrator rather than pronouncing punctuation.

When auto advance is enabled, original game audio is selected only if the story
index supplies completion duration. A source line without trustworthy timing
falls through to generated or live speech so unattended reading cannot pause
indefinitely. Pocket TTS buffers 250 ms of newly generated audio before its
first real-time write to reduce output underruns; cached audio remains available
immediately. Once playback has started, a transient OCR generation replacement
does not cut it short. Pause, Skip, Clear Queue, and Emergency Stop remain
explicit interruption controls.

A completed generated route or observed source-delay route seals its exact OCR
generation and may become auto-advance eligible. An unobserved source pass
through seals only duplicate OCR and blocks automatic dispatch. Live TTS remains
tracker-driven rather than being force-sealed by the router. Any interrupted or
failed generated/live/source outcome leaves the generation unsealed and blocks
auto advance until a new dialogue generation or explicit reader reset. Only complete live
renders enter the memory/persistent speech caches; source and generated routes
bypass those caches. The render-only benchmark measures generation/cache stages
without an audio device, so underrun and driver timing remain hardware gates.
Concrete backend payload methods and mutable `last_*` metrics are deprecated
compatibility only; current routing consumes call-bound typed outcomes. See
[typed live audio routing](docs/live-audio-routing.md) and
[device-independent rendering](docs/synthesis-rendering.md).

Tune live reading with `VNTTS_LIVE_INTERVAL_MS`,
`VNTTS_LIVE_STABILITY_FRAMES`, `VNTTS_LIVE_IDLE_FLUSH_MS`, and
`VNTTS_LIVE_MIN_CHUNK_CHARACTERS`. Invalid values use their defaults.
Low-confidence OCR is retried with alternate preprocessing and is not spoken.
Set its acceptance threshold in Settings or with
`VNTTS_OCR_MINIMUM_CONFIDENCE` from 0 to 100. Settings can also retain one copy
of each uncertain frame and its confidence metadata in the application-data
directory for OCR diagnostics.

Screenshots are stored in `logs/screenshots/` by default. Set
`VNTTS_SCREENSHOT_DIR` to use another directory:

```sh
VNTTS_SCREENSHOT_DIR='/path/to/screenshots' uv run vntts
```

Calibrate the dialog region after changing the game resolution or UI layout:

```sh
uv run vntts-calibrate
```

One-time screenshots are retained until manually deleted. Live-mode frames are
not stored. Generated files under `logs/` are ignored by Git.

Replay saved dialogue frames through the real live fingerprint, OCR, exact line
resolver, audio router, playback-completion, and auto-advance state machine:

```sh
uv run vntts-replay-live samples/live-replay-smoke.json
```

The corpus is a versioned JSON document. Each dialogue entry supplies one or
more saved frames, expected speaker/text, stable line ID, source-audio status,
and optional expected route. Frames may be full screenshots when the document
also supplies a normalized `dialog_region`; otherwise they are treated as
already-cropped dialogue images. The command writes a report next to the corpus
unless `--output` is provided and fails if any line is stale, duplicated,
skipped, misrouted, or does not advance.

For the device-free Rhiannon software matrix, run:

```sh
uv run vntts-replay-live \
  samples/rhiannon-live-replay-representative.json \
  --timeout 10
```

That corpus is explicitly representative, not a real-game capture or listening
test. Its saved project frames, generated manifest and WAV are checksum-bound;
declared frame observations deterministically drive the production live tracker
after capture/fingerprint. The report preserves the fixture kind, corpus and
generated-manifest digests, recognition-source provenance, and an exact
dialogue/frame ledger with relative path, digest, consumed and skipped counts.
It verifies incomplete-prefix waiting, one verified generated-prefix expansion,
exact game/generated/live route selection, PCM consumption, completion and
advance integrity. See
[live replay acceptance](docs/live-replay-acceptance.md) for the evidence
boundary and remaining manual/device gates.

Run the objective PCM reference preflight before using a cloning reference:

```sh
uv run vntts-check-reference \
  data/reverse1999-voices/references/rhiannon-game-01.wav \
  data/reverse1999-voices/references/rhiannon-game-02.wav \
  data/reverse1999-voices/references/rhiannon-game-03.wav \
  --output rhiannon-reference-preflight.json
```

This rejects invalid format, clipping, extreme silence, very low signal, and DC
offset. Speaker identity, music/background contamination, and pronunciation
still require listening; objective ranking does not silently reorder a
multi-reference voice. The MOSS router currently uses the first configured
reference, and its path/checksum remains part of prompt and audio cache identity.

Benchmark a versioned per-line MOSS corpus with distinct fresh, memory-cache,
and persistent-cache measurements:

```sh
uv run vntts-benchmark-tts \
  --backend moss-tts \
  --model '/local/path/to/moss-tts-local-v1.5-mlx-int8' \
  --manifest data/reverse1999-voices/manifest.json \
  --corpus samples/rhiannon-moss-benchmark.json
```

Pass `--model` for an offline, reproducible run; otherwise the backend's model
identifier may trigger a Hugging Face snapshot check. MOSS generation uses a
text-length-based token and audio-duration budget. If the model misses EOS, the
audio is stopped at that budget, marked `generation_limited` in the timeline
and benchmark, shown in the compact/full status, and not cached. This prevents
a short hesitation such as `I, erhm ...` from repeating for minutes while still
letting playback completion and auto advance finish normally.

The default MOSS streaming profile uses 4 first-chunk frames and a 0.25-second
interval. On the local Rhiannon probe this reduced fresh first PCM from about
1264 ms at 16/1.0 to 640 ms, while the run remained faster than realtime (RTF
0.88). Use
`--moss-first-chunk-frames` and `--moss-streaming-interval` with
`vntts-benchmark-tts` to repeat the device-independent render/cache grid on
another machine. The benchmark deliberately leaves underrun unknown because it
does not open a device; underrun, driver jitter, and stop latency require a real
audio-device soak.

For the faster English CPU engine, install its isolated runtime once, select
Chatterbox Nano in Settings, and restart the app:

```sh
uv sync --project backends/chatterbox-nano
```

The first Nano start downloads several gigabytes of model assets.

Pocket TTS is the default speech engine. Install its isolated runtime once and
restart the app:

```sh
uv sync --project backends/pocket-tts
```

Character voice cloning also requires accepting the model terms at
<https://huggingface.co/kyutai/pocket-tts> and authenticating once with
`uvx hf auth login`.

MOSS-TTS v1.5 is the high-quality streaming option for Apple Silicon Macs.
Install its isolated MLX runtime once, choose MOSS-TTS in Settings, select a
narrator reference recording, and restart the app:

```sh
uv sync --project backends/moss-tts
```

The default int8 model and audio tokenizer download about 6.8 GB on first use.
VNTTS keeps the model loaded, caches each character reference, and streams
48 kHz stereo audio with a short initial buffer. Enable voice warm-up to move
the first MLX compilation cost to startup. Set `VNTTS_MOSS_MODEL` to use a
compatible local path or Hugging Face model. MOSS-TTS requires macOS on Apple
Silicon; Pocket TTS remains the portable default.

The runtime pins the official `mlx-audio` release. VNTTS carries a guarded
compatibility loader for its int8 MOSS audio tokenizer until the equivalent
quantized-weight handling is available upstream; it disables itself when
native support is detected.

Game-specific extraction lives in a separate repository. An extractor may
produce a versioned, checksum-bound `vntts.game-pack` that contains three local,
game-agnostic artifacts for VNTTS:

- a character voice `manifest.json`;
- a versioned `vntts.story-index` JSONL file for chapter detection and likely
  next-speaker preloading;
- an optional versioned `vntts.generated-audio` JSON manifest for verified
  ahead-of-time audio lookup.

Select the game-pack manifest in Settings, store it in a game profile, or set
`VNTTS_GAME_PACK`. VNTTS validates the complete pack and every declared SHA-256
before applying its resolved component paths. Individual artifact fields remain
available for unpackaged local workflows. See [the game-pack import
contract](docs/game-packs.md) for the public API and read-only preflight command.

VNTTS does not decrypt game configuration, parse engine assets, inspect audio
banks, or distribute extracted game content. The Reverse: 1999 implementation
and its local-only story/voice workflow are in the sibling
`reverse1999-extractor` project.

Existing Reverse: 1999 pregeneration jobs, explicitly paired standalone
generation outputs, and blind-listening sessions can be inspected and imported
into VNTTS application data without executing extractor code or changing the
source files. Use the separate `vntts-pregenerate` command; see the
[legacy authoring import contract](docs/authoring-legacy-import.md) for its
validation, idempotency and collision rules. Generation and review remain
outside the primary player.

Generic multi-model rendering benchmarks and blind A/B review are owned by the
authoring package through `vntts-benchmark-models` and `vntts-listen`. They use
shared corpus/queue documents and typed device-independent rendering; imported
legacy listening sessions resume without extractor code. See the
[authoring model-evaluation contract](docs/authoring-model-evaluation.md).

Generic generation-queue planning is also owned by `vntts.authoring`. The
`vntts-pregenerate preflight-queue` and `build-queue` commands consume only the
public, collection-aware story-index document and voice-manifest contracts.
They apply an explicit canonical source-audio policy and report missing voice
references before generation. See the
[collection-driven queue contract](docs/authoring-generation-queues.md).

Queue planning preserves source-owned delivery metadata by default. Authors can
explicitly opt into the deterministic `legacy-english-heuristic-v1` overlay for
otherwise unannotated English records; policy provenance is separate from
producer extensions and queue identity remains unchanged. See
[delivery-annotation authoring](docs/authoring-delivery-annotations.md).

The same authoring entry point now resumes typed, device-independent bulk
generation and owns generated-WAV validation, cumulative attempts/seeds,
approval/rejection and approved-only manifest publication. It dual-reads
preserved legacy state while treating state, not a possibly stale manifest, as
the review authority. See the
[resumable bulk-generation contract](docs/authoring-bulk-generation.md).
Missing character references remain blocked by default. A new
config-addressed workspace can explicitly authorize exact roles with repeated
`--narrator-fallback-role` flags, or deliberately authorize every unresolved
named role with `--narrator-fallback-all`. State and approved manifests retain
the source role, effective Narrator role, selected narrator character and
versioned policy rather than claiming the fallback is the original speaker.
See the [workspace contract](docs/authoring-workspaces.md) for the typed API and
review behavior.

Exact MOSS failures that exhaust their bounded attempts can move to a distinct
Pocket TTS workspace with `--carry-forward-from` and repeated
`--offline-fallback-failed` IDs. The source workspace is never mutated;
source-provider attempts, references and failure hashes remain bound while
Pocket starts its own provider-local seed sequence and uses the same output and
manual-review gates.

If an exact line still cannot produce acceptable offline audio after source
audit and bounded Pocket generation, `vntts-pregenerate live-fallback` can make
that outcome explicitly terminal without pretending a WAV exists. Final packs
carry the exact decision in generated-audio metadata, and runtime permits only
the bound Pocket model/profile for that line after source and approved generated
audio have been considered. Raw failures and pending review never imply this
authorization.

Reference changes are explicit too: `reference-report` checksum-binds objective
candidate metrics, and `select-reference` publishes a no-overwrite manifest
whose selected-first ordering is revalidated when a workspace copies the WAVs.
Perceptual choice remains manual. See
[immutable reference selection](docs/authoring-reference-selection.md).

Extractor-owned Character Story decisions enter VNTTS through a separate
variant-aware boundary:

```bash
uv run vntts-pregenerate import-reference-review \
  --report CANDIDATES/report.json \
  --review CANDIDATES/review.json \
  --story-index STORY/story-index.jsonl \
  --output AUTHORING/source-reference-plan
```

The plan copies accepted WAVs, keeps character/portrait/bank clusters separate,
maps each cluster to exact story-derived queue IDs and includes a fixed
evaluation corpus. It does not edit a manifest or authorize generation. See
[variant-aware source-reference import](docs/source-reference-review-import.md).
The same workflow publishes non-overwriting fixed-corpus inputs with
`vntts-pregenerate build-reference-evaluation`; rendering output is kept in a
separate mutable directory until blind source/result review is complete.
Checksum-valid results can be converted to strict reports with
`vntts-pregenerate build-reference-listening-reports` and opened through the
existing `vntts-listen` blind workbench. After manual review,
`vntts-pregenerate build-reference-bindings` publishes a new self-contained
partial manifest that maps only explicitly chosen variants to their exact queue
IDs. Generation state and final packs preserve and verify that mapping; no
existing manifest or workspace is rewritten.

After every selected queue item has a terminal approval, rejection or explicit
live-fallback decision, publish a portable final delivery with
`vntts-pregenerate publish-pack`. Publication
copies only bound inputs and approved WAVs into sibling staging, validates the
complete shared game-pack contract, and commits with atomic no-replace rename;
the queue, generated WAVs and review state remain unchanged in application
data. See [final game-pack publication](docs/authoring-game-pack-publication.md).

Use XTTS with character-specific voices and a default narrator voice:

```sh
VNTTS_TTS_MODEL='tts_models/multilingual/multi-dataset/xtts_v2' \
VNTTS_TTS_LANGUAGE='en' \
VNTTS_TTS_PROFILE='stable' \
VNTTS_GAME_PACK='/path/to/game-pack.json' \
VNTTS_AUDIO_SOURCE_POLICY='live-tts-only' \
VNTTS_NARRATOR_SPEAKER='Claribel Dervla' \
uv run vntts
```

Each character voice uses several varied references and is cloned and cached
when that speaker is first detected. XTTS uses `stable` by default. Select
`stable`, `natural`, or `expressive` with `VNTTS_TTS_PROFILE`; invalid profile
names also use `stable`. Unknown speakers use the narrator voice until mapped.
Imported recordings and user manifests are stored under application data.
Manual assignments are stored in the active game profile and take effect
immediately. Pocket TTS exposes its built-in voice catalog in the chooser; XTTS
exposes the speakers reported by the loaded model; imported character voices are
available with every cloning backend. An explicit manual assignment also uses
live synthesis instead of pre-recorded story audio, so the selected voice is
honored consistently. Under either preference policy, generated audio is used
only for an exact story line whose stable line ID and current UTF-8 text SHA-256
match the manifest. Missing, modified, partial, ambiguous, or speed-incompatible
entries follow the selected policy's next fallback route.

## Project layout

- `vntts/` - application code
- `tests/` - automated unit tests
- `examples/` - interactive GUI, screenshot, and voice demos
- `exps/` - OCR experimentation notebooks
- `samples/` - sample images and audio

## Development

Add a dependency:

```sh
uv add <name>
```

Synchronize or refresh dependencies:

```sh
uv sync --group dev
uv lock --refresh
```

uv creates the virtual environment in `.venv/`. Activation is optional:

```sh
# macOS and Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

Format and automatically fix lint findings:

```sh
uv run ruff check --fix .
uv run ruff format .
```

Verify formatting, linting, and tests:

```sh
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests
```

Run an interactive example:

```sh
uv run python examples/gui_demo.py
uv run python examples/screenshot_demo.py
uv run python examples/voice_demo.py
uv run python examples/voice_clone_demo.py
```

Run the OCR notebooks from `exps/`:

```sh
uv sync --no-dev --group notebook
uv run --no-dev --group notebook jupyter lab
```
