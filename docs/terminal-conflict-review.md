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

The interface presents opaque candidate A/B labels, requires both candidates
to be played, permits replay, and offers candidate A, candidate B or
`Neither candidate is acceptable`. Saving runs outside the Qt thread and shows
that the source reconciliation, workspace state, queue and candidate WAVs are
being rechecked. The mutable `progress.json` is separate from immutable
`review.json` and copied audio.

The progress document is decision evidence only. It deliberately does not
rewrite a workspace or suppress conflicts in a later reconciliation. Applying
completed choices must be a separate fail-closed transaction that copies the
selected exact outcome through the normal review/merge workflow and then
publishes a successor reconciliation report.

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
