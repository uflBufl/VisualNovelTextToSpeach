# Terminal authority conflict review

Parallel immutable authoring workspaces can retain contradictory terminal
decisions for the same queue record. A newer approval does not silently erase
an older rejection, and workspace age or a majority of copied outcomes is not
review authority.

`publish_terminal_conflict_review` converts one exact reconciliation report
into a self-contained review directory. Publication:

- binds the exact reconciliation payload and report ID;
- requires identical queue-record and text identity for an audio comparison;
- reloads every referenced workspace through the public workbench validator;
- captures exact state, queue, item and WAV authority;
- collapses occurrences only when terminal decision and WAV SHA-256 are both
  identical;
- copies every distinct PCM16 mono WAV into the immutable review directory;
- publishes without replacing an existing directory; and
- never changes source state, review decisions, manifests or WAVs.

Publication builds the display projection once per affected workspace. The
immutable bundle then retains each exact `ReviewAuthority`: queue SHA-256,
state SHA-256, state-item SHA-256 and WAV SHA-256 together with the canonical
state and queue paths. Decision-time revalidation uses those direct
compare-and-swap inputs instead of rebuilding the complete story projection.
This preserves source authority while keeping an individual background save
bounded by the small conflict set.

Run the operator interface with:

```bash
uv run --no-sync vntts-conflict-review REVIEW_DIRECTORY
```

The interface initially presents a deterministic blind A/B order derived from
the review/case/candidate identities rather than historical approval state.
Playback bytes are prepared outside the Qt thread. A candidate counts as heard
only at `EndOfMedia`; stop and playback errors do not unlock a decision. After
both complete listens, the controls reveal the exact consequence: keeping an
approved candidate places it in the derived manifest, while keeping a rejected
candidate preserves the explicit rejection outside the manifest. `Neither
candidate is acceptable` requires a new repair hypothesis. Saving runs outside
the Qt thread and shows that the source reconciliation, workspace state, queue
and candidate WAVs are being rechecked. The mutable `progress.json` is separate
from immutable `review.json` and copied audio. Its write lock records owner,
PID, host and process-start identity; a proven-dead local owner is archived and
recovered, while live or uninspectable owners fail closed.

The progress document is decision evidence only. It deliberately does not
rewrite a workspace or suppress conflicts in a later reconciliation. Applying
completed choices must be a separate fail-closed transaction that copies the
selected exact outcome through the normal review/merge workflow and then
publishes a successor reconciliation report.

The supported command pipeline is:

```bash
uv run --no-sync vntts-pregenerate terminal-conflict-resolution \
  REVIEW_DIRECTORY RESOLUTION_DIRECTORY
uv run --no-sync vntts-pregenerate terminal-conflict-successor \
  RECONCILIATION_JSON RESOLUTION_DIRECTORY SUCCESSOR_DIRECTORY
uv run --no-sync vntts-pregenerate terminal-conflict-merge \
  BASE_WORKSPACE SUCCESSOR_DIRECTORY --workspaces-root WORKSPACES_ROOT
```

Each command prints a JSON result. Existing output destinations are never
replaced.

`publish_terminal_conflict_resolution` is the first application boundary. It
refuses incomplete progress, binds the exact immutable review and mutable
progress payloads, rechecks every source `ReviewAuthority`, and copies only the
selected candidate WAVs into a new no-replace publication. A `neither` decision
is retained explicitly with no selected WAV and the policy `new repair
hypothesis required`. The resolution publication still does not rewrite a
workspace or hide a historical occurrence.

`publish_terminal_conflict_successor` is the second boundary. It accepts only
the exact reconciliation report named by the resolution, validates both from
one captured byte snapshot, rechecks the review, progress, selected resolution
WAVs and all historical workspace authorities, and publishes a no-replace
`successor.json`. Every original occurrence remains in the successor ledger.
Only the exact matching resolution adds one explicit next action:

