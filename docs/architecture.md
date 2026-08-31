# Architecture index

This page is the shortest path to the current design. Documents under
**Canonical contracts** describe behavior that production code must preserve.
Documents under **Operational evidence** record experiments, audits and one-off
story runs; they explain why decisions were made, but are not the source of
truth for current APIs.

## Canonical contracts

### Runtime

- [`onboarding-runtime.md`](onboarding-runtime.md) - startup, permissions and
  first-run behavior.
- [`live-audio-routing.md`](live-audio-routing.md) - source priority, speaker
  announcements, fallback routing and playback ownership.
- [`synthesis-rendering.md`](synthesis-rendering.md) - typed renderer protocol,
  backend isolation, limits and device-independent WAV production.
- [`live-voice-preflight.md`](live-voice-preflight.md) - voice readiness and the
  fail-closed gate before live reading.
- [`game-packs.md`](game-packs.md) - read-only runtime import and exact generated
  audio matching.

### Authoring

- [`self-service-pregeneration.md`](self-service-pregeneration.md) - ordinary
  player workflow for local extraction, minimal voice audition, automatic
  generation/quality routing and local pack activation.
- [`authoring-generation-queues.md`](authoring-generation-queues.md) - stable
  queue identity and collection-driven queue construction.
- [`authoring-workspaces.md`](authoring-workspaces.md) - resumable workspace
  authority, rebasing, merging and graphical workbench behavior.
- [`authoring-bulk-generation.md`](authoring-bulk-generation.md) - generation
  state, review lifecycle, leases and recovery.
- [`authoring-module-boundaries.md`](authoring-module-boundaries.md) - import
  direction and extracted leaf foundations.
- [`authoring-reference-selection.md`](authoring-reference-selection.md) and
  [`source-reference-review-import.md`](source-reference-review-import.md) -
  immutable reference decisions and provenance-preserving import.
- [`authoring-audio-events.md`](authoring-audio-events.md) - non-verbal event
  parsing, review and composition boundaries.

### Artifact authority and publication

- [`authoring-authority-reconciliation.md`](authoring-authority-reconciliation.md)
  - checksum-bound reconciliation and authoritative outcomes.
- [`terminal-conflict-review.md`](terminal-conflict-review.md) - human review of
  competing terminal decisions and safe carry-forward.
- [`authoring-game-pack-publication.md`](authoring-game-pack-publication.md) -
  final publication gates and atomic no-replace output.
- [`authoring-delivery-annotations.md`](authoring-delivery-annotations.md) -
  delivery overlays and provenance.
- [`authoring-legacy-import.md`](authoring-legacy-import.md) - non-destructive
  migration of preserved legacy artifacts.

### UI and accessibility

- [`ui-ux-audit.md`](ui-ux-audit.md) - shared interaction and decision-context
  contract across authoring interfaces.
- [`desktop-accessibility.md`](desktop-accessibility.md) - keyboard, focus and
  accessibility invariants.
- [`review-attention-silence-policy.md`](review-attention-silence-policy.md) -
  when generated audio deserves manual attention without turning natural pauses
  into false positives.

### Quality and verification

- [`voice-quality-gates.md`](voice-quality-gates.md) - reusable objective gates
  for reference and generated speech.
- [`speech-robustness-corpus.md`](speech-robustness-corpus.md) - reproducible
  robustness corpus and validation commands.
- [`macos-test-execution.md`](macos-test-execution.md) - exact macOS test
  sharding and duplicate-discovery handling.

## Operational evidence and histories

The following documents are retained as evidence and should not be read first
when implementing current behavior:

- dated censuses and audits, including
  [`character-story-authoring-census-2026-08-25.md`](character-story-authoring-census-2026-08-25.md),
  [`missing-character-reference-audit-2026-08-25.md`](missing-character-reference-audit-2026-08-25.md)
  and
  [`alternative-reference-comparison-2026-08-25.md`](alternative-reference-comparison-2026-08-25.md);
- the evolving Character Story execution ledger in
  [`current-character-story-completion.md`](current-character-story-completion.md);
- model and repair experiments in
  [`offline-model-comparison.md`](offline-model-comparison.md) and
  [`dobharchu-repair-comparison.md`](dobharchu-repair-comparison.md);
- the coverage rollout record in
  [`pregeneration-coverage-plan.md`](pregeneration-coverage-plan.md);
- migration/deprecation audits such as
  [`deprecated-speech-facade-audit.md`](deprecated-speech-facade-audit.md).

If an evidence document conflicts with a canonical contract, follow the
canonical contract and update the stale evidence note separately.
