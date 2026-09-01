# Authoring model evaluation

VNTTS owns the generic model-comparison and blind-listening runtime in
`vntts.authoring`. Neither workflow imports an extractor, understands game
chapters or interprets game-specific line IDs.

## Shared corpus and typed rendering

`vntts-benchmark-models` accepts exactly one of:

- a strict `vntts.tts-benchmark-corpus` version 1 document with unique stable
  sample IDs, line IDs, exact UTF-8 text hashes and voice characters; or
- a public `vntts.voice-generation-queue` version 1 document, converted to a
  corpus with deterministic round-robin sampling over delivery/emotion labels.

Only queue records with `action=generate` are eligible. Queue IDs, line IDs,
text hashes and voice-character choices are copied as opaque shared fields; no
game arithmetic is reproduced.

The authoring corpus loader preserves exact text, including leading and
trailing whitespace, and rejects text-hash drift, duplicate IDs and missing
line identity. It does not reuse the live benchmark loader, whose whitespace
normalization is intentionally unsuitable for authoring identity.

Every configured model is evaluated over the same corpus through its typed
`render(SynthesisRequest)` API with `cache_policy=bypass`. The benchmark never
opens an audio output or uses a fake playback sink. Cancelled or limited renders
fail the model report rather than publishing partial audio. Successful reports
use the VNTTS-owned `vntts.voice-model-report` version 1 schema and retain WAV
SHA-256, PCM timing, sample rate, sample count, duration, peak, seed and
generation profile. Completion diagnostics must return the requested seed and
profile. Model IDs are converted to contained output/cache directory names;
dot paths, path escape and case-insensitive destination collisions are rejected.
The aggregate `vntts.voice-model-benchmark` version 1 document records the exact
corpus and per-model report paths; model selection remains a manual decision.
For any voice-cloning backend, every used manifest reference is captured once
into the staged output before model construction. All variants read those same
immutable copies. The aggregate retains the original path, published copy,
byte size and SHA-256 for each control plus a canonical inventory digest, and
each model report binds that same digest. A missing or unreadable reference
fails the whole comparison before a backend is created. Fake-only test variants
use an explicitly empty control inventory.

A model variant may set an explicit `voice` override. This changes only the
voice passed to synthesis: the corpus character, line identity, exact text,
seed and generation profile remain unchanged. The report preserves both the
corpus `character` and the effective `synthesis_voice`, and records the
top-level `voice_override`. This supports controlled narrator comparisons
without misrepresenting the shared sample as character dialogue.

Example model configuration:

```json
[
  {"model_id": "moss/stable", "backend": "moss-tts", "generation_profile": "stable"},
  {"model_id": "moss/expressive", "backend": "moss-tts", "generation_profile": "expressive"}
]
```

For a same-model narrator comparison, keep the model and profile identical and
vary only `voice`:

```json
[
  {"model_id": "narrator/centurion", "backend": "moss-tts", "generation_profile": "stable", "voice": "Centurion"},
  {"model_id": "narrator/paper-heron", "backend": "moss-tts", "generation_profile": "stable", "voice": "Paper Heron"}
]
```

Run the multi-model benchmark:

```sh
uv run vntts-benchmark-models \
  --corpus /path/to/corpus.json \
  --models /path/to/models.json \
  --manifest /path/to/voice-manifest.json \
  --output /path/to/model-benchmark
```

Use `--queue /path/to/queue.jsonl --sample-size 24` instead of `--corpus` to
build the shared corpus from a generation queue.

The benchmark treats the voice manifest as an owned input boundary. Reference
paths must remain inside its directory and may not traverse symlinks. Each
published `voice-controls` file is read once through a no-follow descriptor,
with inode and size/mtime checks before and after the read, so a path swap cannot
copy unrelated host data into benchmark output.

### Verified Centurion and Paper Heron narrator comparison

On 2026-08-17, the extractor-owned playable-voice index supplied one compact
Paper Heron reference from `hero3141_mainvoc.bnk`, media ID `354191196`. The
source WEM SHA-256 is
`ff74e74071734a622c6a779ab16843605489fff54c50a757c683365d5ff8033c`;
the decoded PCM16 mono reference SHA-256 is
`aa888913cc71dea8b1744c2eb017c17e3ebdb7a3e9c725e51799a8cb2700c007`.
It is 9.554 seconds long and passed the technical reference preflight with a
score of 100, no clipping, 0.195 silence ratio, no leading silence and 0.08
seconds trailing silence. Perceptual suitability remains a listening decision.

