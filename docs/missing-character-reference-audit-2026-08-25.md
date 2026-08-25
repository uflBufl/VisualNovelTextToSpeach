# Mrs. Owen and Hotelier source-reference audit, 2026-08-25

This is the exhaustive local evidence boundary for the two Character Story
roles still marked `needs_sample`. It does not assign a voice or change a
manifest. Display-name or portrait similarity alone is not identity evidence.

## Mrs. Owen

The exact installed bank is
`activityvoc_story_npcnoname322_beiai.bnk`, SHA-256
`a311bbce85925e16ff92f4bc746a6a662f05c6717beee962dd1298ca1d32fd64`.
Its current bank index contains ten exact event-to-embedded-media routes, not
only the three clips surfaced by the older candidate report. All ten decoded
as PCM16 mono 24 kHz WAVs:

| Media ID | Duration | WAV SHA-256 | Technical result |
| --- | ---: | --- | --- |
| `139271054` | 0.600 s | `9cd0402568de...` | too short |
| `163813113` | 1.142 s | `367f2185e3af...` | signal pass, short |
| `170251022` | 0.978 s | `4c3f6756b75f...` | too short |
| `562400954` | 3.172 s | `82e3125fbc19...` | new strongest technical candidate |
| `599773947` | 1.953 s | `0ccdc09e4531...` | prior uncertain candidate |
| `655579516` | 0.750 s | `e6c0e595e4f4...` | too short |
| `731712423` | 0.832 s | `fccd46ea72b0...` | too short |
| `960585373` | 1.167 s | `5eccb171c741...` | signal pass, short |
| `1016519778` | 0.440 s | `5b4543d046a5...` | too short |
| `1066861069` | 0.971 s | `d3851208140a...` | too short |

Media `562400954` belongs to bank event `3381126906`. Local ASR approximated
its content as `I figured my disguise was almost perfect.` That transcript is
helpful for intelligibility screening but is not authoritative story identity.
Its decoded WAV SHA-256 is
`82e3125fbc195951006817ccd13d507b40c4d2311c2f17ebc7a37f2505e7e22b`.
Because no exact config/story row currently binds this media to the target Mrs.
Owen lines, the candidate required one human check for speaker identity,
single-speaker content and contamination before a quality evaluation could be
published. The user completed that check on 2026-08-25 and judged the voice
legitimate for Mrs. Owen. That is identity authority for publishing a new
checksum-bound candidate, not generated-quality authority and not permission to
mutate the earlier `needs_sample` review or v4 binding.

The complete-bank extractor v2 report later decoded the same source medium as
WAV SHA-256
`6ae90c5957d0d4fbb55f3d9ac19a5c49c86b403930d7fa59a8d18d3bbf8cd92d`.
This was not treated as silently identical bytes. Comparison against the exact
WAV heard by the user found the same mono 24 kHz waveform with correlation
`0.9999999969`, a least-squares gain ratio of `1.2576918470`, residual SNR
`82.05 dB` and only `1.667 ms` duration difference. The new review decision
records both hashes and these equivalence metrics while binding the exact bank,
event `3381126906`, encoded media hash and new decoded WAV.

The bounded seed-0 fixed-corpus evaluation published one valid WAV (`fixed-1`,
SHA-256
`8ab22999249ffa5a36368c5f9938ac6f3cc1a117941287b431b7bf0e86ecde88`).
The other two first ended at the typed MOSS audio limit. Their one planner-
authorized bounded-seed attempt completed synthesis but failed the speech-
silence gate with `3.84 s`/`69%` silent frames and `2.88 s` internal silence.
No rejected WAV was published and the repair planner now requires reference
comparison rather than another seed. A self-contained `0/1` quality card is at
`authoring/source-reference-quality-reviews/character-story-mrs-owen-exact-bank-v2-20260825`;
the remaining human action is to hear the original reference and `fixed-1`,
then choose accept, reject or needs-sample.
The immutable report/review/plan/evaluation hashes are `e1fc0314355f...`,
`6d936371faad...`, `a1b252f82a5c...` and `dabd18e82f5d...`; the final run state
is `a1876317abf0...` and the initial quality review is `c77dbba592fe...`.

