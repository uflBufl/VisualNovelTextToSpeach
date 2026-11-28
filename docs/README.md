# Documentation

## Use VNTTS

| Document | Purpose |
| --- | --- |
| [Project README](../README.md) | First launch and entry points for players and contributors. |
| [Player guide](playing.md) | Platform permissions, calibration, Reading controls and Stories. |
| [Speech engines](speech-engines.md) | Live and offline engine setup, voice cloning and model overrides. |
| [CLI and diagnostics](diagnostics.md) | Readiness checks, support exports, terminal usage and native probes. |

## Develop, build and verify

| Document | Purpose |
| --- | --- |
| [Development and testing](development.md) | Source build, development tools and local or branch validation. |
| [Testing principles](testing-principles.md) | Choosing behavioral checks and maintaining the suite. |
| [Test runner internals](macos-test-execution.md) | Exact discovery and native Qt test isolation on macOS and Windows. |
| [Static typing](static-typing-ratchet.md) | Entry point to the maintained development check. |
| [Repository instructions](../AGENTS.md) | Local working rules and required checks. |
| [TODO](../todo.md) | Outstanding work and remaining verification. |
| [macOS package](../packaging/macos/README.md) | DMG build, signing and notarization. |
| [Windows package](../packaging/windows/README.md) | Portable build, signing and interactive compatibility matrix. |
| [Release speech runtime](release-speech-runtime.md) | Redistribution policy, runtime staging and clean-package verification. |
| [Native capture protocol](../native/capture-worker/PROTOCOL.md) | Worker IPC, frame ownership and error contract. |
| [Windows capture worker](../native/capture-worker/windows/README.md) | Build and qualify Windows Graphics Capture and the session recorder. |
| [macOS recording fixture](../scripts/mac_live_recording/README.md) | Native audio/video isolation and recording evidence. |
| [Native speech probes](../scripts/native/README.md) | OpenMOSS, Chatterbox and related native performance measurements. |
| [Qt screenshot tool](../scripts/pyside6-mcp/README.md) | Optional screenshot-safe PySide6 MCP build and usage. |

## Runtime and interfaces

| Document | Purpose |
| --- | --- |
| [Architecture](architecture.md) | Where capture, routing, rendering, authoring and artifact import meet. |
| [Reader experience](reader-experience.md) | Intended player journey and interaction requirements. |
| [Onboarding runtime](onboarding-runtime.md) | Setup, readiness and startup ownership. |
| [Desktop accessibility](desktop-accessibility.md) | Keyboard, focus and accessibility invariants. |
| [Controller architecture](controller-architecture.md) | Controller composition and runtime coordination. |
| [Live audio routing](live-audio-routing.md) | Original, prepared and synthesized audio decisions and completion. |
| [Synthesis rendering](synthesis-rendering.md) | Typed rendering, isolation, cancellation and caches. |
| [Live voice preflight](live-voice-preflight.md) | Voice readiness for selected reading paths. |
| [Live replay acceptance](live-replay-acceptance.md) | Replay commands, real-game evidence and verification limits. |
| [Sequence-first live reading](sequence-first-live-reading.md) | Exact sequence authority, guarded progression and qualification. |
| [Text-reading architecture](text-reading-architecture.md) | Shared observation design, implemented dialogue/caption scope and proposed document reading. |
| [Game packs](game-packs.md) | Pack validation, semantic evidence and runtime settings import. |

## Prepare and review audio

| Document | Purpose |
| --- | --- |
| [Self-service pregeneration](self-service-pregeneration.md) | Product and implementation rules for the player's Stories workflow. |
| [Generation queues](authoring-generation-queues.md) | Derive stable work items from story and voice inputs. |
| [Delivery annotations](authoring-delivery-annotations.md) | Apply opt-in authoring hints while preserving source metadata. |
| [Bulk generation](authoring-bulk-generation.md) | Resume generation, track attempts and resolve review state. |
| [Authoring workspaces](authoring-workspaces.md) | Configure, rebase, merge and inspect managed workspaces. |
| [Legacy import](authoring-legacy-import.md) | Migrate preserved jobs and listening sessions. |
| [Reference selection](authoring-reference-selection.md) | Publish an immutable selected-first reference manifest. |
| [Source reference review import](source-reference-review-import.md) | Convert reviewed source variants into exact generation bindings. |
| [Portrait expression aliases](portrait-expression-aliases.md) | Preserve exact portrait identity while recording accepted review aliases. |
| [Audio events](authoring-audio-events.md) | Parse, review and compose nonverbal events. |
| [Model evaluation](authoring-model-evaluation.md) | Compare renderers and review blind listening sessions. |
| [Speaker identity diagnostics](speaker-identity-diagnostics.md) | Compare speaker evidence and evaluate whether it is reliable. |
| [Voice quality gates](voice-quality-gates.md) | Reuse listening evidence for exact voice controls. |
| [Review-attention silence policy](review-attention-silence-policy.md) | Sample valid clips that warrant manual listening. |
| [Speech robustness corpus](speech-robustness-corpus.md) | Publish and validate human-labelled speech and optional ASR evidence. |
| [Authoring reconciliation](authoring-authority-reconciliation.md) | Produce a read-only report over explicitly selected workspace authority. |
| [Terminal conflict review](terminal-conflict-review.md) | Resolve contradictory decisions and carry forward exact outcomes. |
| [Render hypothesis review](render-hypothesis-review.md) | Review a single valid reference/result pair for later generation. |
| [Game-pack publication](authoring-game-pack-publication.md) | Publish reviewed, portable output from bound inputs. |
| [Authoring module boundaries](authoring-module-boundaries.md) | Keep wire contracts separate from orchestration and UI. |

## Research and historical records

Use these documents to understand a past decision, reproduce a bounded
experiment or find an evidence ID. Start with the guides and contracts above
for current behavior. Some evidence also requires local snapshots and
installed game data captured at the time.

| Document | Purpose |
| --- | --- |
| [Character Story completion](current-character-story-completion.md) | Character Story generation, review and pack activation history. |
| [Character Story census](character-story-authoring-census-2026-08-25.md) | Dated inventory of generation and review state. |
| [Missing reference audit](missing-character-reference-audit-2026-08-25.md) | Evidence for unresolved source roles in the dated census. |
| [Alternative reference comparison](alternative-reference-comparison-2026-08-25.md) | Dated render-only comparison and its limitations. |
| [Pregeneration coverage](pregeneration-coverage-plan.md) | Coverage policy and historical rollout checkpoints. |
| [Offline model comparison](offline-model-comparison.md) | Reusable benchmark recipe and evidence from the Local, Delay and XTTS comparison. |
| [Dobharchú repair comparison](dobharchu-repair-comparison.md) | Case study and experimental silence-comparison procedure. |
| [Deprecated speech facade audit](deprecated-speech-facade-audit.md) | Migration evidence for the typed rendering boundary. |
| [UI/UX baseline](ui-ux-audit.md) | Earlier review and the interaction decisions it recorded. |
| [UI/UX audit plan](ui-ux-review-plan.md) | Scope and method of the completed desktop audit. |
| [UI control audit](ui-ux-control-audit.md) | Findings and dispositions recorded at the time of the audit. |

The underlying UI investigation notes cover
[runtime setup](../.codex/investigations/ui-ux/runtime-setup.md),
[daily controls](../.codex/investigations/ui-ux/daily-tools.md),
[specialist tools](../.codex/investigations/ui-ux/specialist-tools.md) and
[cross-application integration](../.codex/investigations/ui-ux/integration.md).
