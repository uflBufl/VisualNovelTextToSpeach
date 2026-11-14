import os
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.pregeneration_fixtures import (  # noqa: E402
    ambiguous_fixture,
    clean_wav_bytes,
)
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.pregeneration_audition_ui import VoiceAuditionPanel  # noqa: E402


class VoiceComparisonIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.plan, self.group, _manifest = ambiguous_fixture(Path(temporary.name))
        self.candidate = self.group.candidate_inventory[0]
        self.panel = VoiceAuditionPanel(
            Mock(),
            preview_service=Mock(),
            content_advisor=Mock(),
            thread_pool=ManualThreadPool(),
        )
        self.addCleanup(self.panel.deleteLater)
        self.addCleanup(self.panel.shutdown)

    def assert_comparison(self, saved, candidate, *, backend, different):
        def plan_for(voice):
            group = replace(
                self.group,
                source_id=voice.source_id,
                candidates=(voice,),
                candidate_inventory=(voice,),
                narrator_candidate=None,
            )
            return replace(self.plan, synthesis_backend=backend, groups=(group,))

        self.panel.set_current_voice(plan_for(saved), "Saved voice")
        plan = plan_for(candidate)
        self.panel.start(plan, group_id=self.group.group_id)
        self.assertEqual(self.panel.current_voice_box.isHidden(), not different)
        self.assertEqual(
            self.panel.title(), "New voice" if different else "Selected reference"
        )
        item = self.panel.reference_model.item(0, 0)
        self.assertEqual(item.icon().isNull(), different)
        self.assertEqual(item.font().bold(), not different)

    def test_same_wav_ignores_saved_alias_and_donor_metadata(self):
        saved = replace(
            self.candidate,
            source_id="character:voicelibrary1234",
            source_character="Narrator",
            source_speaker="Voicelibrary1234",
            source_excerpts=("Saved excerpt.",),
        )
        for backend in ("moss-tts", "pocket-tts", "chatterbox-nano", "qwen-tts"):
            with self.subTest(backend=backend):
                self.assert_comparison(
                    saved, self.candidate, backend=backend, different=False
                )

    def test_non_qwen_transcripts_do_not_change_same_recording(self):
        saved = replace(self.candidate, reference_transcript="Saved words.")
        selected = replace(self.candidate, reference_transcript="Other metadata.")
        for backend in ("moss-tts", "pocket-tts", "chatterbox-nano", "coqui-xtts"):
            with self.subTest(backend=backend):
                self.assert_comparison(
                    saved, selected, backend=backend, different=False
                )

    def test_different_recording_or_reference_order_keeps_comparison(self):
        original = self.candidate.reference_sha256s[0]
        other = sha256(clean_wav_bytes(amplitude=0.2)).hexdigest()
        self.assertNotEqual(original, other)
        for saved_refs, selected_refs in (
            ((original,), (other,)),
            ((original, other), (other, original)),
            ((original,), (original, other)),
        ):
            with self.subTest(saved=saved_refs, selected=selected_refs):
                self.assert_comparison(
                    replace(self.candidate, reference_sha256s=saved_refs),
                    replace(self.candidate, reference_sha256s=selected_refs),
                    backend="moss-tts",
                    different=True,
                )

    def test_presets_use_speaker_identity(self):
        saved = replace(
            self.candidate,
            source_id="preset:alba",
            source_character="Alba",
            source_speaker="alba",
            reference_sha256s=(),
        )
        for speaker, different in (("alba", False), ("marius", True)):
            with self.subTest(speaker=speaker):
                selected = replace(
                    saved,
                    source_id=f"preset-alias:{speaker}",
                    source_character="Preset alias",
                    source_speaker=speaker,
                )
                self.assert_comparison(
                    saved, selected, backend="pocket-tts", different=different
                )

    def test_coqui_keeps_speaker_as_synthesis_input_with_same_wav(self):
        selected = replace(self.candidate, source_speaker="other-speaker")
        self.assert_comparison(
            self.candidate, selected, backend="coqui-xtts", different=True
        )

    def test_qwen_preserves_meaningful_reference_transcript_changes(self):
        saved = replace(self.candidate, reference_transcript="Original words.")
        for transcript in ("Different words.", None, ""):
            with self.subTest(transcript=transcript):
                self.assert_comparison(
                    saved,
                    replace(self.candidate, reference_transcript=transcript),
                    backend="qwen-tts",
                    different=True,
                )

    def test_qwen_normalizes_empty_and_surrounding_transcript_whitespace(self):
        for saved_text, selected_text in (
            (None, ""),
            (None, " \t "),
            ("", None),
            ("Original words.", " Original words. "),
        ):
            with self.subTest(saved=saved_text, selected=selected_text):
                self.assert_comparison(
                    replace(self.candidate, reference_transcript=saved_text),
                    replace(self.candidate, reference_transcript=selected_text),
                    backend="qwen-tts",
                    different=False,
                )


if __name__ == "__main__":
    unittest.main()