## Hotelier

The exact installed bank is
`activityvoc_story_npcnoname327_beiai.bnk`, SHA-256
`1962bc13f378848ca0f1f702b5b450bfdbca402ef743d53a88a87cae62abdb1f`.
The bank index proves it contains exactly five media IDs and no hidden longer
sixth candidate:

| Media ID | Duration | ASR screening |
| --- | ---: | --- |
| `69489227` | 0.711 s | `That'll be an extra charge.` |
| `385482003` | 0.926 s | `Ms.` |
| `499947617` | 1.147 s | `What happened next?` |
| `574172592` | 1.323 s | `Mrs. Owen!` |
| `776462590` | 0.902 s | `Please allow me.` |

None passes the established minimum-duration reference gate. Together they
contain 5.009 seconds of exact-role English audio; a silence-trimmed,
checksum-ledgered composite can therefore be evaluated as an experimental
same-bank reference, but it still requires generated-quality review and cannot
become an automatic binding. Portrait `505401`
cannot safely expand the search: current source data reuses it across 46
incompatible roles and performers, including Driver, Debate Moderator,
Participant I, Librarian and Researcher. A same-bank composite could be tested
only as an explicit experimental candidate with human listening; it is not an
identity-supported automatic assignment.

Hotelier is a one-off role in a mostly unvoiced Character Story. The complete
five-media bank is therefore the expected source boundary, not evidence of a
broken extraction. Other game versions are not expected to contain different
voice material for this already complete bank. Public recordings, Wiki audio,
other NPC banks and the reused portrait are deliberately outside the search
boundary. The only remaining voice experiment is a provenance-preserving
composite of these five exact clips; otherwise use a role-scoped fallback.
The composite publisher must consume a complete-bank extractor report, retain
all five original WAVs and their event/media/hash ledger, trim only bounded edge
silence, and insert the documented 120 ms gap. It must not cherry-pick a
different performer or imply that the composite passed generated-quality
review.

The current v3 composite was published from all five exact clips at
`authoring/source-reference-composites/character-story-hotelier-exact-bank-v3-20260825`.
It is PCM16 mono 24 kHz, `5.489333 s`, SHA-256
`79c43a24a232d7ac853c13c93b553fa40cdd652b7dfa9926a988cff9a35b3293`,
and passes objective reference preflight. No clip required edge trimming; the
only inserted audio is four declared 120 ms gaps. This still does not establish
generated voice quality or authorize a Hotelier binding. Its ledger/evaluation
SHA-256 values are `97d66728c21b...` and `7425ee8caa4a...`.

The bounded seed-0 Hotelier composite evaluation published valid `fixed-1` and
`fixed-3` WAVs with SHA-256 `c24fee24f220...` and `cca6cb5515aa...`.
`fixed-2` first failed the speech-silence gate with `2.80 s` internal silence
and `64%` silent frames. Its one planner-authorized inline-pause comparison then
ended at the typed audio limit and published no WAV. The final state SHA-256 is
`2140deef14ae...`. The two valid fixed samples and original composite are ready
for a future Hotelier quality decision; no additional seed is authorized.
A self-contained `0/1` card is published at
`authoring/source-reference-quality-reviews/character-story-hotelier-exact-bank-v3-20260825`
with initial SHA-256 `6d1e16c9318d...`. It explicitly labels the reference as
a five-media exact-bank composite and cannot be consumed as ordinary binding
authority without a dedicated post-decision gate.

## Result

Mrs. Owen now has one exact-bank, technically strong, human-accepted identity
candidate. The next bounded step is a new immutable candidate plus a Mrs.
Owen-only synthesis-quality evaluation. Hotelier remains evidence-blocked as a
single-clip reference by design, not because another asset search is pending.
Evaluate the five-clip exact-bank composite once; otherwise Hotelier must stay
`needs_sample` or use an explicit per-role Narrator fallback. The audit does not
justify selecting another version, portrait, performer or generic NPC bank.