- selected approved audio -> `apply_selected_approved_outcome`;
- selected rejected audio -> `retain_explicit_rejection`; or
- neither acceptable -> `new_repair_hypothesis_required`.

The successor projection does not mutate a workspace, suppress historical
authority or make a `neither` case publishable.

`vntts.authoring.terminal_conflict_workspace.merge_terminal_conflict_resolution`
is the final application boundary, isolated from the general workbench core as
a leaf orchestration module. It
requires the primary workspace recorded by the reconciliation, derives the
selected state authority from the exact blind candidate, and prefers that
primary authority when identical approved audio exists in more than one
workspace. Otherwise it requires one source or identical state-item authority;
ambiguous state metadata fails closed. It copies the selected terminal state
item and the resolution-owned WAV into a new config-addressed workspace, binds
the report, successor, resolution, source workspace/state/item and WAV hashes,
and rebuilds the approved-only manifest. Existing `outcome_merge` provenance in
the selected item remains intact beneath the new terminal-conflict ledger.

The merge and final-pack publisher use the same generation-lease set and atomic
no-replace publication primitives. The merge acquires every source lease in
stable path order, then rechecks every source while those leases remain held
through publication. It never changes the primary or historical workspaces.
Approved choices enter the derived manifest, rejected choices remain explicit terminal
rejections outside it, and any `neither` choice prevents workspace creation
until a separately versioned repair hypothesis exists.

## Verified current Character Story bundle

On 2026-08-26 the current original-scope reconciliation was published as the
app-data review bundle
`authoring/review-bundles/current-character-story-terminal-conflicts-v1`.
Its immutable identity is
`f9727c91128f4c56193eaa082830837eff41882a20cb5cf070df72316b1b39a3`.
It contains five cases, exactly two candidates per case and ten copied WAVs:

- `reverse1999:314602:110`;
- `reverse1999:314602:92`;
- `reverse1999:314608:27`;
- `reverse1999:314608:35`; and
- `reverse1999:314608:71`.

The immutable bundle tree digest is
`f7e6555052cf9b06d9acc4bbc8e4428354b2b35ac5cbc758c0389284fe39f143`.
Repeated publication returned `created=false` with the same review identity.
No progress document exists before human review. The source reconciliation
remains SHA-256
`60bf25a3d163c534392947203ab3995439872593ede53b8a7a876b36a2379b16`,
and the four referenced workspace state hashes remained unchanged after both
publication checks.

The real-data acceptance measured one-time publication at 26.64 seconds,
candidate loading at 0.087 seconds and a full source-authority decision save at
0.27 seconds. Publication is an operator/setup operation; playback and decision
saves are the interactive path, and saving stays outside the Qt thread.

## Authority refresh after primary Narrator review

The original v1 review correctly failed closed after the three Centurion
cohorts changed the primary workspace from 197 to 326 approvals. An attempted
terminal decision reported `Terminal conflict authority changed` and created no
`progress.json`; replay remained available. The candidate WAVs had not changed,
but their captured source state authority was intentionally stale.

A fresh read-only reconciliation over the same explicit bundle and quality
scope was published as
`authoring/reconciliations/current-character-story-20260827-091d56596664.json`,
report ID
`091d56596664aa50c116bfced31d40cd8862ecdf0b64e1972fd4b5f639b1186e`.
It retains the same five conflicts and current action counts while binding the
326-approved primary state.

The current operator review is the no-replace directory
`authoring/review-bundles/current-character-story-terminal-conflicts-v2`, review
ID `784954f97ced807c8f26fa84ef93fd3034e2e0059b5bcaca91a3f130fb44dec3`.
Its five cases and ten candidate WAV SHA-256 values are identical to v1. Public
source-authority validation passes, no progress decision exists initially, and
the primary state SHA-256 remains
`2cdd8a18b4826f423bad7e06b719b07cd6b6a83e4bbd6ec38cf5e93893407f4e`.
The immutable v1 directory remains historical evidence and must not receive new
decisions.
