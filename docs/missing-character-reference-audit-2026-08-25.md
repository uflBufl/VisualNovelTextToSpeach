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

The first external discovery lead is the public Rhiannon Character Story guide
linked from the Reverse: 1999 community post
[`The You That's Meant To Be RHIANNON Puzzle Quest`](https://www.reddit.com/r/Reverse1999/comments/1vnb865/the_you_thats_meant_to_be_rhiannon_puzzle_quest/).
Its linked video is `https://www.youtube.com/watch?v=i77DfLpUrGk`; the post
marks `Honeyed Words` at 5:03, a chapter containing an exact Hotelier line in
the local story index. This is only a location to inspect. It is not yet proof
that the recording contains clean English dialogue, nor does the post grant
reuse authority. A candidate taken from it must still satisfy the source,
speaker, contamination, rights and checksum gates above.

## Result

Mrs. Owen now has one exact-bank, technically strong, human-accepted identity
candidate. The next bounded step is a new immutable candidate plus a Mrs.
Owen-only synthesis-quality evaluation. Hotelier remains evidence-blocked as a
single-clip reference. Search exact config/event aliases and official asset
versions first; separately evaluate the five-clip same-bank composite. Public
story recordings or Wiki pages are discovery evidence only unless the exact
English role, stable source/revision, clean single-speaker bytes and reuse terms
can all be bound. Otherwise Hotelier must stay `needs_sample` or use an explicit
per-role Narrator fallback. The audit does not justify selecting another
portrait, performer or generic NPC bank.
