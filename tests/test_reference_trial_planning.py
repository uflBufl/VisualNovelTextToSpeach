import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from scripts.replay_reference_trial import replay
from tests.story_fixtures import write_content
from tests.voice_manifest_fixtures import (
    write_conflicting_manifest,
    write_player_candidate_manifest,
)
from vntts.document_identity import sha256_file
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import (
    PLAYER_VOICE_CANDIDATES_FIELD,
    PregenerationVoiceError,
    ReferenceRanking,
    ReferenceTrial,
    VoiceDecisionStore,
    VoicePlanStore,
    _eligible_candidates,
)
from vntts.settings import AppSettings
from vntts.voice_library import VoiceLibrary
from vntts.voices import CharacterVoiceRegistry, remember_voice_binding


class ReferenceTrialPlanningTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.story_index = write_content(self.root / "content")
        self.jobs = PregenerationJobStore(self.root / "jobs")
        self.job = self.jobs.create_or_resume(
            inspect_story_index(self.story_index), ("story",)
        )
        self.manifest = write_player_candidate_manifest(
            self.root / "voices",
            self.job.story_index_sha256,
            quality_scores=(100, 100, 100, 100, 100),
            admission_policy="audio-only",
        )
        self.settings = AppSettings(speech_backend="moss-tts")
        # Trial tests inject choices independently of acoustic similarity.
        self.enterContext(
            patch(
                "vntts.reference_selection.make_reference_selector",
                return_value=Mock(return_value=ReferenceRanking(None, None)),
            )
        )
        self.baseline = self.group(self.create())
        self.preferred = self.baseline.candidate_inventory[3]
        self.typical = self.baseline.candidate_inventory[4]
        self.trial = ReferenceTrial(
            sha256_file(self.manifest),
            {"Rhiannon": self.preferred.reference_sha256s[0]},
        )
        self.dual_trial = replace(
            self.trial,
            typical_selections={"Rhiannon": self.typical.reference_sha256s[0]},
        )

    @staticmethod
    def group(plan):
        return next(group for group in plan.groups if group.character == "Rhiannon")

    def create(
        self, *, trial=None, qualifier=None, library=None, decisions=None, settings=None
    ):
        return VoicePlanStore(
            self.jobs, voice_library=library, decisions=decisions
        ).create(
            self.job,
            settings or self.settings,
            manifest_path=self.manifest,
            reference_trial=trial,
            reference_qualifier=qualifier,
        )

    def test_trial_enters_real_preview_shortlist_and_preserves_remaining_budget(self):
        qualifier = Mock(return_value={})
        selected = self.group(self.create(trial=self.trial, qualifier=qualifier))
        expected = (self.preferred, *self.baseline.candidates[:2])
        self.assertEqual(selected.candidates, expected)
        self.assertEqual(self.group(qualifier.call_args.args[0]).candidates, expected)
        self.assertEqual(
            selected.candidate_inventory, self.baseline.candidate_inventory
        )
        self.assertEqual(self.group(self.create()).candidates, self.baseline.candidates)

    def test_stale_catalog_rejected_before_plan_is_written(self):
        with self.assertRaisesRegex(PregenerationVoiceError, "does not match"):
            self.create(trial=ReferenceTrial("a" * 64, self.trial.selections))
        with self.assertRaisesRegex(PregenerationVoiceError, "does not match"):
            self.create(
                trial=replace(self.trial, typical_selections={"Rhiannon": "invalid"})
            )

    def test_integrated_selector_uses_original_catalog_and_persists_qualified_voice(
        self,
    ):
        library = VoiceLibrary(self.root / "library")
        select = Mock(
            return_value=ReferenceRanking(
                self.preferred.reference_sha256s[0],
                self.typical.reference_sha256s[0],
            )
        )

        def qualify(plan):
            group = self.group(plan)
            self.assertEqual(group.candidates[:2], (self.preferred, self.typical))
            return {group.group_id: self.typical.source_id}

        with patch(
            "vntts.reference_selection.make_reference_selector",
            return_value=select,
        ) as factory:
            selected = self.group(self.create(qualifier=qualify, library=library))
            self.assertEqual(factory.call_args.args, (self.manifest.resolve(),))
            self.assertEqual(
                factory.call_args.kwargs["expected_manifest_sha256"],
                sha256_file(self.manifest),
            )
            self.assertEqual(selected.reference_sha256s, self.typical.reference_sha256s)
            select.reset_mock()
            saved = self.group(self.create(library=library))
            self.assertEqual(saved.reference_sha256s, self.typical.reference_sha256s)
            select.assert_not_called()

    def test_distinct_typical_enters_real_budget_beyond_original_shortlist(self):
        qualifier = Mock(return_value={})
        selected = self.group(self.create(trial=self.dual_trial, qualifier=qualifier))
        expected = (self.preferred, self.typical, self.baseline.candidates[0])
        self.assertEqual(selected.candidates, expected)
        self.assertEqual(self.group(qualifier.call_args.args[0]).candidates, expected)
        self.assertEqual(
            selected.candidate_inventory, self.baseline.candidate_inventory
        )

    def test_transcript_bound_engines_share_ranking_only_for_cloned_voices(self):
        self.manifest = write_player_candidate_manifest(
            self.root / "transcript-bound",
            self.job.story_index_sha256,
            quality_scores=(100,) * 5,
        )
        preferred = self.preferred.reference_sha256s[0]
        for backend, cloning in (
            ("coqui-xtts", True),
            ("qwen-tts", True),
            ("chatterbox-nano", True),
            ("pocket-tts", True),
            ("pocket-tts", False),
        ):
            with (
                self.subTest(backend=backend, cloning=cloning),
                patch(
                    "vntts.pregeneration_voices.pocket_model_ready",
                    return_value=cloning,
                ),
                patch("vntts.reference_selection.make_reference_selector") as factory,
            ):
                factory.return_value.return_value = ReferenceRanking(preferred, None)
                group = self.group(
                    self.create(settings=AppSettings(speech_backend=backend))
                )
                if cloning:
                    self.assertEqual(
                        factory.call_args.kwargs["speech_backend"], backend
                    )
                    self.assertEqual(group.reference_sha256s, (preferred,))
                else:
                    factory.assert_not_called()
                    self.assertFalse(group.reference_sha256s)

    def test_identical_typical_uses_only_one_preview_slot(self):
        trial = replace(self.trial, typical_selections=self.trial.selections)
        selected = self.group(self.create(trial=trial))
        self.assertEqual(
            selected.candidates, (self.preferred, *self.baseline.candidates[:2])
        )

    def test_dual_trial_preserves_playable_reservation(self):
        playable = replace(
            self.baseline.candidates[0],
            source_bank="mianvoc_hero_test.bnk",
            source_line_ids=("playable-voice:test",),
            reference_duration_seconds=6.0,
        )
        inventory = (playable, *self.baseline.candidate_inventory[1:])
        self.assertEqual(
            _eligible_candidates(
                inventory,
                preferred_reference_sha256=self.preferred.reference_sha256s[0],
                typical_reference_sha256=self.typical.reference_sha256s[0],
            ),
            (self.preferred, self.typical, playable),
        )

    def test_typical_cannot_promote_weaker_unlinked_or_overlong_playable_evidence(self):
        for typical in (
            replace(self.typical, match_score=self.typical.match_score - 1),
            replace(self.typical, candidate_origin="unlinked_bank_media"),
            replace(
                self.typical,
                source_bank="mianvoc_hero_test.bnk",
                source_line_ids=("playable-voice:test",),
                reference_duration_seconds=12.1,
            ),
        ):
            with self.subTest(typical=typical):
                self.assertEqual(
                    _eligible_candidates(
                        (*self.baseline.candidate_inventory[:4], typical),
                        preferred_reference_sha256=self.preferred.reference_sha256s[0],
                        typical_reference_sha256=typical.reference_sha256s[0],
                    ),
                    (self.preferred, *self.baseline.candidates[:2]),
                )

    def test_saved_manual_and_automatic_bindings_and_decisions_keep_priority(self):
        registry = CharacterVoiceRegistry.from_file(self.manifest)
        source_id = self.baseline.candidates[0].source_id
        for method in ("manual", "automatic"):
            library = VoiceLibrary(self.root / method)
            remember_voice_binding(
                library, registry, "Rhiannon", source_id, method=method
            )
            selected = self.group(self.create(trial=self.dual_trial, library=library))
            self.assertEqual(
                selected.reference_sha256s, self.baseline.reference_sha256s
            )
            self.assertEqual(selected.resolution, "saved-voice-assignment")
        decisions = VoiceDecisionStore(self.root / "decisions.json")
        decisions.remember(self.baseline, source_id)
        selected = self.group(self.create(trial=self.dual_trial, decisions=decisions))
        self.assertEqual(selected.source_id, source_id)
        self.assertEqual(selected.resolution, "saved-player-decision")

    def test_trial_does_not_override_other_backend_or_weaker_or_ineligible_evidence(
        self,
    ):
        document = json.loads(self.manifest.read_text())
        document[PLAYER_VOICE_CANDIDATES_FIELD]["reference_admission_policy"] = (
            "transcript-bound"
        )
        self.manifest.write_text(json.dumps(document))
        selected = self.group(
            self.create(
                trial=ReferenceTrial(sha256_file(self.manifest), self.trial.selections),
                settings=AppSettings(
                    speech_backend="coqui-xtts",
                    xtts_terms_accepted=True,
                ),
            )
        )
        self.assertEqual(selected.candidates, self.baseline.candidates)
        for candidate in (
            replace(self.preferred, match_score=10),
            replace(self.preferred, candidate_origin="unlinked_bank_media"),
        ):
            shortlist = _eligible_candidates(
                (*self.baseline.candidates, candidate),
                preferred_reference_sha256=candidate.reference_sha256s[0],
            )
            self.assertEqual(shortlist, self.baseline.candidates)

    def test_replay_uses_selected_and_reports_pending_without_legacy_substitution(self):
        report = {
            "manifest_sha256": self.trial.manifest_sha256,
            "character": "Rhiannon",
            "selected": {"sha256": self.preferred.reference_sha256s[0]},
            "representative": {"sha256": self.typical.reference_sha256s[0]},
        }
        result = replay(self.story_index, self.manifest, [report], self.root / "replay")
        trial = next(iter(result["trial"].values()))
        self.assertEqual(
            trial["preview_shortlist"][0]["source_id"], self.preferred.source_id
        )
        self.assertEqual(
            trial["preview_shortlist"][1]["source_id"], self.typical.source_id
        )
        report["selected"] = None
        report["representative"] = {"sha256": self.preferred.reference_sha256s[0]}
        result = replay(
            self.story_index, self.manifest, [report], self.root / "pending"
        )
        self.assertEqual(result["pending_characters"], ["Rhiannon"])
        self.assertEqual(result["baseline"], result["trial"])

    def test_replay_rejects_malformed_typical_checksum(self):
        report = {
            "manifest_sha256": self.trial.manifest_sha256,
            "character": "Rhiannon",
            "selected": {"sha256": self.preferred.reference_sha256s[0]},
            "representative": {"sha256": "invalid"},
        }
        with self.assertRaisesRegex(ValueError, "typical"):
            replay(self.story_index, self.manifest, [report], self.root / "replay")

    def test_legacy_representative_only_report_remains_usable(self):
        report = {
            "manifest_sha256": self.trial.manifest_sha256,
            "character": "Rhiannon",
            "representative": {"sha256": self.typical.reference_sha256s[0]},
        }
        result = replay(self.story_index, self.manifest, [report], self.root / "legacy")
        selected = next(iter(result["trial"].values()))
        self.assertEqual(
            [candidate["source_id"] for candidate in selected["preview_shortlist"]],
            [
                self.typical.source_id,
                *[c.source_id for c in self.baseline.candidates[:2]],
            ],
        )
        self.assertEqual(result["pending_characters"], [])

    def test_bound_source_keeps_priority(self):
        self.manifest = write_conflicting_manifest(
            self.root / "bound-voices", bind_selected_lines=True
        )
        baseline = self.group(self.create())
        trial = ReferenceTrial(
            sha256_file(self.manifest),
            {"Rhiannon": sha256_file(self.manifest.parent / "references/child.wav")},
            {"Rhiannon": sha256_file(self.manifest.parent / "references/child.wav")},
        )
        selected = self.group(self.create(trial=trial))
        self.assertEqual(selected.source_id, baseline.source_id)
        self.assertEqual(selected.reference_sha256s, baseline.reference_sha256s)
        self.assertEqual(selected.candidates, baseline.candidates)


if __name__ == "__main__":
    unittest.main()
