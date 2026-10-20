import json
import os
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent


class UiCatalogTest(unittest.TestCase):
    def test_ocr_review_catalog_uses_real_writes_and_recoverable_drafts(self):
        script = dedent("""
            from contextlib import ExitStack
            from PySide6.QtWidgets import QApplication
            from scripts.render_ui_catalog import (
                _ocr_review_story, _close_catalog_widget, _prepare_ocr_review_screenshot,
            )
            from tests.qt_task_fixtures import ManualThreadPool

            app = QApplication([])
            states = (
                "saving", "save-failure", "partial-save-failure", "stale-save",
                "close-pending", "next-sample", "complete", "draft-switched",
                "blank-source", "unavailable-image", "compact", "long-text",
                "compact-save-failure", "compact-save-failure-end",
                "compact-partial-save-failure", "compact-partial-save-failure-end",
                "compact-stale-save", "compact-stale-save-end",
            )
            for state in states:
                with ExitStack() as resources:
                    pool = ManualThreadPool()
                    dialog = _ocr_review_story(resources, state, pool, pool.run_next)
                    if state.startswith("compact-") and state.endswith("-end"):
                        dialog.show()
                        app.processEvents()
                        _prepare_ocr_review_screenshot(dialog, "ocr-review." + state, app)
                        for bar in (
                            dialog.status.verticalScrollBar(),
                            dialog.details_scroll.verticalScrollBar(),
                        ):
                            assert bar.maximum() > 0
                            assert bar.value() == bar.maximum()
                        assert "END OF ERROR" in dialog.status.toPlainText()
                    if state in {"saving", "close-pending"}:
                        assert dialog._write_active and len(pool.tasks) == 1
                        assert not dialog.resolve_button.isEnabled()
                        assert dialog.corrected_character.text() == "Marcus"
                    elif state == "stale-save":
                        assert not dialog.save_button.isEnabled()
                        assert not dialog.resolve_button.isEnabled()
                        assert "reopen" in dialog.status.toPlainText()
                    elif state in {"save-failure", "partial-save-failure"}:
                        assert dialog.save_button.isEnabled()
                        assert dialog.resolve_button.isEnabled()
                        assert dialog.corrected_text.toPlainText() == "Hello timekeeper."
                    elif state == "draft-switched":
                        dialog.sample_list.setCurrentRow(1)
                        assert dialog.corrected_character.text() == "Northern Keeper"
                        assert dialog.scope.currentData() is None
                    elif state == "blank-source":
                        assert not dialog.save_button.isEnabled()
                        assert dialog.resolve_button.isEnabled()
                    elif state == "compact":
                        assert dialog.font().pointSize() == 22
                        for control in (
                            dialog.sample_list, dialog.corrected_text, dialog.scope,
                            dialog.save_button, dialog.resolve_button,
                        ):
                            assert control.font().pointSize() == 22
                    elif state == "long-text":
                        assert "<b>Stay here</b>" in dialog.source_text.toPlainText()
                        dialog.show()
                        app.processEvents()
                        _prepare_ocr_review_screenshot(dialog, "ocr-review.long-text", app)
                        for editor in (dialog.source_text, dialog.corrected_text):
                            assert editor.height() > editor.minimumHeight()
                            assert editor.maximumHeight() >= editor.height()
                    if pool.tasks:
                        pool.run_next()
                        app.processEvents()
                    _close_catalog_widget(dialog)
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_catalog_inputs_match_selected_lines_and_captured_files(
        self,
    ) -> None:
        from vntts_artifacts.file_integrity import sha256_file
        from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue
        from vntts_artifacts.voice_manifest import load_voice_manifest

        from scripts.render_ui_catalog import (
            _catalog_generation_input,
            _catalog_offline_content,
        )
        from vntts.pregeneration_setup import (
            PregenerationJobStore,
            load_verified_story_index_document,
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = _catalog_offline_content(root)
            store = PregenerationJobStore(root / "jobs")
            job = store.create_or_resume(content, ("chapter-7", "chapter-8"))
            self.assertEqual(store.load(job.job_id), job)
            prepared = _catalog_generation_input(job)
            story = load_verified_story_index_document(
                prepared.story_index, prepared.story_index_sha256
            )
            queue = VoiceGenerationQueue.load(prepared.queue)
            load_voice_manifest(prepared.voice_manifest)
            self.assertEqual(
                {r.line_id for r in story.records}, set(job.selected_line_ids)
            )
            expected = {
                r.line_id for r in story.records if r.source_audio_status == "absent"
            }
            self.assertEqual({item.line_id for item in queue.items}, expected)
            self.assertEqual(len(story.records), job.estimate.selected_lines)
            self.assertEqual(
                (job.estimate.selected_lines, job.estimate.original_audio_lines),
                (42, 8),
            )
            self.assertEqual(
                (len(queue.items), prepared.queue_items, prepared.ready_items),
                (34, 34, 34),
            )
            self.assertEqual(sha256_file(prepared.queue), prepared.queue_sha256)
            self.assertEqual(
                sha256_file(prepared.voice_manifest), prepared.voice_manifest_sha256
            )

    def test_catalog_resource_failures_keep_primary_error_and_release_files(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        script = dedent(r"""
            from contextlib import ExitStack, contextmanager
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PySide6.QtWidgets import QApplication
            from shiboken6 import isValid
            from scripts import render_ui_catalog as catalog

            app = QApplication.instance() or QApplication([])
            directories = []
            closed = []
            temporary = catalog.temporary_directory
            close = catalog._close_catalog_widget

            @contextmanager
            def tracked_directory(**kwargs):
                with temporary(**kwargs) as directory:
                    directories.append(Path(directory))
                    yield directory

            primary = ValueError("screenshot preparation failed")
            def fail_screenshot(*args):
                raise primary

            def fail_cleanup(widget):
                assert all(path.exists() for path in directories)
                close(widget)
                assert not isValid(widget)
                closed.append(widget)
                assert all(path.exists() for path in directories)
                raise OSError("widget cleanup failure")

            document = catalog.load_catalog(catalog.DEFAULT_CATALOG)
            surface = next(item for item in document["surfaces"] if item["id"] == "settings")
            document["surfaces"] = [{**surface, "related": [], "stories": [surface["stories"][0]]}]
            with TemporaryDirectory() as output:
                with patch.object(catalog, "temporary_directory", tracked_directory), patch.object(catalog, "_prepare_catalog_screenshot", fail_screenshot), patch.object(catalog, "_close_catalog_widget", fail_cleanup):
                    try:
                        catalog._render_stories(document, Path(output), "settings")
                    except ValueError as error:
                        assert error is primary
                        assert any("widget cleanup failure" in note for note in error.__notes__)
                    else:
                        raise AssertionError("screenshot failure was lost")
            assert len(closed) == 1
            assert directories and all(not path.exists() for path in directories)

            directories.clear()
            construction = ValueError("dialog construction failed")
            with patch.object(catalog, "temporary_directory", tracked_directory), patch("vntts.authoring.source_reference_quality_ui.SourceReferenceQualityDialog", side_effect=construction):
                try:
                    with ExitStack() as resources:
                        catalog._source_reference_review("pending", resources)
                except ValueError as error:
                    assert error is construction
                else:
                    raise AssertionError("construction failure was lost")
            assert directories and all(not path.exists() for path in directories)
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_history_catalog_renders_every_event_driven_state(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "dialogue-history",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets/dialogue-history.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    f"dialogue-history.{state}"
                    for state in (
                        "empty",
                        "populated",
                        "filtered",
                        "no-matches",
                        "speaking-filtered",
                        "stopping",
                        "speaking",
                        "failure",
                        "long-text",
                        "compact",
                        "compact-failure",
                    )
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater((output / story["screenshot"]).stat().st_size, 0)

    def test_authoring_review_catalog_covers_focus_and_long_names(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "authoring-workbench",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for state in (
                "review",
                "review-play-focus",
                "review-long-names",
                "review-long-names-second",
                "review-long-names-details",
                "review-long-names-second-details",
                "compact",
                "compact-actions",
                "load-failure",
                "load-failure-22pt",
                "load-failure-22pt-end",
                "recovered",
                "selection-refresh-pending",
                "selection-refresh-active",
                "stopping",
                "stopping-22pt",
                "stopping-close",
                "stopping-close-22pt",
                "stopping-close-22pt-details",
                "technical-details",
            ):
                path = output / "screenshots" / f"authoring-workbench.{state}.png"
                self.assertGreater(path.stat().st_size, 0)
            packet = json.loads(
                (output / "review-packets" / "authoring-workbench.json").read_text()
            )
            self.assertEqual(packet["target"]["id"], "authoring-workbench")
            self.assertEqual(
                {surface["id"] for surface in packet["related_surfaces"]},
                {"cohort-review", "missing-voice-review", "failed-reference-review"},
            )
            self.assertNotIn("source_path", json.dumps(packet))

    def test_authoring_catalog_projects_real_failure_refresh_and_stopping_states(self):
        script = dedent("""
            from contextlib import ExitStack
            from unittest.mock import patch
            from PySide6.QtWidgets import QApplication
            from scripts.render_ui_catalog import (
                AUTHORING_CATALOG_ERROR, _authoring_workbench_story,
                _close_catalog_widget, _prepare_catalog_screenshot,
            )

            app = QApplication([])
            original_size = app.font().pointSize()
            states = (
                "load-failure", "load-failure-22pt", "load-failure-22pt-end",
                "recovered", "selection-refresh-pending", "selection-refresh-active",
                "stopping", "stopping-22pt", "technical-details",
                "stopping-close", "stopping-close-22pt",
                "stopping-close-22pt-details",
                "review-long-names-details", "review-long-names-second-details",
            )
            for state in states:
                with ExitStack() as resources:
                    dialog = _authoring_workbench_story(resources, state)
                    dialog.show()
                    _prepare_catalog_screenshot(dialog, "authoring-workbench." + state, app)
                    if "22pt" in state:
                        assert app.font().pointSize() == 22
                        assert dialog.font().pointSize() == 22
                        assert dialog.reload_authority.font().pointSize() == 22
                        assert dialog.stop_generation.font().pointSize() == 22
                        assert dialog.workbench_scroll.horizontalScrollBar().maximum() == 0
                        for action in (
                            dialog.reload_authority, dialog.choose_collections,
                            dialog.generate, dialog.retry_failed, dialog.stop_generation,
                        ):
                            assert action.width() >= action.minimumSizeHint().width()
                    if state.startswith("load-failure"):
                        assert dialog.summary is None
                        assert dialog.counts.text() == ""
                        assert dialog.workspace_error.isReadOnly()
                        assert dialog.workspace_error.toPlainText() == AUTHORING_CATALOG_ERROR
                        dialog.workspace_error.selectAll()
                        dialog.workspace_error.copy()
                        assert app.clipboard().text() == AUTHORING_CATALOG_ERROR
                        assert dialog.reload_authority.isEnabled()
                        assert not dialog.generate.isEnabled()
                        assert not dialog.retry_failed.isEnabled()
                        assert not dialog.previous_pending.isEnabled()
                        assert not dialog.next_pending.isEnabled()
                        if state.endswith("-end"):
                            bar = dialog.workspace_error.verticalScrollBar()
                            assert bar.maximum() > 0 and bar.value() == bar.maximum()
                    elif state == "recovered":
                        assert dialog.summary is not None
                        assert dialog.workspace_error.toPlainText() == ""
                        assert dialog.review_table.rowCount() == 1
                        assert dialog.review_play.isEnabled()
                    elif state.startswith("selection-refresh-"):
                        assert dialog._selected_collection_ids == ("source-only",)
                        assert "UPDATING" in dialog.status.text()
                        assert "READY" not in dialog.status.text()
                        assert not dialog.generate.isEnabled()
                        assert not dialog.retry_failed.isEnabled()
                        assert dialog._projection_active == state.endswith("active")
                        assert dialog._selection_refresh_pending == state.endswith("pending")
                        with patch.object(dialog, "_start_child") as start:
                            dialog.start_generation()
                            dialog.start_failed_retry()
                            start.assert_not_called()
                    elif state.startswith("stopping"):
                        assert "STOPPING" in dialog.status.text()
                        assert not dialog.stop_generation.isEnabled()
                        assert not dialog.generate.isEnabled()
                        dialog.stop_child()
                        dialog.process.terminate.assert_called_once_with()
                        viewport = dialog.workbench_scroll.viewport()
                        controls = (
                            (dialog.review_action_reason, dialog.review_play,
                             dialog.approve, dialog.reject_button)
                            if state.endswith("-details") else (dialog.stop_generation,)
                        )
                        for control in controls:
                            top = control.mapTo(viewport, control.rect().topLeft()).y()
                            assert 0 <= top and top + control.height() <= viewport.height()
                            assert control.height() >= control.heightForWidth(control.width())
                        if state.startswith("stopping-close"):
                            assert dialog.close_after_stop
                            assert not dialog.workbench_content.isEnabled()
                            assert not dialog.review_play.isEnabled()
                            assert not dialog.specialist_review.isEnabled()
                            assert dialog.review_action_reason.isVisibleTo(dialog)
                            assert dialog.review_action_reason.text() == "Closing: waiting for generation to stop"
                            dialog.process.start.assert_not_called()
                    elif state == "technical-details":
                        assert dialog.technical.isChecked()
                        assert not dialog.review_table.isColumnHidden(8)
                        assert dialog.process_log.toPlainText()
                    elif state.startswith("review-long-names"):
                        selected = dialog._selected_review_item()
                        assert selected.speaker in dialog.current_review.text()
                        assert selected.voice_character in dialog.current_review.text()
                        assert selected.text in dialog.review_text.text()
                        expected = "The Traveling Hotelier" if "second" in state else "The Keeper of the Northern Library"
                        assert selected.speaker == expected
                        viewport = dialog.workbench_scroll.viewport()
                        for control in (
                            dialog.current_review, dialog.review_text,
                            dialog.review_play, dialog.approve, dialog.reject_button,
                        ):
                            top = control.mapTo(viewport, control.rect().topLeft())
                            assert 0 <= top.y() and top.y() + control.height() <= viewport.height()
                            assert control.height() >= control.heightForWidth(control.width())
                    _close_catalog_widget(dialog)
                assert app.font().pointSize() == original_size
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_voice_route_catalog_covers_portraits_and_compact_actions(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "offline-preparation",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for state in (
                "voice-confirmation",
                "voice-confirmation-portraits",
                "voice-confirmation-mixed-portraits",
                "voice-confirmation-compact",
                "voice-confirmation-compact-scrolled",
                "voice-confirmation-compact-last-route",
            ):
                path = output / "screenshots" / f"offline-preparation.{state}.png"
                self.assertGreater(path.stat().st_size, 0)

    def test_legacy_reason_review_packet_covers_listening_failure_and_compact(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "legacy-reason-review",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "legacy-reason-review.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "legacy-reason-review.pending",
                    "legacy-reason-review.heard",
                    "legacy-reason-review.selected",
                    "legacy-reason-review.save-error",
                    "legacy-reason-review.publish-error",
                    "legacy-reason-review.compact",
                    "legacy-reason-review.compact-reasons",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_settings_catalog_uses_real_validation_and_recovery_actions(self):
        script = dedent("""
            import sys
            from contextlib import ExitStack
            from PySide6.QtWidgets import QApplication, QCheckBox, QLabel
            from scripts.render_ui_catalog import (
                _settings_story, _prepare_settings_screenshot, _close_catalog_widget,
            )

            app = QApplication([])
            platform = sys.platform
            states = (
                "shortcuts", "shortcuts-windows", "application", "advanced-speech",
                "basic-offline-moss", "advanced-offline-moss", "compact-capture",
                "compact-shortcuts", "compact-playback", "compact-application",
                "compact-advanced-speech-scrolled", "compact-validation-errors",
                "compact-validation-errors-end", "validation-recovered",
                "playback-speed-reset-needed", "playback-speed-reset",
            )
            for state in states:
                with ExitStack() as resources:
                    dialog = _settings_story(resources, state)
                    dialog.show()
                    app.processEvents()
                    _prepare_settings_screenshot(dialog, "settings." + state, app)
                    assert sys.platform == platform
                    assert dialog.rect().contains(dialog.save_button.mapTo(dialog, dialog.save_button.rect().center()))
                    if state.startswith("compact-"):
                        assert dialog.font().pointSize() == 22
                    if "validation-errors" in state:
                        summary = dialog.validation_summary
                        assert len(dialog.validation_errors()) >= 6
                        assert summary.isReadOnly() and summary.tabChangesFocus()
                        assert "Game pack: the selected file does not exist." in summary.toPlainText()
                        assert dialog.game_pack.text() not in summary.toPlainText()
                        assert summary.verticalScrollBar().maximum() > 0
                        assert dialog.focusWidget() is dialog.screenshot_directory
                        viewport = dialog.settings_scroll.viewport()
                        assert viewport.rect().contains(dialog.screenshot_directory.mapTo(viewport, dialog.screenshot_directory.rect().center()))
                        if state.endswith("-end"):
                            assert summary.verticalScrollBar().value() == summary.verticalScrollBar().maximum()
                        else:
                            assert summary.verticalScrollBar().value() == 0
                    elif state == "validation-recovered":
                        assert not dialog.validation_errors()
                        assert dialog.validation_summary.isHidden()
                        assert not dialog.validation_summary.toPlainText()
                    elif "offline-moss" in state:
                        assert dialog.speech_backend.currentData() == "pocket-tts"
                        assert dialog.offline_speech_backend.currentData() == "moss-tts"
                        assert dialog.tts_language.isEnabled()
                        assert dialog.offline_tts_model.isHidden() == (state == "basic-offline-moss")
                    elif state.startswith("playback-speed"):
                        reset = state == "playback-speed-reset"
                        assert not dialog.speech_rate.isEnabled()
                        assert dialog.speech_rate.value() == (100 if reset else 125)
                        assert dialog.reset_speech_rate.isEnabled() == (not reset)
                        assert dialog.reset_speech_rate.text() == "Reset to 100%"
                    elif state in {"shortcuts", "shortcuts-windows", "application"}:
                        assert dialog.advanced_settings.isHidden()
                        if state == "shortcuts-windows":
                            assert any("when saved" in label.text() and label.isVisibleTo(dialog) for label in dialog.findChildren(QLabel))
                    elif state == "advanced-speech":
                        assert not dialog.tts_model.isHidden()
                        assert not dialog.offline_tts_model.isHidden()
                    if state == "compact-advanced-speech-scrolled":
                        bar = dialog.settings_scroll.verticalScrollBar()
                        assert bar.maximum() > 0 and bar.value() == bar.maximum()
                    _close_catalog_widget(dialog)
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_settings_packet_covers_sections_disclosure_and_compact_state(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "settings",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "settings.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "settings.speech-and-voices",
                    "settings.shortcuts",
                    "settings.capture",
                    "settings.capture-window",
                    "settings.advanced-capture",
                    "settings.playback",
                    "settings.playback-advanced",
                    "settings.application",
                    "settings.checkbox-focus",
                    "settings.advanced-speech",
                    "settings.compact-capture",
                    "settings.compact-shortcuts",
                    "settings.compact-playback",
                    "settings.compact-application",
                    "settings.compact-advanced-speech",
                    "settings.compact-advanced-speech-scrolled",
                    "settings.validation-error",
                    "settings.shortcuts-windows",
                    "settings.basic-offline-moss",
                    "settings.advanced-offline-moss",
                    "settings.compact-validation-errors",
                    "settings.compact-validation-errors-end",
                    "settings.validation-recovered",
                    "settings.playback-speed-reset-needed",
                    "settings.playback-speed-reset",
                },
            )
            self.assertGreater(
                (output / "screenshots" / "settings.checkbox-focus.png").stat().st_size,
                0,
            )

    def test_readiness_packet_covers_distinct_recovery_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "readiness",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "readiness.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "readiness.loading",
                    "readiness.cancelled",
                    "readiness.probe-failed",
                    "readiness.blocked",
                    "readiness.audio-error",
                    "readiness.warning-selected",
                    "readiness.warning-only",
                    "readiness.non-english-ocr",
                    "readiness.ready",
                    "readiness.full-details",
                    "readiness.long-error",
                    "readiness.long-error-expanded",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_onboarding_packet_covers_first_run_and_compact_recovery(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "onboarding",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "onboarding.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "onboarding.game-window",
                    "onboarding.screen-region",
                    "onboarding.game-window-error",
                    "onboarding.game-window-error-compact",
                    "onboarding.diagnostics-loading",
                    "onboarding.diagnostics-error",
                    "onboarding.calibration",
                    "onboarding.calibration-complete",
                    "onboarding.diagnostics-multiple",
                    "onboarding.diagnostics-multiple-last",
                    "onboarding.test",
                    "onboarding.test-success",
                    "onboarding.compact-error",
                    "onboarding.diagnostics-complete",
                    "onboarding.diagnostics-warning",
                    "onboarding.diagnostics-audio-error",
                    "onboarding.diagnostics-exception-end",
                    "onboarding.compact-error-end",
                    "onboarding.diagnostics-cancelled",
                    "onboarding.diagnostics-exception",
                    "onboarding.diagnostics-install",
                    "onboarding.diagnostics-disk-space",
                    "onboarding.test-running",
                    "onboarding.test-cancelling",
                    "onboarding.test-failure",
                    "onboarding.test-cancelled",
                    "onboarding.test-long-error",
                    "onboarding.test-long-error-end",
                    "onboarding.first-run-welcome",
                    "onboarding.first-run-test-long-error",
                    "onboarding.first-run-test-long-error-end",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_macos_permission_packet_covers_known_unknown_and_compact_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "macos-permissions",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "macos-permissions.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "macos-permissions.denied",
                    "macos-permissions.granted",
                    "macos-permissions.unavailable",
                    "macos-permissions.capture-ready",
                    "macos-permissions.accessibility-unknown",
                    "macos-permissions.requesting",
                    "macos-permissions.status-failure",
                    "macos-permissions.request-failure",
                    "macos-permissions.settings-failure",
                    "macos-permissions.settings-opened",
                    "macos-permissions.compact-status-failure",
                    "macos-permissions.compact-requesting",
                    "macos-permissions.compact-request-failure",
                    "macos-permissions.compact-settings-failure",
                    "macos-permissions.compact-settings-opened",
                    "macos-permissions.compact",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_asset_packet_covers_voice_and_model_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "asset-manager",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "asset-manager.json").read_text()
            )
            story_ids = {story["id"] for story in packet["target"]["stories"]}
            self.assertTrue(
                {
                    "asset-manager.voices-empty",
                    "asset-manager.voices-manifest",
                    "asset-manager.voices-validation",
                    "asset-manager.voices-error",
                    "asset-manager.model-unchecked",
                    "asset-manager.model-verified",
                    "asset-manager.model-download",
                    "asset-manager.model-failure",
                    "asset-manager.compact-voices",
                    "asset-manager.compact-model",
                    "asset-manager.voice-importing",
                    "asset-manager.voice-import-failure",
                    "asset-manager.voice-import-success",
                    "asset-manager.voice-import-close-pending",
                    "asset-manager.model-verifying",
                    "asset-manager.model-cancel-pending",
                    "asset-manager.compact-voices-long-error",
                    "asset-manager.compact-model-long-error",
                }
                <= story_ids
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_diagnostics_catalog_covers_split_ocr_images_and_recovery(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "diagnostics",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "diagnostics.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "diagnostics.empty",
                    "diagnostics.snapshot",
                    "diagnostics.missing-images",
                    "diagnostics.technical",
                    "diagnostics.refreshing",
                    "diagnostics.warning",
                    "diagnostics.long-warning",
                    "diagnostics.timeout",
                    "diagnostics.compact",
                    "diagnostics.compact-technical",
                    "diagnostics.enlarged-dialogue",
                    "diagnostics.enlarged-speaker",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )
            for story_id in (
                "diagnostics.compact",
                "diagnostics.compact-technical",
            ):
                screenshot = output / "screenshots" / f"{story_id}.png"
                self.assertEqual(
                    struct.unpack(">II", screenshot.read_bytes()[16:24]),
                    (620, 460),
                )

    def test_game_profile_packet_covers_selection_and_settings_edge_cases(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "game-profiles",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "game-profiles.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "game-profiles.empty",
                    "game-profiles.active",
                    "game-profiles.other-selected",
                    "game-profiles.compact",
                    "game-profiles.missing-window",
                    "game-profiles.long-values",
                    "game-profiles.pack-backed",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_voice_import_packet_covers_empty_long_and_compact_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "voice-import",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "voice-import.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "voice-import.empty",
                    "voice-import.selected",
                    "voice-import.long-values",
                    "voice-import.compact",
                    "voice-import.compact-overflow",
                    "voice-import.compact-overflow-end",
                    "voice-import.duplicate-filenames",
                    "voice-import.invalid-name",
                    "voice-import.invalid-alias",
                    "voice-import.compact-invalid-alias",
                    "voice-import.replacement",
                    "voice-import.compact-replacement",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_calibration_packet_covers_selection_and_review_outcomes(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "calibration",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "calibration.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "calibration.overlay-empty",
                    "calibration.overlay-selected",
                    "calibration.overlay-top-selected",
                    "calibration.overlay-save-failure",
                    "calibration.overlay-keyboard-selection",
                    "calibration.overlay-compact-save-error",
                    "calibration.review-loading",
                    "calibration.review-recognized",
                    "calibration.review-empty",
                    "calibration.review-unreadable-name",
                    "calibration.review-failure-compact",
                    "calibration.review-failure",
                    "calibration.review-compact",
                    "calibration.review-long-compact",
                    "calibration.review-long-compact-scrolled",
                    "calibration.review-long-enlarged",
                    "calibration.review-long-enlarged-scrolled",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_compact_calibration_story_scrolls_to_last_dialogue_line(self):
        root = Path(__file__).resolve().parents[1]
        script = dedent("""
            from PIL import Image
            from PySide6.QtCore import QThreadPool
            from PySide6.QtWidgets import QApplication
            from scripts.render_ui_catalog import (
                _calibration_review_story, _close_catalog_widget,
                _prepare_catalog_screenshot,
            )

            class IdlePool(QThreadPool):
                def start(self, *args, **kwargs):
                    pass

            app = QApplication.instance() or QApplication([])
            pool = IdlePool()
            dialog = _calibration_review_story(
                "review-long-compact-scrolled", Image.new("RGB", (800, 450)), pool
            )
            try:
                dialog.show()
                app.processEvents()
                app.processEvents()
                assert dialog.size().toTuple() == (620, 430)
                assert dialog.font().pointSize() == 18
                text = dialog.result_text
                scrollbar = text.verticalScrollBar()
                assert scrollbar.maximum() > 0
                cursor = text.document().find("END OF DIALOGUE")
                assert not cursor.isNull()
                assert text.cursorRect(cursor).top() >= text.viewport().height()
                _prepare_catalog_screenshot(
                    dialog, "calibration.review-long-compact-scrolled", app
                )
                assert scrollbar.value() > 0
                assert text.viewport().rect().contains(text.cursorRect(cursor))
                for button in (dialog.save_button, dialog.retry_button, dialog.cancel_button):
                    rect = button.rect().translated(
                        button.mapTo(dialog, button.rect().topLeft())
                    )
                    assert dialog.rect().contains(rect)
            finally:
                _close_catalog_widget(dialog)
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dashboard_packet_renders_active_and_recovery_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "dashboard",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "dashboard.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "dashboard.stories-ready",
                    "dashboard.reading-loading",
                    "dashboard.reading-active",
                    "dashboard.reading-empty",
                    "dashboard.reading-setup-error",
                    "dashboard.reading-live-tts",
                    "dashboard.auto-advance-off",
                    "dashboard.auto-advance-unavailable",
                    "dashboard.auto-advance-manual",
                    "dashboard.auto-advance-unavailable-compact",
                    "dashboard.auto-advance-focus",
                    "dashboard.auto-advance-settings-route",
                    "dashboard.reading-waiting",
                    "dashboard.reading-paused",
                    "dashboard.reading-stopped",
                    "dashboard.reading-technical",
                    "dashboard.story-position-recovery",
                    "dashboard.reading-long-values",
                    "dashboard.reading-long-capture",
                    "dashboard.reading-long-values-scrolled",
                    "dashboard.story-position-manual",
                    "dashboard.setup-expanded",
                },
            )
            for story_id in (
                "dashboard.reading-paused",
                "dashboard.reading-long-values",
                "dashboard.reading-long-capture",
                "compact-controller.sequence-recovery",
                "compact-controller.starting",
                "compact-controller.ready",
                "compact-controller.reading",
                "compact-controller.paused",
                "compact-controller.speaking",
                "compact-controller.starting-reading",
                "compact-controller.stopping",
                "compact-controller.unavailable",
                "compact-controller.warning",
                "compact-controller.long-error",
                "compact-controller.long-error-end",
                "compact-controller.sequence-choice",
                "compact-controller.scaled-warning",
                "compact-controller.scaled-warning-end",
                "compact-controller.scaled-sequence-recovery",
            ):
                self.assertGreater(
                    (output / "screenshots" / f"{story_id}.png").stat().st_size,
                    0,
                )

    def test_story_match_recovery_renders_both_modes_and_font_sizes(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "story-match-recovery",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "story-match-recovery.json").read_text()
            )
            self.assertEqual(packet["target"]["id"], "story-match-recovery")
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "story-match-recovery.awaiting-choice",
                    "story-match-recovery.larger-text",
                    "story-match-recovery.sequence-mode",
                    "story-match-recovery.sequence-larger-text",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_catalog_renders_real_states_and_focused_astra_packet(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "voice-editor",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output / "index.html").is_file())
            for story_id in (
                "dashboard.stories-ready",
                "dashboard.reading-active",
                "dashboard.setup-expanded",
                "voice-editor.narrator",
                "voice-editor.live-recovery",
                "voice-editor.one-shot-assignment",
                "voice-editor.preview-generating",
                "voice-editor.preview-failure",
                "voice-editor.long-values",
                "voice-editor.saved-return",
                "settings.speech-and-voices",
                "settings.validation-error",
                "unknown-speaker-prompt.awaiting-choice",
                "unknown-speaker-prompt.long-name",
            ):
                self.assertGreater(
                    (output / "screenshots" / f"{story_id}.png").stat().st_size,
                    0,
                )

            packet = json.loads(
                (output / "review-packets" / "voice-editor.json").read_text()
            )
            self.assertEqual(packet["target"]["id"], "voice-editor")
            self.assertEqual(packet["target"]["canonical_owner"], "voice-editor")
            self.assertIn(
                "settings", {surface["id"] for surface in packet["related_surfaces"]}
            )
            self.assertNotIn("source_path", json.dumps(packet))
            self.assertEqual(
                set(packet["target"]),
                {
                    "id",
                    "title",
                    "family",
                    "audience",
                    "mission",
                    "canonical_owner",
                    "related",
                    "contracts",
                    "stories",
                },
            )

            obsolete = output / "screenshots" / "removed-story.png"
            obsolete.write_bytes(b"stale")

            second_result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "character-story-review",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertFalse(obsolete.exists())
            screenshots = {path.name for path in (output / "screenshots").glob("*.png")}
            self.assertEqual(
                {
                    name
                    for name in screenshots
                    if name.startswith("character-story-review.")
                },
                {
                    "character-story-review.pending.png",
                    "character-story-review.compact.png",
                    "character-story-review.compact-actions.png",
                },
            )
            self.assertFalse(
                any(name.startswith("source-voice-mapping.") for name in screenshots)
            )


if __name__ == "__main__":
    unittest.main()