The first controlled comparison used narration line
`reverse1999:314601:6`, seed 0 and the stable profile. Centurion completed, but
Paper Heron correctly returned `limited`; no Paper Heron WAV was published and
the safety cap was not extended. A second comparison therefore used the shorter
real narration line `reverse1999:314601:69`, exact text SHA-256
`22efd1a9278e220da2ca6b525083e1e062c034ac4ccbadcea5bbc0c3580262b9`,
with the same model, profile and seed. Both variants completed as PCM16 mono at
48 kHz:

- Centurion: 238,080 samples / 4.96 seconds, WAV SHA-256
  `10b6e0ecb8ac5723b0e49d6d37d1130344dab859fb14ab3c2041a42093524ad1`.
- Paper Heron: 215,040 samples / 4.48 seconds, WAV SHA-256
  `b497187721bff59c7c08fa0fada7a00e3805ebdce2ea96189a46233eb038da39`.

The ignored local comparison lives under
`data/reverse1999-voices/narrator-comparisons/centurion-paper-heron/` and has a
one-trial blind session. Its session SHA-256 is
`83c4a5956162a46aa63c1bc5838188ab41b9ae7b16e5a47a36429185f5019c1f`.
Choosing the narrator remains a manual confidence and presentation gate; the
comparison does not authorize batch regeneration.

Review the prepared trial from the repository root with:

```sh
uv run --no-sync vntts-listen ui \
  --session data/reverse1999-voices/narrator-comparisons/centurion-paper-heron/listening/session.json
```

The trial was completed on 2026-08-17. After saving the one rating, the
integrity-checked report was produced with:

```sh
uv run --no-sync vntts-listen report \
  --session data/reverse1999-voices/narrator-comparisons/centurion-paper-heron/listening/session.json
```

The report SHA-256 is
`21a8fce3fbc4ebf40e0ae36e98e669ce9821fb3fc59ed028b0d0e9486bce2168`.
It ranked the old Paper Heron variant first in this single trial. Subsequent
direct reference and synthesis review found that candidate too slow and its
reference unsuitable. The final manual presentation decision therefore
overrides that narrow result and selects **Centurion** as Narrator. Preserve the
report as historical evidence; do not rewrite its rating to manufacture
agreement with the later decision.

### Paper Heron reference and duration-control follow-up

A later controlled probe used another official Paper Heron playable line,
Chitchat II from `hero3141_mainvoc.bnk`, media ID `856018807`. The decoded
PCM16 mono reference is 20.552 seconds at 24 kHz with SHA-256
`9fe2e799426feead3383d771e14189e2a643be48aca03df300b163266b0748b5`.
Listening found Chinese speech in the reference, so it is rejected as a
production English cloning prompt even though its PCM structure is valid.

The probe rendered `Her eyes are wide as saucers.` with the int8 Local v1.5
model, stable profile, seed 0, explicit English language, and otherwise equal
inputs:

- natural model duration, with token-level duration control disabled:
  `complete`, 115,200 samples at 48 kHz / 2.4 seconds, SHA-256
  `1db50986c7cf56f91149a5805e63f9d241f788605326d900423db3e0894be646`;
- forced 35 audio tokens: `complete`, 134,400 samples at 48 kHz / 2.8 seconds,
  SHA-256
  `a933fbb96f4462d42c918749e1fea55eae460f698ab64fbbeea1fc3c007814b6`.

The listening decision preferred the uncontrolled render because it did not
insert the objectionable mid-phrase pause. This single pair does not prove that
duration control always creates pauses; it establishes that it should remain
off for ordinary Paper Heron narration and be evaluated only when an explicit
duration target is required. It also does not approve the contaminated
reference.

