# Control-level UI/UX review method

**Status: historical review plan, completed at source/offscreen level in
September 2026.** The consolidated findings and their recorded implementation
status are in [Control-level audit](ui-ux-control-audit.md). This is reusable
review methodology, not an active implementation backlog or an instruction to
repeat the old inventory. Native-platform and representative-user qualification
remain distinct from automated UI evidence.

## Scope and evidence

The original review covered the VNTTS player/setup shell, specialist authoring
reviewers, `reverse1999-extractor` review interfaces, Windows portable launch and
application-owned tray notifications. `vntts-artifacts` had no UI. Native file
choosers and confirmation/error call sites were included because wording and
launch context affect the journey; unrelated OS interfaces and CLI-only tools
were excluded.

A new review must discover the current entry points and surfaces from source
and runtime introspection. Historical surface counts, removed windows and old
module names are not a current completeness gate. Shared components need review
both alone and in each host workflow.

Keep temporary screenshots and raw ledgers in `.codex/investigations/` and
publish only durable conclusions. Local investigation files from the historical
review are not repository-delivered evidence.

## Ledger fields

| Ledger | Evidence to retain |
| --- | --- |
| Surface/state | Stable ID, entry/exit, primary mission, intended user, risk, useful/empty/busy/failure/recovery states, viewport/text scale, keyboard path, close/cancel/stale-result behavior |
| Control | Surface/state, label/type, value/default, visibility/enabled reason, consequence, duplicates, focus/shortcut/accessibility metadata, validation/busy/cancel/undo behavior |
| Finding | Reproduction or evidence, confidence, shared root cause, disposition, smallest fix, affected journeys and acceptance criteria |

Controls discovered dynamically, hidden controls and disabled controls still
need a disposition: keep, rename, move, merge, automate, use native behavior,
remove or add. A screenshot demonstrates layout, not successful interaction.

## Review questions

- Which user mission needs the control, and what fails without it?
- Can the app derive or remember the value instead of asking?
- Is this the right moment and location for the decision?
- Do native interaction, label, default and consequence agree?
- Do visual and keyboard order match the decision order?
- Can a blocked user understand the reason without hover or logs?
- Does action show progress, prevent duplicates and provide safe recovery?
- Can keyboard and assistive-technology users complete it at compact size and
  enlarged text without color or positional assumptions?
- Is emphasis proportional to frequency and risk, with a clear primary action?
- Does another surface solve the same need, and can the shared boundary remove
  duplication?

A proposed new control needs a demonstrated dead end. Better defaults, inline
feedback, automation and existing actions should be considered first.

## Journey coverage

For players, exercise first-run permissions/capture/voice setup and a successful
preview; returning start/pause/replay/stop; preparation and recovery; unknown
speaker decisions; capture/speech remediation; OCR correction scope; history
export; and applying/cancelling profile or engine changes.

For authoring, exercise workspace readiness, eligible generation, individual
review, dedicated cohort/reference decisions, missing-voice reuse, stale or
failed writes, terminal conflicts and blind comparison. Preserve checksum-bound
evidence, decision scope and blinding. For source curation, inspect the currently
shipped candidate/evidence/review flows rather than reopening removed tools.

Use populated, empty, busy, failed and recovered fixtures. Include long text,
slow storage, permissions denied, unavailable models, interrupted work and stale
results. Check minimum supported work area and 100%, 150% and 200% text scale;
qualify native fullscreen/multi-monitor behavior separately on macOS and Windows.

## Synthesis and completion

Merge findings by root cause before creating an implementation backlog. Rank
unsafe/destructive behavior and blocked primary journeys first; separate
observed defects, violated invariants and heuristic design judgments. Prototype
high-impact changes when their behavior is uncertain, then validate affected
missions with representative users.

A source/offscreen review is complete when discovered surfaces and controls are
accounted for, each disposition has evidence, required journeys include recovery,
and duplicate findings have one owner and acceptance gate. Native screen-reader,
platform input/dialog and physical-scaling checks require their own evidence;
passing offscreen tests must not mark them complete.

For current product direction use [Reader experience](reader-experience.md).
For implementation constraints use [Desktop accessibility](desktop-accessibility.md)
and [Testing principles](testing-principles.md).
