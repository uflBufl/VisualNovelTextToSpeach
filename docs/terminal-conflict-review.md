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