The accepted replacement is the English-only official line from
`hero3141_mainvoc.bnk`, media ID `357643769`. Its source WEM SHA-256 is
`8352cc19f63bf4fa4f926a007081112051c144d080e32dea5fb64d0881614c2f`;
the decoded PCM16 mono 24 kHz WAV is 8.088 seconds with SHA-256
`2bec2a8484749976a45d49516aa98f0ad30159beacda72585f675a005ec7ab8b`.
Objective analysis found no clipping, no leading silence, 0.06 seconds trailing
silence, 30.39% silent frames and a longest internal pause of 0.96 seconds. The
manual review accepted those natural pauses as good enough and found no
language contamination.

With otherwise identical stable-profile, seed-0 inputs and token-level duration
control disabled, `Her eyes are wide as saucers.` completed at 119,040 samples /
2.48 seconds with SHA-256
`52abaa87c71913bf88dc2dd80da03d83726c8a89edc968ebda20ae95dc08c09e`.
The generated result had no detected silent frames or internal pause. This is
the production Paper Heron reference and ordinary rendering policy; Paper Heron
remains a character voice, while Centurion is the selected Narrator.

## Blind listening and report semantics

The listening domain and its presentation adapters have a one-way dependency
boundary. `authoring.listening` owns session/key/report validation and mutation;
`authoring.listening_ui` depends on that core; and `authoring.listening_cli`
adapts the core plus the optional Qt launcher. The `vntts-listen` entry point is
bound to the CLI adapter, so importing the core never imports PySide6 or a
presentation module. An AST import-graph regression prevents the former
`listening`/`listening_ui` cycle from returning. The old `listening:main`
symbol remains as a lazy compatibility bridge for already-installed entry-point
scripts; new installs bind directly to `listening_cli:main`.

Start a session directly from the aggregate benchmark:

```sh
uv run vntts-listen start \
  --benchmark /path/to/model-benchmark/benchmark.json \
  --output /path/to/listening-session \
  --seed 42
```

Alternatively, start from two or more explicitly selected per-model reports:

```sh
uv run vntts-listen start-reports \
  --reports /path/to/model-a/report.json /path/to/model-b/report.json \
  --output /path/to/listening-session \
  --seed 42
```

Use repeated `--sample-id EXACT_ID` arguments to publish only a small
preselected finalist set. Non-complete model outcomes remain in the technical
benchmark report but are never turned into blind audio trials; an explicitly
selected ID fails closed unless at least two reports contain complete,
checksum-valid audio for it.

The single-backend `vntts-benchmark-tts` command publishes the strict
`vntts.tts-benchmark-report` version 1 adapter schema with stable line/text
identity and WAV hashes, so its reports can use the same `start-reports`
boundary. Historical schema-less reports are intentionally rejected and must
be regenerated.

### Rhiannon MOSS 4B timing checkpoint, 2026-09-01

The checked-in three-line Rhiannon replay corpus uses the strict benchmark
schema; its SHA-256 is
`a5ae711d69e1497cb4e2890921b676c3c2d8dd00e08936e32d80e765533ab095`.
Its first line, `I, erhm ...`, declares available original-game audio and is not
a MOSS generation target. Forcing it through MOSS correctly failed closed at the
three-second missed-EOS guard and published no partial artifact. The two actual
generated-route lines have a separate strict timing corpus with SHA-256
`fb78e5f08242c985f2dd91d25e1e51f0c62af7a3828eef431e798fa18f0c0c3e`.

The deterministic stable-profile seed-0 run with
`shraey/MOSS-TTS-Local-Transformer-v1.5-MLX-int8` is retained in the application
data directory under
`benchmarks/tts/rhiannon-moss-4b-stable-generated-seed0-20260901`. Its report
SHA-256 is
`af073f9a01043c94e604bfc0e3e97bc81ef50f3cc73621076a78f9964faaa395`.
Startup took 2520 ms. Fresh first PCM was 1287 ms and 670 ms, realtime factors
were 1.16 and 1.00, and memory-cache retrieval took 13.3 ms and 13.1 ms. The WAV
hashes are `387bb7336866266793d258a9375a2cc714d03ec68d85673c3ba40e524f6b5497`
and `039a35c9a5c6a17c988878d4a31e7d29d837871b2a7fc5c227930c4ecaf52538`.
The benchmark report now records its exact seed, and MOSS applies the same
short trailing-ellipsis normalization at its live, offline and benchmark
boundary. These remain renderer timings: they provide no audio-device underrun
evidence and do not measure the original-game playback route.

