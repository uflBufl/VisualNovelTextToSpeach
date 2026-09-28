#!/usr/bin/env python3
"""Render a small Storybook-like catalog from real VNTTS Qt widgets."""

from __future__ import annotations

import argparse
import html
import json
import os
import socket
import sys
import time
import wave
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "ui-catalog.json"
DEFAULT_OUTPUT = PROJECT_ROOT / ".codex" / "ui-catalog"


def load_catalog(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("ui-catalog.json must use schema_version 1")
    contracts = document.get("contracts")
    surfaces = document.get("surfaces")
    if not isinstance(contracts, dict) or not isinstance(surfaces, list):
        raise ValueError("catalog requires contracts and surfaces")

    surface_ids = _validate_surface_definitions(surfaces, contracts)
    _validate_surface_links(surfaces, surface_ids)
    return document


def _validate_surface_definitions(
    surfaces: list[dict[str, Any]], contracts: dict[str, Any]
) -> set[str]:
    surface_ids: set[str] = set()
    story_ids: set[str] = set()
    for surface in surfaces:
        surface_id = _required_text(surface, "id")
        if surface_id in surface_ids:
            raise ValueError(f"duplicate surface id: {surface_id}")
        surface_ids.add(surface_id)
        for field in ("title", "family", "audience", "mission", "canonical_owner"):
            _required_text(surface, field)
        for contract_id in surface.get("contracts", []):
            if contract_id not in contracts:
                raise ValueError(
                    f"{surface_id} references unknown contract: {contract_id}"
                )
        for story in surface.get("stories", []):
            story_id = _required_text(story, "id")
            if story_id in story_ids:
                raise ValueError(f"duplicate story id: {story_id}")
            story_ids.add(story_id)
            _required_text(story, "title")
            _required_text(story, "state")
    return surface_ids


def _validate_surface_links(
    surfaces: list[dict[str, Any]], surface_ids: set[str]
) -> None:
    for surface in surfaces:
        surface_id = surface["id"]
        for related_id in surface.get("related", []):
            if related_id not in surface_ids:
                raise ValueError(
                    f"{surface_id} references unknown related surface: {related_id}"
                )
        owner = surface["canonical_owner"]
        if owner not in surface_ids:
            raise ValueError(f"{surface_id} references unknown owner: {owner}")


def _required_text(value: dict[str, Any], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"catalog field {field!r} must be non-empty text")
    return result


def _render_stories(
    catalog: dict[str, Any], output: Path, selected_surface: str | None
) -> dict[str, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(PROJECT_ROOT))

    from PIL import Image, ImageDraw, ImageFont
    from PySide6.QtCore import QPoint, QSettings, QSignalBlocker, QTimer
    from PySide6.QtGui import QColor, QPalette, QTextCursor
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication
    from r1999extractor.story_voice_candidates import REPORT_SCHEMA, REPORT_VERSION
    from r1999extractor.story_voice_review_ui import StoryVoiceReviewDialog
    from vntts_artifacts.file_integrity import sha256_file

    from tests.test_authoring_cohort_review import create_pending_cohort_workspace
    from tests.test_authoring_failure_reference_audit import FailureReferenceAuditTest
    from tests.test_authoring_legacy_reason_review import _legacy_bad_fixture
    from tests.test_authoring_listening import FakePlayback, write_model_reports
    from tests.test_authoring_missing_voice_reuse_review import (
        create_missing_voice_reuse_review_fixture,
    )
    from tests.test_authoring_source_reference_quality_ui import write_quality_session
    from tests.test_authoring_terminal_conflict_review_ui import (
        TerminalConflictReviewUiTest,
    )
    from tests.test_authoring_workbench import create_test_workspace
    from vntts.app import (
        SettingsDialog,
        build_story_match_recovery_prompt,
        build_unknown_speaker_prompt,
    )
    from vntts.asset_ui import AssetManagerDialog, VoiceImportDialog
    from vntts.authoring.cohort_bundle import build_cohort_review_bundle
    from vntts.authoring.cohort_bundle_ui import CohortReviewBundleDialog
    from vntts.authoring.failure_reference_audit import publish_failure_reference_audit
    from vntts.authoring.failure_reference_audit_ui import FailureReferenceAuditDialog
    from vntts.authoring.generation_lease import process_started_at
    from vntts.authoring.legacy_reason_review import build_legacy_reason_review
    from vntts.authoring.legacy_reason_review_ui import LegacyReasonReviewDialog
    from vntts.authoring.listening import (
        create_listening_session_from_reports,
        record_trial_preference,
    )
    from vntts.authoring.listening_ui import ModelListeningDialog
    from vntts.authoring.missing_voice_reuse_review import (
        build_missing_voice_reuse_review,
    )
    from vntts.authoring.missing_voice_reuse_review_ui import (
        MissingVoiceReuseReviewDialog,
    )
    from vntts.authoring.source_reference_quality_ui import (
        SourceReferenceQualityDialog,
    )
    from vntts.authoring.terminal_conflict_review import (
        record_terminal_conflict_decision,
    )
    from vntts.authoring.terminal_conflict_review_ui import (
        TerminalConflictReviewDialog,
    )
    from vntts.authoring.workbench_ui import AuthoringWorkbenchDialog
    from vntts.calibration import CalibrationReviewDialog, DialogRegionOverlay
    from vntts.controller import LiveSequenceStatus
    from vntts.dashboard_ui import (
        CompactController,
        ControlDashboard,
        RuntimeControlState,
    )
    from vntts.diagnostics import DiagnosticSnapshot
    from vntts.diagnostics_ui import DiagnosticsDialog
    from vntts.game_content_importer import ImporterAvailability
    from vntts.game_narrator_ui import GameNarratorDialog
    from vntts.history import DialogueHistory
    from vntts.history_ui import DialogueHistoryDialog
    from vntts.macos_ui import MacOSPermissionsDialog
    from vntts.ocr import DialogRegion, OCRResult, UncertainFrameRecorder
    from vntts.ocr_corrections import OCRCorrectionStore
    from vntts.ocr_corrections_ui import OCRCorrectionsDialog
    from vntts.ocr_review_ui import OCRReviewDialog
    from vntts.onboarding import DiagnosticResult, OnboardingDiagnostics
    from vntts.onboarding_ui import OnboardingWizard
    from vntts.pregeneration_generation import OfflineGenerationProgress
    from vntts.pregeneration_setup import (
        ContentDiscovery,
        GameContent,
        PregenerationJobStore,
        StorySelection,
    )
    from vntts.pregeneration_ui import OfflineAudioPreparationDialog
    from vntts.pregeneration_voices import VoiceCandidate, VoiceGroup, VoicePlan
    from vntts.profiles import GameProfile, GameProfileStore
    from vntts.profiles_ui import GameProfilesDialog
    from vntts.readiness_ui import ReadinessDialog
    from vntts.settings import AppSettings
    from vntts.support_ui import SupportCenterDialog
    from vntts.voice_library import VoiceLibrary

    app = QApplication.instance() or QApplication(["vntts-ui-catalog"])
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#272727"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#151515"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#303030"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#a8a8a8"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#555555"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f0f0f0"))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#929292")
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#929292")
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor("#929292")
    )
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#148a2b"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)

    settings = AppSettings(
        onboarding_completed=True,
        speech_backend="moss-tts",
        tts_profile="stable",
        game_window_title="Reverse: 1999",
    )
    screenshots = output / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    for screenshot in screenshots.glob("*.png"):
        screenshot.unlink()

    def write_silent_wav(path: Path) -> None:
        with wave.open(str(path), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16_000)
            target.writeframes(b"\x00\x00" * 1_600)

    def dashboard_stories() -> Any:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.set_status("Ready. Choose a prepared story or start live reading.")
        dashboard.set_ready(True)
        dashboard.show_stories()
        return dashboard

    def dashboard_reading() -> Any:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.set_status("Reading the selected game window.")
        dashboard.set_dialogue(
            "Believer IV",
            "They at least paid with their lives. What about you?",
        )
        dashboard.set_runtime_controls(
            RuntimeControlState(
                ready=True,
                live=True,
                speaking=True,
                queued=True,
                replayable=True,
            )
        )
        dashboard.set_live(True)
        dashboard.set_speech_identity(settings, narrator="Centurion")
        dashboard.voice.setText("Believer IV voice")
        dashboard.audio_source.setText("Live TTS")
        dashboard.speech_runtime.setText("Compute: Apple GPU · model loaded")
        dashboard.moss_runtime_button.setText("Unload OpenMOSS")
        dashboard.moss_runtime_button.setEnabled(False)
        dashboard.show_reading()
        return dashboard

    def dashboard_setup() -> Any:
        dashboard = dashboard_stories()
        dashboard.setup_more_button.setChecked(True)
        return dashboard

    def dashboard_loading() -> Any:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.show_reading()
        dashboard.set_status("Loading the reading engine and voices.")
        dashboard.set_loading(True)
        return dashboard

    def dashboard_waiting() -> Any:
        dashboard = dashboard_reading()
        dashboard.set_status("Waiting for dialogue in the selected game window.")
        dashboard.set_runtime_controls(RuntimeControlState(ready=True, live=True))
        return dashboard

    def dashboard_paused() -> Any:
        dashboard = dashboard_reading()
        dashboard.set_status("Reading paused. Resume to hear the next dialogue.")
        dashboard.set_runtime_controls(
            RuntimeControlState(
                ready=True, live=True, paused=True, queued=True, replayable=True
            )
        )
        dashboard.set_paused(True)
        return dashboard

    def dashboard_stopped() -> Any:
        dashboard = dashboard_reading()
        dashboard.set_status("Reading stopped. The last dialogue remains available.")
        dashboard.set_runtime_controls(RuntimeControlState(ready=True, replayable=True))
        dashboard.set_live(False)
        return dashboard

    def dashboard_technical() -> Any:
        dashboard = dashboard_reading()
        dashboard.details_toggle.setChecked(True)
        return dashboard

    def dashboard_story_recovery() -> Any:
        dashboard = dashboard_reading()
        dashboard.set_sequence_status(
            LiveSequenceStatus(
                "audio-manual",
                "desynchronized",
                chapter="7",
                story_title="The Fourth Story",
                sequence=12,
                recovery_required=True,
                guidance="Current line does not match the saved story position.",
            )
        )
        return dashboard

    def dashboard_story_manual() -> Any:
        dashboard = dashboard_reading()
        dashboard.set_sequence_status(
            LiveSequenceStatus(
                "audio-manual",
                "locked",
                chapter="7",
                story_title="The Fourth Story",
                sequence=12,
            )
        )
        return dashboard

    def dashboard_long_values() -> Any:
        dashboard = dashboard_reading()
        dashboard.resize(620, 440)
        font = dashboard.font()
        font.setPointSize(font.pointSize() + 4)
        dashboard.setFont(font)
        dashboard.set_dialogue(
            "The Very Long Name of the Fourth Believer from the Northern District",
            "They at least paid with their lives. What about you? " * 5,
        )
        return dashboard

    def dashboard_long_values_scrolled() -> Any:
        dashboard = dashboard_long_values()
        dashboard.voice.setText(
            "Believer IV voice from a long user-provided reference recording"
        )
        dashboard.audio_source.setText(
            "Prepared recording for Chapter 7, line 12, with fallback to live speech"
        )
        QTimer.singleShot(
            0,
            lambda: dashboard.content_scroll.verticalScrollBar().setValue(
                max(0, dashboard.content_scroll.verticalScrollBar().maximum() - 45)
            ),
        )
        return dashboard

    def compact_sequence_recovery() -> Any:
        compact = CompactController(platform="win32")
        compact.set_runtime_controls(
            RuntimeControlState(ready=True, live=True, paused=True)
        )
        compact.set_live(True)
        compact.set_paused(True)
        compact.set_status("Paused at an unmatched story line.")
        compact.set_sequence_status(
            LiveSequenceStatus(
                "audio-manual",
                "desynchronized",
                recovery_required=True,
                expected_candidate_count=0,
            )
        )
        return compact

    def dashboard_voice_saved() -> Any:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.set_status(
            "Voice saved for Narrator: Believer IV. Prepared recordings are unchanged."
        )
        dashboard.set_speech_identity(settings, narrator="Believer IV")
        dashboard.set_ready(True)
        dashboard.show_voices()
        return dashboard

    def settings_speech() -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        reference = Path(temporary.name) / "reference.wav"
        reference.touch()
        dialog = SettingsDialog(
            settings.updated(
                tts_speaker_wav=str(reference),
                screenshot_directory="/Users/Player/Screenshots",
                ocr_diagnostics_directory="/Users/Player/OCR Diagnostics",
            ),
            voice_library=VoiceLibrary(Path(temporary.name) / "voice-library"),
        )
        dialog._catalog_temporary_directory = temporary
        dialog.resize(760, 600)
        dialog.section_navigation.setCurrentIndex(2)
        return dialog

    def settings_validation_error() -> Any:
        dialog = settings_speech()
        dialog.advanced_narrator.setChecked(True)
        dialog.narrator_reference.setText("/missing/voice-reference.wav")
        dialog.validate_and_accept()
        return dialog

    def settings_section(
        index: int, *, advanced: bool = False, compact: bool = False
    ) -> Any:
        dialog = settings_speech()
        dialog.section_navigation.setCurrentIndex(index)
        dialog.advanced_settings.setChecked(advanced)
        if compact:
            dialog.resize(620, 500)
            font = dialog.font()
            font.setPointSize(font.pointSize() + 4)
            dialog.setFont(font)
        return dialog

    def settings_capture_window() -> Any:
        dialog = settings_section(1)
        dialog.capture_mode.setCurrentIndex(dialog.capture_mode.findData("window"))
        dialog.game_window.setCurrentText(
            "Reverse: 1999 - A Particularly Long Game Window Title"
        )
        return dialog

    def settings_checkbox_focus() -> Any:
        dialog = settings_section(4)
        QTimer.singleShot(0, dialog.warm_up_voices.setFocus)
        return dialog

    def settings_compact_advanced_scrolled() -> Any:
        dialog = settings_section(2, advanced=True, compact=True)
        dialog.show()
        app.processEvents()
        scrollbar = dialog.settings_scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        return dialog

    def readiness(state: str) -> Any:
        class IdlePool:
            def start(self, _task: Any) -> None:
                pass

        dialog = ReadinessDialog(
            settings.updated(ocr_language="jpn")
            if state == "non-english-ocr"
            else settings,
            OnboardingDiagnostics(),
            thread_pool=IdlePool(),
        )
        if state == "loading":
            return dialog
        if state == "ready":
            results = (
                DiagnosticResult("Capture source", "ok", "Reverse: 1999"),
                DiagnosticResult("Tesseract OCR", "ok", "Version 5.5.0"),
                DiagnosticResult("Audio output", "ok", "Built-in speakers"),
                DiagnosticResult("Speech runtime", "ok", "Installed at /app/runtime"),
            )
        elif state == "warning-only":
            results = (
                DiagnosticResult("Capture source", "ok", "Reverse: 1999"),
                DiagnosticResult("Tesseract OCR", "ok", "Version 5.5.0"),
                DiagnosticResult("Audio output", "ok", "Built-in speakers"),
                DiagnosticResult(
                    "Character voices",
                    "warning",
                    "Narrator fallback will be used",
                    "voices",
                ),
            )
        else:
            results = (
                DiagnosticResult("Capture source", "ok", "Reverse: 1999"),
                DiagnosticResult(
                    "Tesseract OCR",
                    "error",
                    "Tesseract executable was not found in the application environment."
                    if state != "long-error"
                    else "Tesseract executable was not found at /Applications/"
                    + "An Example App/Resources/" * 8,
                    "external-ocr",
                ),
                DiagnosticResult(
                    "Audio output",
                    "error",
                    "No output device available",
                    "external-audio",
                ),
                DiagnosticResult(
                    "Character voices",
                    "warning",
                    "Narrator fallback will be used",
                    "voices",
                ),
            )
        dialog._checks_finished(results, None)
        if state == "audio-error":
            dialog.table.selectRow(2)
        elif state == "warning-selected":
            dialog.table.selectRow(3)
        elif state == "long-error":
            dialog.table.selectRow(1)
            dialog.resize(620, 440)
            font = dialog.font()
            font.setPointSize(font.pointSize() + 4)
            dialog.setFont(font)
        return dialog

    def onboarding(state: str) -> Any:
        wizard = OnboardingWizard(
            settings.updated(onboarding_completed=False),
            diagnostics=Mock(),
            window_loader=lambda: (),
            auto_discover_windows=False,
            reading_setup=True,
        )
        if state == "game-window-error":
            wizard.configuration_page.game_window.setCurrentText("")
            wizard.configuration_page.update_validation_summary()
        elif state == "screen-region":
            mode = wizard.configuration_page.capture_mode
            mode.setCurrentIndex(mode.findData("screen"))
        elif state.startswith("diagnostics") or state == "compact-error":
            wizard.diagnostics_page.runner.start = Mock()
            wizard.show_page(1)
            if state != "diagnostics-loading":
                wizard.diagnostics_page._checks_finished(
                    (
                        DiagnosticResult("Capture source", "ok", "Reverse: 1999"),
                        DiagnosticResult(
                            "Tesseract OCR",
                            "error",
                            "Tesseract executable was not found in the application environment.",
                            "external-ocr",
                        ),
                    ),
                    None,
                )
        elif state == "calibration":
            wizard.show_page(2)
        elif state in {"test", "test-success"}:
            wizard.calibration_page.calibrated = True
            wizard.show_page(3)
            if state == "test-success":
                wizard.test_page.set_result(
                    True,
                    "Success. Recognized Believer IV: The storm has passed.",
                )
        if state == "compact-error":
            wizard.resize(560, 440)
            font = wizard.font()
            font.setPointSize(font.pointSize() + 4)
            wizard.setFont(font)
        return wizard

    def macos_permissions(state: str) -> Any:
        status = {
            "screen_capture": state == "granted",
            "accessibility": state == "granted",
        }
        if state == "unavailable":
            status = {"screen_capture": None, "accessibility": None}
        dialog = MacOSPermissionsDialog(
            status_provider=lambda: status,
            screen_request=Mock(),
            accessibility_request=Mock(),
            url_opener=Mock(return_value=True),
        )
        if state == "compact":
            dialog.resize(620, 380)
            font = dialog.font()
            font.setPointSize(font.pointSize() + 4)
            dialog.setFont(font)
        return dialog

    def asset_manager(state: str) -> Any:
        model_manager = Mock()
        model_manager.model_path.return_value = Path(
            "/Users/Player/Library/Application Support/VNTTS/models/xtts_v2"
        )
        model_state = state.startswith("model-") or state == "compact-model"
        manifest = (
            None if state == "voices-empty" else "/Users/Player/voices/manifest.json"
        )
        dialog = AssetManagerDialog(
            settings.updated(
                speech_backend="coqui-xtts" if model_state else "moss-tts",
                voice_manifest=manifest,
                xtts_terms_accepted=True,
            ),
            model_manager=model_manager,
            voice_manager=Mock(),
        )
        if state == "voices-validation":
            dialog._set_manifest_validation_pending(True)
            dialog.voice_status.setText(
                "Checksum-validating the selected manifest and voice files..."
            )
        elif state == "voices-error":
            dialog._manifest_validation_finished(
                None,
                ValueError(
                    "Voice reference checksum failed: voices/Centurion/line-03.wav"
                ),
            )
        elif state == "model-verified":
            dialog.set_operation_running(True, "verify")
            dialog.model_finished(
                True,
                "Model verified and ready at /Users/Player/Library/VNTTS/models/xtts_v2",
            )
        elif state == "model-download":
            dialog.set_operation_running(True, "download")
            dialog.update_progress(45, "Downloading selected speech model: 45%")
        elif state == "model-failure":
            dialog.set_operation_running(True, "download")
            dialog.model_finished(False, "Model download failed: Network unavailable")
        elif state in {"compact-voices", "compact-model"}:
            dialog.resize(700, 320)
            font = dialog.font()
            font.setPointSize(font.pointSize() + 4)
            dialog.setFont(font)
        return dialog

    def game_profiles(state: str) -> Any:
        region = DialogRegion(0.1, 0.6, 0.8, 0.3)
        active = GameProfile.from_settings(
            "Reverse: 1999",
            settings.updated(
                capture_mode="window",
                game_window_title="Reverse: 1999",
            ),
            region=region,
            profile_id="active",
        )
        other = GameProfile.from_settings(
            "A much longer second game profile name",
            settings.updated(capture_mode="screen", game_window_title=None),
            region=region,
            profile_id="other",
        )
        profiles = [] if state == "empty" else [active, other]
        store = GameProfileStore(
            Path("/private/tmp/vntts-ui-catalog-unused-profiles.json"), profiles
        )
        dialog = GameProfilesDialog(
            settings.updated(active_profile_id=None if state == "empty" else "active"),
            store,
        )
        if state in {"other-selected", "compact"}:
            dialog.profiles.setCurrentIndex(dialog.profiles.findData("other"))
        if state == "compact":
            dialog.resize(600, 410)
            font = dialog.font()
            font.setPointSize(font.pointSize() + 4)
            dialog.setFont(font)
        return dialog

    def voice_import(state: str) -> Any:
        dialog = VoiceImportDialog()
        dialog.resize(580, 220)
        if state != "empty":
            dialog.character.setText("Believer IV")
            dialog.aliases.setText("Believer, Faithful")
            filenames = (
                [
                    "Chapter_7_Believer_IV_They_at_least_paid_with_their_lives.wav",
                    "Chapter_7_Believer_IV_What_about_you.wav",
                ]
                if state == "long-values"
                else ["Believer_01.wav", "Believer_02.wav"]
            )
            dialog.references = [f"/Users/Player/voices/{name}" for name in filenames]
            dialog.reference_files.setPlainText("\n".join(filenames))
            dialog.reference_files.setToolTip("\n".join(dialog.references))
            dialog._update_add_button()
        if state == "compact":
            dialog.resize(520, 250)
            font = dialog.font()
            font.setPointSize(font.pointSize() + 4)
            dialog.setFont(font)
        return dialog

    def calibration(state: str) -> Any:
        scene = Image.new("RGB", (800, 450), "#272936")
        painter = ImageDraw.Draw(scene)
        painter.rectangle((75, 275, 730, 420), fill="#141822", outline="#7a8198")
        painter.text((105, 300), "SELONE", fill="#e9e9e9")
        painter.text(
            (105, 345), "I have returned. We can continue our journey.", fill="#e9e9e9"
        )
        if state.startswith("overlay-"):
            overlay = DialogRegionOverlay(
                output=Path("/private/tmp/vntts-ui-catalog-unused-region.json"),
                background=scene,
            )
            overlay.resize(800, 450)
            if state in {"overlay-selected", "overlay-save-failure"}:
                overlay.origin = QPoint(85, 285)
                overlay.current = QPoint(735, 425)
            if state == "overlay-save-failure":
                overlay.save_error = "Unable to save the dialogue area: disk full"
            return overlay
        pool = Mock()
        dialog = CalibrationReviewDialog(
            scene.crop((85, 285, 735, 425)), thread_pool=pool
        )
        if state in {"review-recognized", "review-compact"}:
            dialog._recognition_finished(
                OCRResult(
                    "Selone",
                    "I have returned. We can continue our journey.",
                    94.5,
                    "balanced",
                    1,
                ),
                None,
            )
        elif state == "review-failure":
            dialog._recognition_finished(None, OSError("OCR runtime unavailable"))
        if state == "review-compact":
            dialog.resize(620, 430)
            font = dialog.font()
            font.setPointSize(font.pointSize() + 4)
            dialog.setFont(font)
        return dialog

    def voice_editor(
        role: str,
        *,
        generating: bool = False,
        failed: bool = False,
        compact_long_values: bool = False,
        narrator_fallback: bool = False,
    ) -> Any:
        dashboard = ControlDashboard(settings)
        dashboard.resize(
            820 if compact_long_values else 1180,
            720 if compact_long_values else 900,
        )
        importer = Mock()
        importer.selected_installation_root.return_value = None
        pool = Mock()
        pool.start.side_effect = lambda _task: None
        previews = Mock()
        previews.backend.runtime_status = "Apple GPU · model loaded"
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        dashboard._catalog_temporary_directory = temporary
        panel = GameNarratorDialog(
            settings,
            importer=importer,
            preview_service=previews,
            thread_pool=pool,
            player=Mock(),
            voice_library=VoiceLibrary(Path(temporary.name) / "voice-library"),
        )
        panel._initializing = True
        long_role = "The Extremely Long Character Name Used to Verify Layout"
        selected_role = long_role if compact_long_values else role
        panel.set_voice_context(
            character=selected_role,
            roles=("Believer IV", "Centurion", "Selone", long_role),
            story_titles=("Chapter 7",),
        )
        selected_character = (
            long_role
            if compact_long_values
            else role
            if role != "Narrator"
            else "Believer IV"
        )
        if compact_long_values:
            reference_title = (
                "Chapter 7 - A deliberately long original reference title that "
                "must remain readable without hiding the playback action"
            )
            transcript = (
                "A deliberately long transcript verifies wrapping and resizing "
                "while remaining visible beside the reference it describes."
            )
        elif role == "Selone":
            reference_title = "Chapter 7 - Selone reference 1"
            transcript = "The road is quiet now. We should keep moving."
        else:
            reference_title = (
                "Chapter 7 - They at least paid with their lives. What about you?"
            )
            transcript = "They at least paid with their lives. What about you?"
        with QSignalBlocker(panel.characters), QSignalBlocker(panel.references):
            panel.characters.clear()
            panel.characters.addItems(("Believer IV", "Centurion", "Selone", long_role))
            panel.characters.setCurrentText(selected_character)
            panel.references.clear()
            panel.references.addItem(
                reference_title,
                f"reference:{selected_character.casefold().replace(' ', '-')}"
                ":chapter-7",
            )
        panel._character = panel.characters.currentText()
        with QSignalBlocker(panel.source):
            panel.source.setCurrentIndex(panel.source.findData("game"))
        panel.reference_text.setText(transcript)
        panel.reference_details.setText(
            f"{selected_character} spoken reference 1 | 12.510 s\n"
            "Technical reference checks passed; voice quality is yours to judge."
        )
        panel._prepared[panel.references.currentData()] = (
            Path(temporary.name) / "reference-manifest.json"
        )
        panel._initializing = False
        panel._engine_available = lambda: True
        panel._update()
        panel.status.setText(
            "Choose a voice for this role. Nothing changes until you save."
            if role == "Narrator"
            else "Choose narrator fallback for Selone. Nothing changes until you save."
            if narrator_fallback
            else f"{role} was not mapped during live reading. Choose and save a voice."
        )
        if role == "Selone" and not narrator_fallback:
            panel.set_recovery_context("Selone", resume_live=True)
        if narrator_fallback:
            with QSignalBlocker(panel.source):
                panel.source.setCurrentIndex(panel.source.findData("narrator"))
            panel._update()
        if generating:
            QTimer.singleShot(
                0,
                lambda: panel._start(
                    "preview", "Generating your preview...", lambda: None
                ),
            )
        elif failed:

            def finish_with_error() -> None:
                panel._operation = "preview"
                with patch("vntts.support.record_game_import"):
                    panel._finished(None, RuntimeError("Preview generation failed."))

            QTimer.singleShot(0, finish_with_error)
        if narrator_fallback:
            panel._catalog_temporary_directory = temporary
            panel.resize(940, 620)
            return panel
        dashboard.embed_narrator(panel)
        dashboard.set_status(
            "Choose and preview a voice. Nothing changes until you save."
        )
        return dashboard

    def unknown_speaker_prompt(*, long_name: bool = False) -> Any:
        speaker = "The Keeper of the Moonlit Observatory" if long_name else "Selone"
        prompt, _choose, _continue, _cancel = build_unknown_speaker_prompt(speaker)
        if long_name:
            font = prompt.font()
            font.setPointSize(font.pointSize() + 3)
            prompt.setFont(font)
        return prompt

    def story_match_recovery(*, larger_text: bool = False) -> Any:
        prompt, _read, _stories, _stop = build_story_match_recovery_prompt(
            "The visible dialogue does not match the prepared story at this point."
        )
        if larger_text:
            font = prompt.font()
            font.setPointSize(font.pointSize() + 5)
            prompt.setFont(font)
            prompt.setMaximumWidth(620)
        return prompt

    def offline_preparation(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        root = Path(temporary.name)
        story_index = root / "story-index.jsonl"
        story_index.touch()
        chapters = (
            StorySelection(
                "chapter-7",
                "Chapter 7 - The long night",
                "chapter",
                0,
                tuple(f"chapter-7:{index}" for index in range(18)),
                18,
                18,
                3,
                15,
                3,
                ("Narrator", "Believer IV", "Centurion"),
                ("Narrator", "Believer IV", "Centurion"),
                1_400,
            ),
            StorySelection(
                "chapter-8",
                "Chapter 8 - The road ahead",
                "chapter",
                1,
                tuple(f"chapter-8:{index}" for index in range(24)),
                24,
                24,
                5,
                19,
                4,
                ("Narrator", "Believer IV", "Centurion", "Selone"),
                ("Narrator", "Believer IV", "Centurion", "Selone"),
                1_900,
            ),
            StorySelection(
                "side-story",
                "A long side-story title that still has to remain readable",
                "character_story",
                2,
                tuple(f"side-story:{index}" for index in range(12)),
                12,
                12,
                2,
                10,
                2,
                ("Narrator", "Selone"),
                ("Narrator", "Selone"),
                900,
            ),
        )
        content = GameContent(
            "reverse1999",
            "Reverse: 1999",
            "3.7",
            story_index,
            "a" * 64,
            chapters,
        )
        importer = Mock()
        importer.availability.return_value = ImporterAvailability(True, "Ready")
        pool = Mock()
        pool.start.side_effect = lambda _task: None
        discovery = None if state == "loading" else lambda: ContentDiscovery((content,))
        offline_settings = settings.updated(
            speech_backend="pocket-tts",
            tts_model=None,
            tts_profile="default",
            narrator_speaker="alba",
        )
        voice_library = VoiceLibrary(root / "voice-library")
        voice_library.select(
            "Narrator", route="voice", source_id="preset:alba", method="manual"
        )
        audition_service = Mock()
        audition_service.backend = SimpleNamespace(
            runtime_status="Apple GPU - model loaded"
        )
        original_reference = root / "original-reference.wav"
        write_silent_wav(original_reference)
        audition_service.reference_audio.return_value = original_reference
        dialog = OfflineAudioPreparationDialog(
            offline_settings,
            discovery=discovery,
            importer=importer,
            job_store=PregenerationJobStore(root / "jobs"),
            thread_pool=pool,
            game_narrator_chooser=Mock(return_value=None),
            voice_library=voice_library,
            audition_service=audition_service,
            preview_player=Mock(),
            automatic_activation=True,
        )
        dialog._catalog_temporary_directory = temporary
        dialog.resize(1040, 760)
        if state == "loading":
            return dialog

        dialog.select_all_button.click()
        dialog._unsaved_story_selections.clear()
        if state == "selection":
            dialog._story_job_statuses["chapter-8"] = "ready"
            dialog._refresh_story_statuses()
            dialog.stories.setCurrentRow(1)
            dialog.continue_button.setFocus()
            return dialog

        dialog._job = SimpleNamespace(
            story_index_sha256=content.story_index_sha256,
            selected_story_ids=("chapter-7", "chapter-8"),
            estimate=SimpleNamespace(original_audio_lines=8, selected_lines=42),
        )
        dialog._story_selection_drafts[content.story_index_sha256] = {
            "chapter-7",
            "chapter-8",
        }
        dialog._populate_stories(content)
        if state == "confirmation":
            dialog.step.setText("Step 2 of 4 - Choose and confirm voices")
            centurion = VoiceCandidate(
                "character:centurion",
                "Centurion",
                "centurion-v1",
                ("1" * 64,),
                94,
                "Closest character voice in the selected stories",
                source_line_ids=("chapter-7:10",),
                reference_duration_seconds=12.5,
            )
            knight = VoiceCandidate(
                "character:knight",
                "A Knight",
                "knight-v2",
                ("2" * 64,),
                88,
                "Second-best character voice match",
                source_line_ids=("chapter-8:3",),
                reference_duration_seconds=8.7,
            )
            plan = VoicePlan(
                "catalog-job",
                "2026-09-22T00:00:00+00:00",
                content.story_index_sha256,
                None,
                None,
                "pocket-tts",
                None,
                "en",
                "stable",
                False,
                "b" * 64,
                (
                    VoiceGroup(
                        "believer",
                        "Believer IV",
                        ("Believer IV",),
                        None,
                        None,
                        None,
                        ("chapter-7:4", "chapter-8:3"),
                        "The storm has passed.",
                        "We can continue our journey.",
                        "voice",
                        "character:centurion",
                        "Centurion",
                        "Centurion",
                        ("1" * 64,),
                        "c" * 64,
                        "d" * 64,
                        "automatic-character-match",
                        candidates=(centurion, knight),
                        candidate_inventory=(centurion, knight),
                    ),
                    VoiceGroup(
                        "selone",
                        "Selone",
                        ("Selone",),
                        None,
                        None,
                        None,
                        ("chapter-8:9",),
                        "We should keep moving.",
                        None,
                        "narrator",
                        "preset:alba",
                        None,
                        None,
                        (),
                        "e" * 64,
                        "f" * 64,
                        "automatic-narrator-fallback",
                    ),
                ),
            )
            dialog._voice_plan = plan
            dialog._show_voice_confirmation(plan)
            dialog.content_scroll.show()
            dialog.continue_button.setFocus()
            return dialog

        dialog._generation_input = SimpleNamespace(ready_items=34)
        dialog.generating = True
        dialog.step.setText("Step 3 of 4 - Generate and check audio")
        dialog.cancel_button.setText("Cancel generation")
        dialog._start_generation_progress()
        if state == "partial-ready":
            progress = OfflineGenerationProgress(
                generated=14,
                active_phase="generating",
                runtime_status="Apple GPU - model loaded",
                ready_line_ids=tuple(f"chapter-7:{index}" for index in range(14)),
            )
            dialog._progress_finished(progress, None)
            dialog.play_ready_button.setFocus()
            return dialog
        if state == "failure":
            progress = OfflineGenerationProgress(
                generated=14,
                failed=1,
                active_phase="generating",
                runtime_status="Apple GPU - model loaded",
                ready_line_ids=tuple(f"chapter-7:{index}" for index in range(14)),
            )
            dialog._progress_finished(progress, None)
            dialog.generating = False
            dialog._stop_generation_progress()
            dialog._preparation_paused(
                "Generation paused",
                "One line could not be generated safely.",
            )
            dialog.resume_status.setText(
                "Unable to generate one dialogue line safely. Continue to retry it."
            )
            dialog.progress_cancel_consequence.setText(
                "Continue retries only unfinished lines; completed audio stays saved."
            )
            dialog.continue_button.setText("Continue preparation")
            dialog.continue_button.setEnabled(True)
            dialog.cancel_button.setText("Close")
            dialog.continue_button.setFocus()
            return dialog

        dialog.generating = False
        dialog._generation_result = SimpleNamespace(
            generated=34,
            failed=0,
            other_terminal=0,
        )
        result = SimpleNamespace(
            approved=34,
            live_fallbacks=0,
            story_lines=42,
            omissions=0,
        )
        dialog._stop_generation_progress()
        dialog._show_final_handoff(result)
        dialog.defer_activation("Reading settings changed after preparation started.")
        dialog.continue_button.setFocus()
        return dialog

    def voice_audition(state: str) -> Any:
        dialog = offline_preparation("confirmation")
        dialog._inspect_character_voice()
        panel = dialog.voice_panel
        if state == "alternate":
            panel.voice_reference.setCurrentIndex(1)
        elif state == "alternate-phrase":
            panel.preview_phrase.setCurrentIndex(1)
        elif state == "preview-ready":
            preview = Path(dialog._catalog_temporary_directory.name) / "preview.wav"
            write_silent_wav(preview)
            panel._preview_finished(SimpleNamespace(path=preview), None)
            panel._set_playing_source(None)
            panel.status.setText("Generated preview ready. Use this voice if suitable.")
        elif state == "preview-failure":
            with patch("vntts.support.record_game_import"):
                panel._preview_finished(
                    SimpleNamespace(path=Path("unused.wav")),
                    RuntimeError("The speech engine stopped before producing audio."),
                )
        elif state == "save-failure":
            candidate, choice, _narrator = panel._current_entry()
            panel._displayed = ((candidate, None, choice),)
            panel.a_use.setEnabled(True)
            panel.use_a()
            panel._decision_finished(None, OSError("Application data is read-only."))
        return dialog

    def ocr_review(state: str) -> Any:
        temporary = TemporaryDirectory()
        directory = Path(temporary.name)
        if state != "empty":
            size = (1200, 1000) if state == "enlarged-long" else (640, 200)
            image = Image.new("RGB", size, "#282b33")
            draw = ImageDraw.Draw(image)
            shared_source = state == "shared-source-conflict"
            draw.text(
                (24, 68),
                "No" if shared_source else "Mareus: Hello tiniekeeper.",
                fill="white",
            )
            if state == "enlarged-long":
                draw.text(
                    (760, 820),
                    "Dialogue continues near the opposite corner.",
                    fill="white",
                )
            UncertainFrameRecorder(directory).record(
                image,
                OCRResult(
                    "No" if shared_source else "Mareus",
                    "No" if shared_source else "Hello tiniekeeper.",
                    42.5,
                    "balanced",
                    3,
                ),
                60,
            )
        dialog = OCRReviewDialog(
            directory,
            OCRCorrectionStore(directory / "corrections.json"),
            "game",
            "Reverse: 1999",
        )
        dialog._catalog_temporary_directory = temporary
        if state in {"enlarged-screenshot", "enlarged-long"}:
            enlarged = dialog._screenshot_dialog()
            if enlarged is None:
                raise RuntimeError("OCR review screenshot could not be enlarged")
            enlarged._catalog_temporary_directory = temporary
            enlarged._catalog_owner = dialog
            return enlarged
        if state in {"corrected", "all-games", "saving", "save-failure", "compact"}:
            dialog.corrected_character.setText("Marcus")
            dialog.corrected_text.setPlainText("Hello timekeeper.")
        if state == "all-games":
            dialog.scope.setCurrentIndex(0)
        if state == "shared-source-conflict":
            dialog.corrected_character.setText("Narrator")
            dialog.corrected_text.setPlainText("Yes")
        if state == "resolve-confirm":
            dialog.corrected_character.setText("Marcus")
            confirmation = dialog._dismissal_dialog()
            confirmation._catalog_temporary_directory = temporary
            confirmation._catalog_owner = dialog
            return confirmation
        elif state == "saving":
            dialog._write_active = True
            dialog.save_button.setEnabled(False)
            dialog.resolve_button.setEnabled(False)
            dialog.sample_list.setEnabled(False)
            dialog.corrected_character.setEnabled(False)
            dialog.corrected_text.setEnabled(False)
            dialog.scope.setEnabled(False)
            dialog.status.setText("Saving review in the background...")
        elif state == "save-failure":
            dialog._write_finished(None, OSError("Application data is read-only"))
        elif state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(730, 560)
        return dialog

    def ocr_corrections(state: str = "profile-rules") -> Any:
        temporary = TemporaryDirectory()
        directory = Path(temporary.name)
        profile_rules = {
            "Mareus": "Marcus",
            "Hello tiniekeeper.": "Hello timekeeper.",
        }
        if state == "empty":
            profile_rules = {}
        elif state == "long-values":
            profile_rules = {
                "The unknowable chronology of the distant island's memories": "The hidden chronology of the distant island's memories"
            }
        store = OCRCorrectionStore(
            directory / "corrections.json",
            global_entries={} if state == "empty" else {"tiniekeeper": "timekeeper"},
            profile_entries={"game": profile_rules},
        )
        dialog = OCRCorrectionsDialog("game", "Reverse: 1999", store)
        dialog._catalog_temporary_directory = temporary
        if state == "validation":
            dialog._append_row(dialog.global_table, "Unclear", "")
            dialog._append_row(dialog.profile_table, "Mareus", "Different")
            dialog.save()
        elif state == "saving":
            dialog._save_active = True
            dialog.tabs.setEnabled(False)
            dialog.buttons.setEnabled(False)
            dialog.status.setText("Saving OCR corrections in the background...")
        elif state == "save-failure":
            dialog._append_row(dialog.profile_table, "Vertln", "Vertin")
            dialog._save_finished(None, OSError("Application data is read-only"))
        elif state == "unsaved-close":
            dialog._append_row(dialog.profile_table, "Vertln", "Vertin")
            confirmation = dialog._discard_dialog()
            confirmation._catalog_temporary_directory = temporary
            confirmation._catalog_owner = dialog
            return confirmation
        elif state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(560, 400)
        return dialog

    def dialogue_history(state: str) -> Any:
        history = DialogueHistory(
            clock=lambda: datetime(2026, 9, 22, 12, 34, tzinfo=timezone.utc)
        )
        if state != "empty":
            history.add("Mareus", "The storm has passed. We can continue our journey.")
            history.finish_current()
            history.add(
                "Believer IV", "They at least paid with their lives. What about you?"
            )
            history.finish_current()
            if state == "long-text":
                history.add(
                    "A very long character name from the distant island",
                    "The narration continues beyond the edge of the list. " * 8,
                )
        dialog = DialogueHistoryDialog(
            history, lambda *_args: None, stop_handler=lambda: None
        )
        dialog.timer.stop()
        if state == "filtered":
            dialog.search.setText("storm")
        elif state == "speaking":
            dialog.replay_button.setEnabled(False)
            dialog.stop_button.setEnabled(True)
            dialog.status.setText("Speaking as Believer IV with the current voice...")
        elif state == "failure":
            dialog._replay_finished(
                None, RuntimeError("The speech engine is unavailable")
            )
        elif state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(600, 420)
        return dialog

    def diagnostics(state: str) -> Any:
        dialog = DiagnosticsDialog()
        if state != "empty":
            capture = Image.new("RGB", (760, 190), "#171a20")
            ImageDraw.Draw(capture).text(
                (24, 72),
                "Believer IV: They at least paid with their lives. What about you?",
                fill="white",
                font=ImageFont.load_default(size=25),
            )
            dialog.set_snapshot(
                DiagnosticSnapshot(
                    capture,
                    character="Believer IV",
                    text="They at least paid with their lives. What about you?",
                    confidence=91.2,
                    preprocessing_profile="dark-background",
                    voice="Believer IV; voice ID: game-believer-iv",
                    capture_ms=12.3,
                    ocr_ms=45.6,
                    synthesis_ms=789.0,
                    playback_ms=321.0,
                    capture_interval_ms=600.0,
                    game_focused=True,
                    corrections=("BeIiever -> Believer",),
                    speech_queue_depth=1,
                    max_speech_queue_depth=3,
                    last_first_audio_ms=427.0,
                    captured_at=datetime(2026, 9, 22, 12, 34, tzinfo=timezone.utc),
                )
            )
        if state == "technical":
            dialog.technical_toggle.setChecked(True)
        if state == "refreshing":
            dialog.request_refresh()
        elif state == "warning":
            dialog.set_warning(
                "The selected game window is unavailable. Restore the game or choose another window.",
                remediation=("settings", "Open Settings"),
            )
        elif state == "timeout":
            dialog.request_refresh()
            dialog._refresh_timed_out(dialog.refresh_generation)
        elif state == "compact":
            dialog.set_warning(
                "Screen Recording permission is missing. Allow VNTTS in macOS System Settings, then reopen the app.",
                remediation=("macos-permissions", "Open macOS permissions"),
            )
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.warning.setFont(font)
            dialog.resize(620, 460)
        return dialog

    def support_center(state: str) -> Any:
        events: list[dict[str, str]] = []
        if state != "empty":
            events.extend(
                {
                    "recorded_at": f"2026-09-22T12:{index:02d}:00+00:00",
                    "level": "info" if index % 7 else "warning",
                    "message": f"Captured dialogue and checked selected voice ({index})",
                }
                for index in range(42 if state == "new-events" else 3)
            )
        dialog = SupportCenterDialog(SimpleNamespace(snapshot=lambda: list(events)))
        dialog.refresh()
        if state == "new-events":
            dialog.show()
            app.processEvents()
            dialog.events.moveCursor(QTextCursor.MoveOperation.Start)
            dialog.events.verticalScrollBar().setValue(0)
            events.append(
                {
                    "recorded_at": "2026-09-22T13:00:00+00:00",
                    "level": "warning",
                    "message": "Selected game window is no longer available",
                }
            )
            dialog.refresh()
        elif state == "exporting":
            dialog.request_export()
        elif state == "failure":
            dialog.set_export_result(False, "The chosen folder is read-only")
        elif state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(640, 440)
        return dialog

    def authoring_workbench(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        root = Path(temporary.name)
        workspace = create_test_workspace(root)[2].directory
        state_path = workspace / "generated-audio" / "generation-state.json"
        document = json.loads(state_path.read_text(encoding="utf-8"))
        if state == "ready":
            document["active"] = None
            document["items"] = {}
        elif state in {"review", "compact", "compact-actions"}:
            document["active"] = None
            item = next(iter(document["items"].values()))
            item["status"] = "generated"
            item["review_status"] = "pending_review"
        elif state == "approved":
            document["active"] = None
        elif state == "failed":
            document["active"] = None
            queue_id = next(iter(document["items"]))
            document["items"][queue_id] = {
                "status": "failed",
                "attempts": 3,
                "seed": 2,
                "last_error": "Voice synthesis ended without usable speech",
                "updated_at": "2026-08-17T00:00:00+00:00",
            }
        if state in {
            "ready",
            "review",
            "approved",
            "compact",
            "compact-actions",
            "failed",
        }:
            state_path.write_text(json.dumps(document), encoding="utf-8")
        if state == "running-elsewhere":
            (workspace / "generated-audio" / ".generation-lease.json").write_text(
                json.dumps(
                    {
                        "schema": "vntts.authoring-generation-lease",
                        "schema_version": 1,
                        "queue_sha256": sha256_file(workspace / "queue.jsonl"),
                        "pid": os.getpid(),
                        "hostname": socket.gethostname(),
                        "process_started_at": process_started_at(os.getpid()),
                        "lease_id": "catalog-running-owner",
                        "started_at": "2026-08-17T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
        dialog = AuthoringWorkbenchDialog(
            workspace,
            settings=QSettings(str(root / "ui.ini"), QSettings.Format.IniFormat),
            synchronous_projection=True,
        )
        dialog._catalog_temporary_directory = temporary
        if state == "filtered-empty":
            dialog.review_status.setCurrentText("Awaiting review")
        if state in {"compact", "compact-actions"}:
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        if state == "compact-actions":
            QTimer.singleShot(
                0,
                lambda: dialog.workbench_scroll.ensureWidgetVisible(dialog.approve),
            )
        return dialog

    def cohort_review(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        root = Path(temporary.name)
        first = create_pending_cohort_workspace(root / "first")[0]
        second = create_pending_cohort_workspace(root / "second")[0]
        bundle = build_cohort_review_bundle((first, second))
        if state == "load-failure":

            def fail_load(_bundle: object) -> None:
                raise ValueError("The sample manifest could not be read")

            dialog = CohortReviewBundleDialog(bundle, sample_loader=fail_load)
        else:
            dialog = CohortReviewBundleDialog(bundle)
        dialog._catalog_temporary_directory = temporary
        deadline = time.monotonic() + 3
        while dialog._load_active and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        if state in {"heard", "bad"}:
            sample = dialog._selected_sample()
            key = dialog._current_key()
            if sample is not None and key is not None:
                dialog.heard[key].add(sample.item.queue_id)
                dialog._show_current_cohort()
            if state == "bad":
                dialog.toggle_bad()
                dialog.defect_toggle.setChecked(True)
        if state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        if state == "preparing":
            dialog._playback_prepare_active = True
            dialog.status.setText("Preparing selected recording...")
            dialog._update_actions()
        if state == "saving":
            dialog._decision_active = True
            dialog._decision_started_at = time.perf_counter()
            dialog._decision_scope_text = "one exact cohort"
            dialog._update_actions()
        return dialog

    def legacy_reason_review(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        root = Path(temporary.name)
        _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
        review = build_legacy_reason_review(corpus, root)
        if state in {"compact", "compact-reasons"}:
            item = review.items[0]
            review = replace(
                review,
                items=(
                    replace(
                        item,
                        speaker="Rhiannon, the keeper of the northern passage",
                        text=(
                            "The road is long and the storm has returned. "
                            "We must cross the valley before morning, even if "
                            "the others decide to turn back."
                        ),
                    ),
                ),
            )
        writer = (
            Mock(side_effect=OSError("The selected folder is read-only"))
            if state == "save-error"
            else Mock()
        )
        dialog = LegacyReasonReviewDialog(
            review,
            root / "progress.json",
            player=Mock(),
            progress_writer=writer,
            publisher=(
                Mock(side_effect=OSError("The decision folder is read-only"))
                if state == "publish-error"
                else Mock(return_value=(root / "decision.json",))
            ),
        )
        dialog._catalog_temporary_directory = temporary
        if state in {
            "heard",
            "selected",
            "save-error",
            "publish-error",
            "compact-reasons",
        }:
            dialog.play_current()
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        if state in {"selected", "publish-error"}:
            dialog.reason_controls["pause_or_pacing"].click()
        if state == "save-error":
            dialog.acceptable.click()
        if state == "publish-error":
            dialog.finish.click()
        if state in {"compact", "compact-reasons"}:
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        if state == "compact-reasons":
            QTimer.singleShot(0, dialog.reason_controls["other_or_unclear"].setFocus)
        return dialog

    def character_story_review(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        root = Path(temporary.name)
        references = root / "references"
        references.mkdir()
        portraits = root / "portraits"
        portraits.mkdir()
        Image.new("RGB", (100, 100), "#5a6285").save(portraits / "534704.png")
        Image.new("RGB", (100, 100), "#806453").save(portraits / "314601.png")
        candidates = []
        groups = []
        for index, (character, portrait, media_id) in enumerate(
            (
                ("Dobharchú", "534704.png", 10),
                ("Aderyn", "314601.png", 20),
            )
        ):
            reference = references / f"{media_id}.wav"
            write_silent_wav(reference)
            candidates.append(
                {
                    "character": character,
                    "portrait": portrait,
                    "source_bank": f"voice-{index}.bnk",
                    "media_id": media_id,
                    "candidate_origin": "story_line_route",
                    "source_event_ids": [1000 + media_id],
                    "reference": f"references/{media_id}.wav",
                    "reference_sha256": sha256_file(reference),
                    "technical_pass": False,
                    "transcript_conflict": False,
                    "metrics": {
                        "duration_seconds": 0.1,
                        "quality_score": 0,
                        "technical_flags": ["synthetic-silence-not-voice"],
                    },
                    "source_lines": [
                        {
                            "line_id": f"reverse1999:test:{index}",
                            "text": f"Evidence for {character}",
                        }
                    ],
                }
            )
            groups.append(
                {
                    "character": character,
                    "portrait": portrait,
                    "source_bank": f"voice-{index}.bnk",
                    "recommended_media_ids_for_audition": [],
                }
            )
        report = root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "schema_version": REPORT_VERSION,
                    "groups": groups,
                    "candidates": candidates,
                }
            ),
            encoding="utf-8",
        )
        dialog = StoryVoiceReviewDialog(report, portrait_directory=portraits)
        dialog._catalog_temporary_directory = temporary
        dialog.recommended_only.setChecked(False)
        if state.startswith("compact"):
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(640, 480)
        if state == "compact-actions":
            QTimer.singleShot(
                0, lambda: dialog.scroll_area.ensureWidgetVisible(dialog.clear_ab)
            )
        return dialog

    def missing_voice_review(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        root = Path(temporary.name)
        statuses = (
            ("failed", "failed")
            if state == "none"
            else ("generated", "generated")
            if state in {"pair", "compared"}
            else ("generated", "failed")
        )
        plan_path, evidence, snapshots, _queue_id = (
            create_missing_voice_reuse_review_fixture(root, statuses=statuses)
        )
        with patch(
            "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
            side_effect=lambda _plan, _candidate, path: snapshots[Path(path).resolve()],
        ):
            session_path = build_missing_voice_reuse_review(
                plan_path, evidence, root / "review", seed=7
            )
        dialog = MissingVoiceReuseReviewDialog(session_path)
        dialog._catalog_temporary_directory = temporary
        if state == "compared" and dialog._cohort is not None:
            for candidate in dialog.bundle["candidates"]:
                dialog.session["heard"].append(
                    {
                        "cohort_id": dialog._cohort["cohort_id"],
                        "queue_id": dialog._cohort["samples"][0]["queue_id"],
                        "label": candidate["label"],
                    }
                )
            dialog._refresh_sample()
        if state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        return dialog

    def failed_reference_review(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        root = Path(temporary.name)
        workspace, _queue_id = FailureReferenceAuditTest().create_failed_workspace(root)
        audit = root / "audit"
        publish_failure_reference_audit(workspace, audit)
        dialog = FailureReferenceAuditDialog(audit)
        dialog._catalog_temporary_directory = temporary
        if state == "heard":
            group = dialog._current_group()
            if group is not None:
                dialog._heard_candidates[group["group_id"]] = {
                    candidate["candidate_id"] for candidate in group["candidates"]
                }
                dialog._update_candidate_card()
                dialog._update_actions()
        if state == "preview":
            dialog.preview_toggle.setChecked(True)
        if state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        return dialog

    def source_reference_review(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        session = write_quality_session(Path(temporary.name))
        dialog = SourceReferenceQualityDialog(session)
        dialog._catalog_temporary_directory = temporary
        if state == "heard":
            for token in (
                "reference",
                *(sample["queue_id"] for sample in dialog.current["generated_samples"]),
            ):
                dialog._playing_token = token
                dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        elif state == "complete":
            dialog.session["variants"][0]["decision"] = "accept"
            dialog._load_next(dialog.session)
        elif state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        return dialog

    def blind_listening(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        root = Path(temporary.name)
        session = create_listening_session_from_reports(
            write_model_reports(root, item_count=1), root / "session"
        )
        playback = FakePlayback()
        dialog = ModelListeningDialog(
            session, auto_play=False, playback_factory=lambda: playback
        )
        dialog._catalog_temporary_directory = temporary
        if state == "heard":
            for side in ("a", "b"):
                dialog.play(side)
                playback.finish()
                dialog.poll_playback()
        elif state == "complete":
            record_trial_preference(session, dialog.current_trial["trial_id"], "a")
            dialog.load_next_trial()
        elif state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        return dialog

    def terminal_conflict_review(state: str) -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        directory = TerminalConflictReviewUiTest().create_review(Path(temporary.name))
        dialog = TerminalConflictReviewDialog(directory)
        dialog._catalog_temporary_directory = temporary
        if state == "heard":
            dialog._heard = {
                candidate["candidate_id"] for candidate in dialog._display_candidates
            }
            dialog._update_decision_buttons()
        elif state == "complete":
            record_terminal_conflict_decision(
                directory, dialog._current["case_id"], "neither_acceptable"
            )
            dialog._load_next()
        elif state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        return dialog

    renderers: dict[str, Callable[[], Any]] = {
        "dashboard.stories-ready": dashboard_stories,
        "dashboard.reading-loading": dashboard_loading,
        "dashboard.reading-active": dashboard_reading,
        "dashboard.reading-waiting": dashboard_waiting,
        "dashboard.reading-paused": dashboard_paused,
        "dashboard.reading-stopped": dashboard_stopped,
        "dashboard.reading-technical": dashboard_technical,
        "dashboard.story-position-recovery": dashboard_story_recovery,
        "dashboard.story-position-manual": dashboard_story_manual,
        "dashboard.reading-long-values": dashboard_long_values,
        "dashboard.reading-long-values-scrolled": dashboard_long_values_scrolled,
        "dashboard.setup-expanded": dashboard_setup,
        "compact-controller.sequence-recovery": compact_sequence_recovery,
        "settings.speech-and-voices": settings_speech,
        "settings.shortcuts": lambda: settings_section(0),
        "settings.capture": lambda: settings_section(1),
        "settings.capture-window": settings_capture_window,
        "settings.advanced-capture": lambda: settings_section(1, advanced=True),
        "settings.playback": lambda: settings_section(3),
        "settings.application": lambda: settings_section(4),
        "settings.checkbox-focus": settings_checkbox_focus,
        "settings.advanced-speech": lambda: settings_section(2, advanced=True),
        "settings.compact-capture": lambda: settings_section(1, compact=True),
        "settings.compact-advanced-speech": lambda: settings_section(
            2, advanced=True, compact=True
        ),
        "settings.compact-advanced-speech-scrolled": settings_compact_advanced_scrolled,
        "settings.validation-error": settings_validation_error,
        "readiness.loading": lambda: readiness("loading"),
        "readiness.blocked": lambda: readiness("blocked"),
        "readiness.audio-error": lambda: readiness("audio-error"),
        "readiness.warning-selected": lambda: readiness("warning-selected"),
        "readiness.warning-only": lambda: readiness("warning-only"),
        "readiness.non-english-ocr": lambda: readiness("non-english-ocr"),
        "readiness.ready": lambda: readiness("ready"),
        "readiness.long-error": lambda: readiness("long-error"),
        "onboarding.game-window": lambda: onboarding("game-window"),
        "onboarding.screen-region": lambda: onboarding("screen-region"),
        "onboarding.game-window-error": lambda: onboarding("game-window-error"),
        "onboarding.diagnostics-loading": lambda: onboarding("diagnostics-loading"),
        "onboarding.diagnostics-error": lambda: onboarding("diagnostics-error"),
        "onboarding.calibration": lambda: onboarding("calibration"),
        "onboarding.test": lambda: onboarding("test"),
        "onboarding.test-success": lambda: onboarding("test-success"),
        "onboarding.compact-error": lambda: onboarding("compact-error"),
        "macos-permissions.denied": lambda: macos_permissions("denied"),
        "macos-permissions.granted": lambda: macos_permissions("granted"),
        "macos-permissions.unavailable": lambda: macos_permissions("unavailable"),
        "macos-permissions.compact": lambda: macos_permissions("compact"),
        "asset-manager.voices-empty": lambda: asset_manager("voices-empty"),
        "asset-manager.voices-manifest": lambda: asset_manager("voices-manifest"),
        "asset-manager.voices-validation": lambda: asset_manager("voices-validation"),
        "asset-manager.voices-error": lambda: asset_manager("voices-error"),
        "asset-manager.model-unchecked": lambda: asset_manager("model-unchecked"),
        "asset-manager.model-verified": lambda: asset_manager("model-verified"),
        "asset-manager.model-download": lambda: asset_manager("model-download"),
        "asset-manager.model-failure": lambda: asset_manager("model-failure"),
        "asset-manager.compact-voices": lambda: asset_manager("compact-voices"),
        "asset-manager.compact-model": lambda: asset_manager("compact-model"),
        "game-profiles.empty": lambda: game_profiles("empty"),
        "game-profiles.active": lambda: game_profiles("active"),
        "game-profiles.other-selected": lambda: game_profiles("other-selected"),
        "game-profiles.compact": lambda: game_profiles("compact"),
        "voice-import.empty": lambda: voice_import("empty"),
        "voice-import.selected": lambda: voice_import("selected"),
        "voice-import.long-values": lambda: voice_import("long-values"),
        "voice-import.compact": lambda: voice_import("compact"),
        "calibration.overlay-empty": lambda: calibration("overlay-empty"),
        "calibration.overlay-selected": lambda: calibration("overlay-selected"),
        "calibration.overlay-save-failure": lambda: calibration("overlay-save-failure"),
        "calibration.review-loading": lambda: calibration("review-loading"),
        "calibration.review-recognized": lambda: calibration("review-recognized"),
        "calibration.review-failure": lambda: calibration("review-failure"),
        "calibration.review-compact": lambda: calibration("review-compact"),
        "unknown-speaker-prompt.awaiting-choice": unknown_speaker_prompt,
        "story-match-recovery.awaiting-choice": story_match_recovery,
        "story-match-recovery.larger-text": lambda: story_match_recovery(
            larger_text=True
        ),
        "unknown-speaker-prompt.long-name": lambda: unknown_speaker_prompt(
            long_name=True
        ),
        "voice-editor.narrator": lambda: voice_editor("Narrator"),
        "voice-editor.live-recovery": lambda: voice_editor("Selone"),
        "voice-editor.narrator-fallback": lambda: voice_editor(
            "Selone", narrator_fallback=True
        ),
        "voice-editor.preview-generating": lambda: voice_editor(
            "Narrator", generating=True
        ),
        "voice-editor.preview-failure": lambda: voice_editor("Narrator", failed=True),
        "voice-editor.long-values": lambda: voice_editor(
            "Narrator", compact_long_values=True
        ),
        "voice-editor.saved-return": dashboard_voice_saved,
        "offline-preparation.loading": lambda: offline_preparation("loading"),
        "offline-preparation.selection": lambda: offline_preparation("selection"),
        "offline-preparation.voice-confirmation": lambda: offline_preparation(
            "confirmation"
        ),
        "offline-preparation.partial-ready": lambda: offline_preparation(
            "partial-ready"
        ),
        "offline-preparation.failure": lambda: offline_preparation("failure"),
        "offline-preparation.completed": lambda: offline_preparation("completed"),
        "voice-audition.reference": lambda: voice_audition("reference"),
        "voice-audition.alternate": lambda: voice_audition("alternate"),
        "voice-audition.alternate-phrase": lambda: voice_audition("alternate-phrase"),
        "voice-audition.preview-ready": lambda: voice_audition("preview-ready"),
        "voice-audition.preview-failure": lambda: voice_audition("preview-failure"),
        "voice-audition.save-failure": lambda: voice_audition("save-failure"),
        "ocr-review.pending": lambda: ocr_review("pending"),
        "ocr-review.enlarged-screenshot": lambda: ocr_review("enlarged-screenshot"),
        "ocr-review.enlarged-long": lambda: ocr_review("enlarged-long"),
        "ocr-review.corrected": lambda: ocr_review("corrected"),
        "ocr-review.all-games": lambda: ocr_review("all-games"),
        "ocr-review.shared-source-conflict": lambda: ocr_review(
            "shared-source-conflict"
        ),
        "ocr-review.resolve-confirm": lambda: ocr_review("resolve-confirm"),
        "ocr-review.saving": lambda: ocr_review("saving"),
        "ocr-review.save-failure": lambda: ocr_review("save-failure"),
        "ocr-review.empty": lambda: ocr_review("empty"),
        "ocr-review.compact": lambda: ocr_review("compact"),
        "ocr-corrections.profile-rules": ocr_corrections,
        "ocr-corrections.empty": lambda: ocr_corrections("empty"),
        "ocr-corrections.validation": lambda: ocr_corrections("validation"),
        "ocr-corrections.saving": lambda: ocr_corrections("saving"),
        "ocr-corrections.save-failure": lambda: ocr_corrections("save-failure"),
        "ocr-corrections.unsaved-close": lambda: ocr_corrections("unsaved-close"),
        "ocr-corrections.long-values": lambda: ocr_corrections("long-values"),
        "ocr-corrections.compact": lambda: ocr_corrections("compact"),
        "dialogue-history.empty": lambda: dialogue_history("empty"),
        "dialogue-history.populated": lambda: dialogue_history("populated"),
        "dialogue-history.filtered": lambda: dialogue_history("filtered"),
        "dialogue-history.speaking": lambda: dialogue_history("speaking"),
        "dialogue-history.failure": lambda: dialogue_history("failure"),
        "dialogue-history.long-text": lambda: dialogue_history("long-text"),
        "dialogue-history.compact": lambda: dialogue_history("compact"),
        "diagnostics.empty": lambda: diagnostics("empty"),
        "diagnostics.snapshot": lambda: diagnostics("snapshot"),
        "diagnostics.technical": lambda: diagnostics("technical"),
        "diagnostics.refreshing": lambda: diagnostics("refreshing"),
        "diagnostics.warning": lambda: diagnostics("warning"),
        "diagnostics.timeout": lambda: diagnostics("timeout"),
        "diagnostics.compact": lambda: diagnostics("compact"),
        "support-center.empty": lambda: support_center("empty"),
        "support-center.events": lambda: support_center("events"),
        "support-center.new-events": lambda: support_center("new-events"),
        "support-center.exporting": lambda: support_center("exporting"),
        "support-center.failure": lambda: support_center("failure"),
        "support-center.compact": lambda: support_center("compact"),
        "authoring-workbench.ready": lambda: authoring_workbench("ready"),
        "cohort-review.pending": lambda: cohort_review("pending"),
        "cohort-review.heard": lambda: cohort_review("heard"),
        "cohort-review.bad": lambda: cohort_review("bad"),
        "cohort-review.preparing": lambda: cohort_review("preparing"),
        "cohort-review.saving": lambda: cohort_review("saving"),
        "cohort-review.load-failure": lambda: cohort_review("load-failure"),
        "cohort-review.compact": lambda: cohort_review("compact"),
        "legacy-reason-review.pending": lambda: legacy_reason_review("pending"),
        "legacy-reason-review.heard": lambda: legacy_reason_review("heard"),
        "legacy-reason-review.selected": lambda: legacy_reason_review("selected"),
        "legacy-reason-review.save-error": lambda: legacy_reason_review("save-error"),
        "legacy-reason-review.publish-error": lambda: legacy_reason_review(
            "publish-error"
        ),
        "legacy-reason-review.compact": lambda: legacy_reason_review("compact"),
        "legacy-reason-review.compact-reasons": lambda: legacy_reason_review(
            "compact-reasons"
        ),
        "missing-voice-review.available": lambda: missing_voice_review("available"),
        "missing-voice-review.pair": lambda: missing_voice_review("pair"),
        "missing-voice-review.compared": lambda: missing_voice_review("compared"),
        "missing-voice-review.none": lambda: missing_voice_review("none"),
        "missing-voice-review.compact": lambda: missing_voice_review("compact"),
        "failed-reference-review.pending": lambda: failed_reference_review("pending"),
        "failed-reference-review.heard": lambda: failed_reference_review("heard"),
        "failed-reference-review.preview": lambda: failed_reference_review("preview"),
        "failed-reference-review.compact": lambda: failed_reference_review("compact"),
        "source-reference-review.pending": lambda: source_reference_review("pending"),
        "source-reference-review.heard": lambda: source_reference_review("heard"),
        "source-reference-review.complete": lambda: source_reference_review("complete"),
        "source-reference-review.compact": lambda: source_reference_review("compact"),
        "blind-listening.pending": lambda: blind_listening("pending"),
        "blind-listening.heard": lambda: blind_listening("heard"),
        "blind-listening.complete": lambda: blind_listening("complete"),
        "blind-listening.compact": lambda: blind_listening("compact"),
        "terminal-conflict-review.pending": lambda: terminal_conflict_review("pending"),
        "terminal-conflict-review.heard": lambda: terminal_conflict_review("heard"),
        "terminal-conflict-review.complete": lambda: terminal_conflict_review(
            "complete"
        ),
        "terminal-conflict-review.compact": lambda: terminal_conflict_review("compact"),
        "character-story-review.pending": lambda: character_story_review("pending"),
        "character-story-review.compact": lambda: character_story_review("compact"),
        "character-story-review.compact-actions": lambda: character_story_review(
            "compact-actions"
        ),
        "authoring-workbench.review": lambda: authoring_workbench("review"),
        "authoring-workbench.approved": lambda: authoring_workbench("approved"),
        "authoring-workbench.interrupted": lambda: authoring_workbench("interrupted"),
        "authoring-workbench.failed": lambda: authoring_workbench("failed"),
        "authoring-workbench.running-elsewhere": lambda: authoring_workbench(
            "running-elsewhere"
        ),
        "authoring-workbench.filtered-empty": lambda: authoring_workbench(
            "filtered-empty"
        ),
        "authoring-workbench.compact": lambda: authoring_workbench("compact"),
        "authoring-workbench.compact-actions": lambda: authoring_workbench(
            "compact-actions"
        ),
    }
    surfaces = {surface["id"]: surface for surface in catalog["surfaces"]}
    allowed_surfaces = set(surfaces)
    if selected_surface:
        if selected_surface not in surfaces:
            raise ValueError(f"unknown surface: {selected_surface}")
        target = surfaces[selected_surface]
        allowed_surfaces = {
            selected_surface,
            target["canonical_owner"],
            *target.get("related", []),
        }

    captured: dict[str, str] = {}
    for surface in catalog["surfaces"]:
        if surface["id"] not in allowed_surfaces:
            continue
        for story in surface.get("stories", []):
            story_id = story["id"]
            renderer = renderers.get(story_id)
            if renderer is None:
                continue
            widget = renderer()
            try:
                widget.show()
                app.processEvents()
                image_name = f"{story_id}.png"
                image_path = screenshots / image_name
                if not widget.grab().save(str(image_path)):
                    raise RuntimeError(f"could not save {image_path}")
                captured[story_id] = f"screenshots/{image_name}"
            finally:
                for timer in widget.findChildren(QTimer):
                    timer.stop()
                if isinstance(widget, OCRCorrectionsDialog):
                    widget._initial_rows = widget._all_table_rows()
                widget.close()
                widget.deleteLater()
                app.processEvents()
    return captured


def _review_packet(
    catalog: dict[str, Any], surface: dict[str, Any], captured: dict[str, str]
) -> dict[str, Any]:
    by_id = {item["id"]: item for item in catalog["surfaces"]}
    related_ids = list(
        dict.fromkeys([surface["canonical_owner"], *surface.get("related", [])])
    )

    def public_surface(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": value["id"],
            "title": value["title"],
            "family": value["family"],
            "audience": value["audience"],
            "mission": value["mission"],
            "canonical_owner": value["canonical_owner"],
            "related": list(value.get("related", [])),
            "contracts": [
                {"id": contract_id, "rule": catalog["contracts"][contract_id]}
                for contract_id in value.get("contracts", [])
            ],
            "stories": [
                {
                    "id": story["id"],
                    "title": story["title"],
                    "state": story["state"],
                    "screenshot": captured.get(story["id"]),
                }
                for story in value.get("stories", [])
            ],
        }

    return {
        "review_boundary": (
            "Review product behavior and visible interface only. Do not infer or "
            "request implementation details."
        ),
        "target": public_surface(surface),
        "related_surfaces": [
            public_surface(by_id[surface_id])
            for surface_id in related_ids
            if surface_id != surface["id"]
        ],
    }


def _write_catalog(
    catalog: dict[str, Any], output: Path, captured: dict[str, str]
) -> None:
    packets = output / "review-packets"
    packets.mkdir(parents=True, exist_ok=True)
    for surface in catalog["surfaces"]:
        packet = _review_packet(catalog, surface, captured)
        (packets / f"{surface['id']}.json").write_text(
            json.dumps(packet, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for surface in catalog["surfaces"]:
        groups[surface["family"]].append(surface)

    navigation: list[str] = []
    sections: list[str] = []
    for family, surfaces in groups.items():
        navigation.append(f"<h3>{html.escape(family)}</h3><ul>")
        sections.append(f"<section><h2>{html.escape(family)}</h2>")
        for surface in surfaces:
            surface_id = surface["id"]
            navigation.append(
                f'<li><a href="#{html.escape(surface_id)}">'
                f"{html.escape(surface['title'])}</a></li>"
            )
            owner = surface["canonical_owner"]
            relations = ", ".join(surface.get("related", [])) or "None"
            contracts = "".join(
                "<li><strong>"
                + html.escape(contract_id)
                + ":</strong> "
                + html.escape(catalog["contracts"][contract_id])
                + "</li>"
                for contract_id in surface.get("contracts", [])
            )
            stories = []
            for story in surface.get("stories", []):
                screenshot = captured.get(story["id"])
                image = (
                    f'<a href="{html.escape(screenshot)}"><img src="{html.escape(screenshot)}" '
                    f'alt="{html.escape(story["title"])}"></a>'
                    if screenshot
                    else '<div class="missing">Map only: deterministic render not added yet.</div>'
                )
                stories.append(
                    '<article class="story">'
                    f"<h4>{html.escape(story['title'])}</h4>"
                    f"<p>{html.escape(story['state'])}</p>{image}</article>"
                )
            if not stories:
                stories.append(
                    '<div class="missing">Mapped relationship; render when this surface is next changed.</div>'
                )
            sections.append(
                f'<article class="surface" id="{html.escape(surface_id)}">'
                f"<header><div><h3>{html.escape(surface['title'])}</h3>"
                f"<p>{html.escape(surface['mission'])}</p></div>"
                f'<a class="packet" href="review-packets/{html.escape(surface_id)}.json">Astra packet</a></header>'
                '<dl class="meta">'
                f"<dt>Audience</dt><dd>{html.escape(surface['audience'])}</dd>"
                f"<dt>Canonical owner</dt><dd>{html.escape(owner)}</dd>"
                f"<dt>Related</dt><dd>{html.escape(relations)}</dd></dl>"
                f'<ul class="contracts">{contracts}</ul>'
                f'<div class="stories">{"".join(stories)}</div></article>'
            )
        navigation.append("</ul>")
        sections.append("</section>")

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(catalog["title"])}</title>
<style>
:root {{ color-scheme: dark; font: 15px/1.5 system-ui, sans-serif; background:#171717; color:#eee; }}
* {{ box-sizing:border-box; }} body {{ margin:0; }} a {{ color:#8fcbff; }}
nav {{ position:fixed; inset:0 auto 0 0; width:270px; overflow:auto; padding:20px; background:#202020; border-right:1px solid #444; }}
nav h1 {{ font-size:18px; margin:0 0 20px; }} nav h3 {{ margin:18px 0 4px; color:#bbb; font-size:12px; text-transform:uppercase; }}
nav ul {{ list-style:none; margin:0; padding:0; }} nav li {{ margin:5px 0; }}
main {{ margin-left:270px; padding:28px; max-width:1500px; }} main > p {{ color:#bbb; max-width:850px; }}
section > h2 {{ margin-top:42px; border-bottom:1px solid #444; padding-bottom:8px; }}
.surface {{ background:#252525; border:1px solid #444; border-radius:12px; margin:18px 0; padding:20px; }}
.surface header {{ display:flex; gap:20px; align-items:start; justify-content:space-between; }} h3,h4,p {{ margin-top:0; }}
.packet {{ white-space:nowrap; border:1px solid #666; border-radius:7px; padding:6px 10px; text-decoration:none; }}
.meta {{ display:grid; grid-template-columns:max-content 1fr; gap:4px 14px; }} .meta dt {{ color:#aaa; }} .meta dd {{ margin:0; }}
.contracts {{ padding-left:20px; color:#ccc; }} .stories {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); gap:16px; margin-top:18px; }}
.story {{ background:#1b1b1b; border-radius:9px; padding:14px; }} .story img {{ display:block; width:100%; height:auto; border:1px solid #555; border-radius:6px; }}
.missing {{ color:#aaa; border:1px dashed #555; border-radius:7px; padding:12px; }}
@media (max-width:850px) {{ nav {{ position:static; width:auto; }} main {{ margin:0; padding:18px; }} .stories {{ grid-template-columns:1fr; }} }}
</style></head><body>
<nav><h1>{html.escape(catalog["title"])}</h1>{"".join(navigation)}</nav>
<main><h1>Interface catalog</h1><p>One product map, shared visual contracts and reproducible states of the real Qt widgets. Use each Astra packet with the target screenshots; it deliberately contains no source-code context.</p>{"".join(sections)}</main>
</body></html>"""
    (output / "index.html").write_text(page, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--surface", help="render a target and its mapped neighbours")
    parser.add_argument("--validate-only", action="store_true")
    arguments = parser.parse_args()

    catalog = load_catalog(arguments.catalog)
    if arguments.validate_only:
        print(f"Validated {len(catalog['surfaces'])} surfaces.")
        return 0
    arguments.output.mkdir(parents=True, exist_ok=True)
    captured = _render_stories(catalog, arguments.output, arguments.surface)
    _write_catalog(catalog, arguments.output, captured)
    print(
        f"Rendered {len(captured)} stories across {len(catalog['surfaces'])} mapped surfaces to {arguments.output / 'index.html'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
