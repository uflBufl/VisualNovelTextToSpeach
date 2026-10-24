import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from tests.pregeneration_fixtures import (  # noqa: E402
    ambiguous_fixture,
    clean_wav_bytes,
)
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from tests.test_pregeneration_audition_ui import with_second_candidate  # noqa: E402
from vntts.document_identity import sha256_file  # noqa: E402
from vntts.pregeneration_audition import (  # noqa: E402
    VoiceAuditionCancelled,
    VoiceAuditionPreview,
    VoiceAuditionReference,
    preview_identity,
)
from vntts.pregeneration_audition_ui import VoiceAuditionPanel  # noqa: E402
from vntts.preview_content_advisor import PreviewContentAdvisor  # noqa: E402


class ReferenceAdviceUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def fixture(self, root):
        plan, group, manifest = ambiguous_fixture(root)
        plan, group = with_second_candidate(plan, group)
        candidates = (
            group.candidates[0],
            replace(group.candidates[1], match_score=group.candidates[0].match_score),
        )
        group = replace(group, candidates=candidates, candidate_inventory=candidates)
        plan = replace(plan, groups=(group,))
        previews = {
            candidate.source_id: self.preview(root, plan, group, candidate)
            for candidate in candidates
        }
        transcript = Mock(
            side_effect=[
                group.sample_text + " Wine.",
                group.sample_text,
            ]
        )
        model = {"status": "missing"}
        installer = Mock(side_effect=lambda: model.update(status="installed"))
        advisor = PreviewContentAdvisor(
            root,
            transcriber_factory=lambda _: transcript,
            model_resolver=lambda: root,
            model_status=lambda: model,
            model_installer=installer,
        )
        pool, service, player, decisions = ManualThreadPool(), Mock(), Mock(), Mock()
        ready = {}
        service.root = root
        service.backend = None
        service.preparing = False
        service.supports_prefetch = True
        service.can_prepare_automatically.return_value = True
        service.peek.side_effect = lambda _plan, _group, source, **_: ready.get(source)
        pending = set()

        def prefetch(_plan, requests, *, automatic=False):
            self.assertTrue(automatic)
            pending.clear()
            pending.update(
                source for _group, source, _text in requests if source not in ready
            )

        service.prefetch.side_effect = prefetch
        service.preparation_state.side_effect = lambda _plan, _group, source, **_: (
            "ready"
            if source in ready
            else "queued"
            if source in pending
            else "not-prepared"
        )

        def generate(_plan, _group, source, *, text, cancel_event, automatic=False):
            if cancel_event.is_set():
                raise VoiceAuditionCancelled("Cancelled")
            self.assertEqual(text, group.sample_text)
            ready[source] = previews[source]
            return ready[source]

        service.generate.side_effect = generate
        reference = manifest.parent / "references" / "rhiannon.wav"
        service.reference_audio.return_value = VoiceAuditionReference(
            reference, reference.read_bytes()
        )
        panel = VoiceAuditionPanel(
            decisions,
            preview_service=service,
            content_advisor=advisor,
            player=player,
            thread_pool=pool,
        )
        self.addCleanup(panel.deleteLater)
        self.addCleanup(panel.shutdown)
        return SimpleNamespace(
            panel=panel,
            pool=pool,
            advisor=advisor,
            transcript=transcript,
            installer=installer,
            service=service,
            ready=ready,
            pending=pending,
            previews=previews,
            decisions=decisions,
            player=player,
            candidates=candidates,
            group=group,
            plan=plan,
        )

    @staticmethod
    def preview(root, plan, group, candidate):
        identity = preview_identity(
            candidate,
            group.sample_text,
            backend=plan.synthesis_backend,
            model=plan.synthesis_model,
            language=plan.synthesis_language,
            profile=plan.synthesis_profile,
        )
        path = root / f"{identity}.wav"
        path.write_bytes(clean_wav_bytes())
        checksum = sha256_file(path)
        path.with_suffix(".json").write_text(
            json.dumps({"identity": identity, "audio_sha256": checksum, "seed": 0})
        )
        return VoiceAuditionPreview(
            identity=identity,
            group_id=group.group_id,
            candidate_source_id=candidate.source_id,
            text=group.sample_text,
            backend=plan.synthesis_backend,
            model=plan.synthesis_model,
            generation_profile=plan.synthesis_profile,
            seed=0,
            path=path,
            audio_sha256=checksum,
            sample_rate=24000,
            duration_seconds=1,
            reused=False,
        )

    def drain(self, fixture):
        for _ in range(20):
            self.application.processEvents()
            fixture.panel._refresh_runtime()
            if not fixture.pool.tasks:
                return
            fixture.pool.run_next()
        self.fail("Automatic reference preparation did not finish its bounded batch")

    def test_opening_keeps_existing_choice_and_only_queues_listening_window(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            group = replace(f.group, source_id=f.candidates[1].source_id)
            f.panel.start(replace(f.plan, groups=(group,)), group_id=group.group_id)
            self.drain(f)
            self.assertEqual(f.panel._current_entry()[0].source_id, group.source_id)
            self.assertEqual(
                f.panel.voice_reference.count(), len(group.candidate_inventory)
            )
            requests = f.service.prefetch.call_args.args[1]
            self.assertEqual(requests[0][1], group.source_id)
            self.assertLessEqual(len(requests), 3)
            f.service.generate.assert_not_called()
            f.transcript.assert_not_called()
            f.player.play_bytes.assert_not_called()
            f.decisions.remember_many.assert_not_called()

    def test_ready_content_checks_never_select_play_or_save(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.ready.update(f.previews)
            f.panel.start(f.plan)
            self.drain(f)
            self.assertEqual(f.transcript.call_count, 2)
            f.installer.assert_called_once_with()
            self.assertEqual(f.panel.voice_reference.currentIndex(), 0)
            self.assertNotIn("Suggested:", f.panel.recommendation.text())
            self.assertIn(
                "Possible extra or missing words", f.panel.recommendation.text()
            )
            self.assertIn("Check words by ear", f.panel.voice_reference.itemText(0))
            self.assertNotIn("No text mismatch", f.panel.voice_reference.itemText(1))
            self.assertEqual(
                f.panel._content_checks[f.candidates[1].source_id].advice.status,
                "no_asr_mismatch",
            )
            self.assertFalse(f.panel.a_use.isEnabled())
            f.player.play_bytes.assert_not_called()
            f.decisions.remember_many.assert_not_called()
            buttons = {button.text() for button in f.panel.findChildren(QPushButton)}
            self.assertNotIn("Check ready previews", buttons)
            self.assertNotIn("Show suggestion", buttons)
            f.panel.play_a()
            f.player.play_bytes.assert_called_once()
            self.assertTrue(f.panel.a_use.isEnabled())
            self.assertEqual(f.panel._accepted_choice, f.candidates[0].source_id)

    def test_font_scaled_playback_buttons_keep_their_geometry(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.ready.update(f.previews)
            f.panel.start(f.plan)
            font = f.panel.font()
            font.setPointSizeF(font.pointSizeF() * 2)
            f.panel.setFont(font)
            f.panel.resize(680, 700)
            f.panel.show()
            self.application.processEvents()
            before = (f.panel.a_play.geometry(), f.panel.a_original.geometry())
            for action in (
                f.panel.a_play,
                f.panel.a_play,
                f.panel.a_original,
                f.panel.a_original,
            ):
                action.click()
                self.application.processEvents()
                self.assertEqual(
                    before, (f.panel.a_play.geometry(), f.panel.a_original.geometry())
                )
            f.service.generate.assert_not_called()
            f.decisions.remember_many.assert_not_called()

    def test_cached_ready_preview_has_play_action_without_waiting_for_timer(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.ready.update(f.previews)
            f.panel.start(f.plan)
            self.assertIn("Ready", f.panel.voice_reference.currentText())
            self.assertEqual(f.panel.a_play.text(), "Play preview")
            f.panel.a_play.click()
            f.service.generate.assert_not_called()
            f.player.play_bytes.assert_called_once()

    def test_verified_current_warning_survives_neighbor_check_and_failure(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            first, second = f.candidates
            f.ready[first.source_id] = f.previews[first.source_id]
            f.panel.start(f.plan)
            self.drain(f)
            self.assertIn(
                "Possible extra or missing words", f.panel.recommendation.text()
            )
            f.ready[second.source_id] = f.previews[second.source_id]
            f.transcript.side_effect = RuntimeError("ASR device unavailable")
            f.panel._refresh_runtime()
            self.assertTrue(f.panel.advice_runner.active)
            self.assertIn(
                "Possible extra or missing words", f.panel.recommendation.text()
            )
            self.drain(f)
            self.assertIn(
                "Possible extra or missing words", f.panel.recommendation.text()
            )
            self.assertIn(
                "ASR device unavailable", f.panel.advice_details.toPlainText()
            )
            f.panel._refresh_runtime()
            self.assertIn(
                "ASR device unavailable", f.panel.advice_details.toPlainText()
            )
            f.player.play_bytes.assert_not_called()
            f.decisions.remember_many.assert_not_called()

    def add_inventory(self, f, root):
        candidates = f.candidates + tuple(
            replace(
                f.candidates[1],
                source_id=f"character:extra-{index}",
                source_speaker=f"extra-{index}",
                reference_transcript=f"Original reference {index}.",
            )
            for index in range(16)
        )
        group = replace(f.group, candidate_inventory=candidates)
        f.previews.update(
            (candidate.source_id, self.preview(root, f.plan, group, candidate))
            for candidate in candidates[2:]
        )
        f.transcript.side_effect = None
        f.transcript.return_value = group.sample_text
        return replace(f.plan, groups=(group,)), group, candidates

    def test_full_inventory_last_reference_plays_matching_original_and_returns(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = self.fixture(root)
            plan, group, candidates = self.add_inventory(f, root)
            f.panel.start(plan, group_id=group.group_id)
            self.drain(f)
            self.assertEqual(f.panel.voice_reference.count(), 18)
            f.panel.voice_reference.setCurrentIndex(17)
            self.assertEqual(f.panel._current_entry()[0], candidates[17])
            original = Path(plan.voice_manifest).parent / "references" / "centurion.wav"
            f.service.reference_audio.return_value = VoiceAuditionReference(
                original, original.read_bytes()
            )
            f.panel.a_original.click()
            self.drain(f)
            f.service.reference_audio.assert_called_once_with(
                plan, group, candidates[17].source_id
            )
            self.assertEqual(
                f.player.play_bytes.call_args.args[0], original.read_bytes()
            )
            f.panel.voice_reference.setCurrentIndex(0)
            self.assertEqual(f.panel._current_entry()[0], candidates[0])
            self.assertIsNone(f.panel._accepted_choice)
            f.decisions.remember_many.assert_not_called()

    def test_browsing_middle_inventory_prepares_current_and_neighbors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = self.fixture(root)
            plan, group, candidates = self.add_inventory(f, root)
            f.panel.start(plan, group_id=group.group_id)
            f.panel.voice_reference.setCurrentIndex(4)
            requests = f.service.prefetch.call_args.args[1]
            self.assertEqual(
                [source for _group, source, _text in requests],
                [candidates[index].source_id for index in (4, 5, 3)],
            )
            self.assertIn("Queued", f.panel.voice_reference.itemText(4))
            self.assertNotIn("Not prepared", f.panel.voice_reference.itemText(7))
            f.ready.update(
                (candidate.source_id, f.previews[candidate.source_id])
                for candidate in candidates
            )
            self.drain(f)
            self.assertEqual(f.transcript.call_count, 3)
            self.assertIn("Ready", f.panel.voice_reference.itemText(7))
            self.assertNotIn("Content not checked", f.panel.voice_reference.itemText(7))
            self.assertEqual(f.panel.voice_reference.currentIndex(), 4)

    def test_highlight_dwell_prepares_before_click_without_changing_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = self.fixture(root)
            plan, group, candidates = self.add_inventory(f, root)
            f.panel.start(plan, group_id=group.group_id)
            f.service.prefetch.reset_mock()
            f.panel.voice_reference.highlighted.emit(5)
            self.assertFalse(f.service.prefetch.called)
            QTest.qWait(220)
            requests = f.service.prefetch.call_args.args[1]
            self.assertEqual(requests[0][1], candidates[5].source_id)
            self.assertEqual(len(requests), 3)
            self.assertEqual(f.panel.voice_reference.currentIndex(), 0)
            f.player.play_bytes.assert_not_called()
            f.decisions.remember_many.assert_not_called()

    def test_runtime_and_status_changes_keep_compact_geometry(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.panel.start(f.plan)
            f.panel.resize(680, 700)
            self.application.processEvents()
            before = (
                f.panel.sizeHint(),
                f.panel.a_play.geometry(),
                f.panel.runtime.geometry(),
            )
            f.service.preparing = True
            f.panel._refresh_runtime()
            f.panel.status.setPlainText(
                "Preparing " + "a long progress description " * 30
            )
            self.application.processEvents()
            self.assertEqual(
                before,
                (
                    f.panel.sizeHint(),
                    f.panel.a_play.geometry(),
                    f.panel.runtime.geometry(),
                ),
            )
            self.assertTrue(f.panel.runtime.isHidden())
            self.assertLess(f.panel.sizeHint().width(), 900)
            self.assertFalse(f.panel.reference_details.isVisible())

    def test_background_content_check_preserves_manual_play_and_stop(self):
        for original in (False, True):
            with self.subTest(original=original), TemporaryDirectory() as directory:
                f = self.fixture(Path(directory))
                f.ready.update(f.previews)
                f.panel.start(f.plan)
                action = f.panel._play_original if original else f.panel.play_a
                action()
                stops = f.player.stop.call_count
                self.drain(f)
                self.assertEqual(f.panel.voice_reference.currentIndex(), 0)
                self.assertEqual(f.player.stop.call_count, stops)
                f.player.play_bytes.assert_called_once()
                self.assertTrue(f.panel.a_use.isEnabled())
                self.assertEqual(f.panel._accepted_choice, f.candidates[0].source_id)
                self.assertEqual(
                    f.panel.a_original.text() if original else f.panel.a_play.text(),
                    "Stop original" if original else "Stop preview",
                )
                action()
                self.assertGreater(f.player.stop.call_count, stops)
                self.assertTrue(f.panel.a_use.isEnabled())

    def test_ready_play_and_prefetch_remain_available_in_both_check_orders(self):
        for check_first in (False, True):
            with (
                self.subTest(check_first=check_first),
                TemporaryDirectory() as directory,
            ):
                f = self.fixture(Path(directory))
                f.panel.start(f.plan)
                self.drain(f)
                f.ready.update(f.previews)
                f.service.preparing = True
                if check_first:
                    f.panel._refresh_runtime()
                    self.assertTrue(f.panel.advice_runner.active)

                f.panel.play_a()
                f.player.play_bytes.assert_called_once()
                self.assertEqual(f.panel._playing_source, "preview")
                f.panel._refresh_runtime()
                self.assertTrue(f.panel.advice_runner.active)
                self.assertTrue(f.panel.a_play.isEnabled())
                self.assertTrue(f.panel.voice_reference.isEnabled())
                f.service.prefetch.assert_called()
                f.service.cancel.assert_not_called()
                f.transcript.assert_not_called()

                stops = f.player.stop.call_count
                self.drain(f)
                self.assertEqual(f.transcript.call_count, 2)
                self.assertEqual(f.player.stop.call_count, stops)
                self.assertEqual(f.panel._playing_source, "preview")

    def test_cached_checks_do_not_reselect_saved_choice(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.advisor.check(tuple(f.previews.values()))
            f.transcript.reset_mock()
            f.ready.update(f.previews)
            saved = replace(
                f.group,
                resolution="saved-player-decision",
                source_id=f.candidates[0].source_id,
            )
            f.panel.start(replace(f.plan, groups=(saved,)))
            self.drain(f)
            self.assertEqual(f.panel.voice_reference.currentIndex(), 0)
            f.service.generate.assert_not_called()
            f.transcript.assert_not_called()
            f.player.play_bytes.assert_not_called()
            f.decisions.remember_many.assert_not_called()

    def test_cancel_stops_pending_hover_and_content_check(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.ready.update(f.previews)
            f.panel.start(f.plan)
            f.panel._refresh_runtime()
            f.panel.voice_reference.highlighted.emit(1)
            f.panel.cancel()
            calls = f.service.prefetch.call_count
            QTest.qWait(220)
            self.drain(f)
            self.assertEqual(f.service.prefetch.call_count, calls)
            f.service.cancel.assert_called()
            f.transcript.assert_not_called()
            f.player.play_bytes.assert_not_called()
            f.decisions.remember_many.assert_not_called()
            self.assertTrue(f.panel.isHidden())

    def test_saving_cancels_pending_check_after_last_group_advances(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.ready.update(f.previews)
            f.panel.start(f.plan)
            f.pool.run_next()
            self.application.processEvents()
            self.assertTrue(f.panel.advice_runner.active)
            f.panel.choose_for_me()
            self.drain(f)
            f.transcript.assert_not_called()
            f.decisions.remember_many.assert_called_once_with(
                ((f.group, f.candidates[0].source_id),)
            )
            self.assertTrue(f.panel.isHidden())

    def test_cache_hydration_restores_off_focus_rows_without_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = self.fixture(root)
            plan, group, candidates = self.add_inventory(f, root)
            f.service.restore_cached.side_effect = lambda *_args, **_kwargs: (
                f.ready.update(f.previews)
            )
            f.panel.start(plan, group_id=group.group_id)
            self.assertTrue(f.panel.cache_runner.active)
            self.drain(f)
            requests = f.service.restore_cached.call_args.args[1]
            self.assertEqual(len(requests), len(candidates))
            self.assertIn("Ready", f.panel.voice_reference.itemText(7))
            self.assertNotIn("Content not checked", f.panel.voice_reference.itemText(7))
            f.service.generate.assert_not_called()
            self.assertEqual(f.transcript.call_count, 3)
            f.player.play_bytes.assert_not_called()
            f.decisions.remember_many.assert_not_called()

    def test_cached_content_evidence_is_not_rehashed_by_runtime_timer(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.advisor.check(tuple(f.previews.values()))
            f.advisor.lookup_saved = Mock(wraps=f.advisor.lookup_saved)
            f.ready.update(f.previews)
            f.panel.start(f.plan)
            self.drain(f)
            reads = f.advisor.lookup_saved.call_count
            self.assertEqual(reads, 2)
            for _ in range(5):
                f.panel._refresh_runtime()
                self.drain(f)
            self.assertEqual(f.advisor.lookup_saved.call_count, reads)
            self.assertNotIn("No text mismatch", f.panel.voice_reference.itemText(1))
            self.assertEqual(
                f.panel._content_checks[f.candidates[1].source_id].advice.status,
                "no_asr_mismatch",
            )
            f.ready.clear()
            f.panel._refresh_runtime()
            self.assertNotIn("No text mismatch", f.panel.advice_details.toPlainText())

    def test_cancelled_phrase_check_can_run_when_returning_to_original_phrase(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.ready.update(f.previews)
            f.panel.start(f.plan)
            self.application.processEvents()
            f.pool.run_next()
            self.application.processEvents()
            self.assertTrue(f.panel.advice_runner.active)
            f.panel.preview_text.setPlainText(f.group.alternate_sample_text)
            f.panel.preview_text.setPlainText(f.group.sample_text)
            self.drain(f)
            self.assertEqual(f.transcript.call_count, 2)
            self.assertNotIn("No text mismatch", f.panel.voice_reference.itemText(1))
            self.assertEqual(
                f.panel._content_checks[f.candidates[1].source_id].advice.status,
                "no_asr_mismatch",
            )

    def test_same_audio_with_changed_seed_cannot_reuse_admitted_evidence(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.ready.update(f.previews)
            f.panel.start(f.plan)
            self.drain(f)
            check = f.panel._content_checks[f.candidates[0].source_id]
            f.panel._content_checks[check.source_id] = replace(
                check, seed=check.seed + 1
            )
            f.panel._refresh_reference_advice()
            self.assertIn("Content not checked", f.panel.advice_details.toPlainText())
            self.assertTrue(f.panel.recommendation.isHidden())

    def test_background_failure_shows_reason_without_playing_or_saving(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.panel.start(f.plan, group_id=f.group.group_id)
            self.assertTrue(f.panel.status.isHidden())
            first = f.candidates[0]
            failed = {first.source_id}
            f.service.preparation_state.side_effect = (
                lambda _plan, _group, source, **_: (
                    "failed" if source in failed else "not-prepared"
                )
            )
            f.service.preparation_error.return_value = "Model assets are missing."
            f.panel._refresh_runtime()
            message = f.panel.status.toPlainText()
            self.assertFalse(f.panel.status.isHidden())
            self.assertIn("Model assets are missing", message)
            self.assertIn("retry", message)
            self.assertIn(
                "Model assets are missing", f.panel.reference_details.toPlainText()
            )
            f.panel.status.selectAll()
            f.panel.status.copy()
            self.assertEqual(self.application.clipboard().text(), message)
            f.panel.voice_reference.setCurrentIndex(1)
            self.assertTrue(f.panel.status.isHidden())
            f.panel.voice_reference.setCurrentIndex(0)
            self.assertIn("Model assets are missing", f.panel.status.toPlainText())
            f.panel._set_status("Unable to save choices: disk full")
            f.panel._refresh_runtime()
            self.assertIn("disk full", f.panel.status.toPlainText())
            f.panel._set_status(message)
            failed.clear()
            f.panel._refresh_runtime()
            self.assertTrue(f.panel.status.isHidden())
            self.assertNotIn(
                "Model assets are missing", f.panel.reference_details.toPlainText()
            )
            f.service.generate.assert_not_called()
            f.player.play_bytes.assert_not_called()
            f.decisions.remember_many.assert_not_called()

    def test_failed_recognition_is_not_retried_by_runtime_timer(self):
        with TemporaryDirectory() as directory:
            f = self.fixture(Path(directory))
            f.ready.update(f.previews)
            f.transcript.side_effect = RuntimeError("Checker unavailable")
            f.panel.start(f.plan)
            self.drain(f)
            attempts = f.transcript.call_count
            self.assertGreater(attempts, 0)
            for _ in range(5):
                f.panel._refresh_runtime()
                self.drain(f)
            self.assertEqual(f.transcript.call_count, attempts)
            self.assertIn("unavailable", f.panel.recommendation.text().lower())
            self.assertTrue(f.panel.a_play.isEnabled())
            f.player.play_bytes.assert_not_called()
            f.decisions.remember_many.assert_not_called()