The engine NFKC-normalizes text, normalizes the Unicode ellipsis to three ASCII
dots and collapses whitespace before matching the same stable sample across
models. It creates every model pair that shares a sample, shuffles trial order
and A/B orientation deterministically from the seed, then hardlinks or copies
neutral relative WAV aliases. Every report source and copied alias is probed as
a supported WAV and bound by SHA-256. Model identities, source paths and A/B
assignment stay only in `.blind-key.json`; it is created atomically with mode
0600, and every resume operation rejects a changed mode. The public session
never names a model.

Runtime commands are:

```sh
uv run vntts-listen status --session /path/to/session.json
uv run vntts-listen next --session /path/to/session.json
uv run vntts-listen score trial-0001 --session /path/to/session.json --preference a
uv run vntts-listen report --session /path/to/session.json
uv run vntts-listen ui --session /path/to/session.json
```

The workbench keeps one anonymous trial visually dominant and shows
`Trial N of M`, completed and remaining counts, plus the exact reason decisions
are locked, ready or saving. Use `Ctrl+1` and `Ctrl+2` to play A and B,
`Ctrl+Space` to pause/continue/restart the active sample, and
`Ctrl+Shift+A`, `Ctrl+Shift+B`, `Ctrl+Shift+T` or `Ctrl+Shift+N` for A, B,
both acceptable/no preference, or neither acceptable. Decisions remain locked
until both anonymous samples have started. The four choices use a compact
two-row layout without exposing model identities.

Scoring is append-safe by default: rating an already completed trial requires
explicit `--overwrite`. Reports rank models by preference rate, then wins, then
model ID and include the same sorted pairwise totals as the legacy workflow.
Current reports must use the current schema and bind the exact current session
path. If a score is durably saved but report publication fails, CLI/UI surfaces
that persisted state explicitly and instructs the operator to regenerate the
derived report; it never claims that the rating was rolled back. The workbench
reloads that persisted session and advances to the next unrated trial (or the
completed state), preventing a second click on the already-saved rating.

`Neither acceptable` is a distinct verdict, not a tie. On disk it retains the
wire-v1-compatible `preference: tie` value and adds
`acceptability: neither`, so older readers can still open the session while
current reports count one rejection for each side and award neither a win nor a
tie. Use `No preference` only when both samples are acceptable and approximately
equal. The CLI equivalent is `--preference neither`; the Qt shortcut is
`Ctrl+Shift+N`.
The Qt workbench resumes the first serialized unrated trial, autoplays A then B,
keeps preference controls locked until both sides start, and provides pause,
restart, seek and five-second skip controls.

Preference and derived-report publication run outside the Qt thread. While the
exact trial snapshot is being committed, decision buttons are disabled with a
visible saving reason but playback remains available. Closing the dialog is
deferred until that authoritative operation reaches a terminal result. A
transient failure restores the current decision controls for an in-dialog retry;
if the preference was durable but report publication failed, the UI advances
from the persisted session and preserves the existing explicit report-recovery
message rather than offering a duplicate rating.

## Legacy-session compatibility

The runtime dual-reads VNTTS-owned version 1 session/key/report schemas and the
legacy `r1999.model-listening-*` version 1 schemas. Imported sessions use only
their copied relative aliases at runtime; stale absolute source-report and
assignment provenance is not required to resume.

Current sessions bind each alias hash in both the public trial and hidden
assignment. Imported legacy sessions, whose original schema had no alias hash,
verify aliases against the non-destructive import inventory; an original source
WAV is also compared when it still exists. Both PCM16 and the float32/stereo WAV
forms present in the preserved legacy benchmark are probed without conversion.

Loading, checking progress or opening a completed imported session does not
rerandomize trials, rewrite the hidden key or regenerate an already equivalent
report. `ensure_listening_report` compares report semantics while ignoring only
the legacy absolute session path and generation timestamp. A read-only gate on
2026-08-16 loaded the existing 45/45-trial, six-model session and its 90 aliases;
all 93 session, hidden-key, report and audio hashes remained unchanged.

The extractor commands remain available as transition shims until extractor
parity is independently verified. This VNTTS ownership block does not delete or
edit extractor code.
