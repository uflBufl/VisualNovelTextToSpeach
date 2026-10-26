#!/usr/bin/env python3
"""Render a small Storybook-like catalog from real VNTTS Qt widgets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import wave
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable, cast
from unittest.mock import Mock, patch

if TYPE_CHECKING:
    from PIL import Image
    from PySide6.QtCore import QProcess, QThreadPool
    from PySide6.QtWidgets import QApplication, QWidget

    from vntts.app import SettingsDialog
    from vntts.asset_ui import AssetManagerDialog
    from vntts.authoring.cohort_bundle import CohortBundleSample, CohortReviewBundle
    from vntts.authoring.cohort_bundle_ui import (
        CohortReviewBundleDialog,
        DecisionExecutor,
        PlaybackPreparer,
        SampleLoader,
    )
    from vntts.authoring.source_reference_quality_ui import SourceReferenceQualityDialog
    from vntts.authoring.workbench_ui import AuthoringWorkbenchDialog
    from vntts.calibration import CalibrationReviewDialog, DialogRegionOverlay
    from vntts.game_narrator_ui import GameNarratorDialog
    from vntts.ocr_review_ui import OCRReviewDialog
    from vntts.onboarding_ui import OnboardingWizard
    from vntts.pregeneration_audition_ui import VoiceAuditionPanel
    from vntts.pregeneration_queue import PregenerationInput
    from vntts.pregeneration_setup import GameContent, PregenerationJob
    from vntts.pregeneration_ui import OfflineAudioPreparationDialog
    from vntts.pregeneration_voices import VoicePlan
    from vntts.support import RuntimeSupportLog

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "ui-catalog.json"
DEFAULT_OUTPUT = PROJECT_ROOT / ".codex" / "ui-catalog"
sys.path.insert(0, str(PROJECT_ROOT))

from vntts.cleanup import cleanup_on_exit, temporary_directory  # noqa: E402
from vntts.pregeneration_audition import VoiceAuditionReference  # noqa: E402
from vntts.ui_catalog import (  # noqa: E402
    UICatalog,
    _write_catalog,
    load_catalog,
)

COHORT_CATALOG_ERROR = (
    "<b>Keep this diagnostic literal.</b> The exact sample authority could not be read.\n"
    + "The selected source changed while checksum-bound recordings were checked. " * 70
    + "\nEND OF ERROR: check the source and retry this operation."
)


def _cohort_catalog_bundle(
    root: Path,
) -> tuple[CohortReviewBundle, tuple[CohortBundleSample, ...]]:
    from copy import deepcopy

    from tests.authoring_fixtures import create_pending_cohort_workspace
    from vntts.authoring.cohort_bundle import (
        build_cohort_review_bundle,
        load_cohort_review_bundle_samples,
    )

    workspaces = tuple(
        create_pending_cohort_workspace(root / name)[0] for name in ("first", "second")
    )
    bundle, source_samples = load_cohort_review_bundle_samples(
        build_cohort_review_bundle(workspaces)
    )
    document = deepcopy(bundle.document)
    samples = []
    for source in source_samples:
        for index, (speaker, text, reason) in enumerate(
            (
                (
                    "The Keeper of the Northern Library",
                    "<b>Stay here</b>, timekeeper. "
                    + "The northern road is closed until dawn. " * 3,
                    "technical-attention: unusual pace; report-only",
                ),
                (
                    "Travelling Hotelier",
                    "Do you have shillings, miss?",
                    "clean representative sample",
                ),
                (
                    "Narrator",
                    "The storm finally passed.",
                    "longest dialogue",
                ),
            )
        ):
            samples.append(
                replace(
                    source,
                    required_reason=reason,
                    item=replace(
                        source.item,
                        queue_id=f"{source.item.queue_id}-catalog-{index}",
                        line_id=f"story:chapter-seven:line-{index + 1}",
                        speaker=speaker,
                        text=text,
                    ),
                )
            )
    for cohort in document["cohorts"]:
        cohort_samples = [
            sample
            for sample in samples
            if sample.workspace_id == cohort["workspace_id"]
        ]
        cohort["samples"] = [
            {
                **cohort["samples"][0],
                "queue_id": sample.item.queue_id,
                "line_id": sample.item.line_id,
                "required_reason": sample.required_reason,
            }
            for sample in cohort_samples
        ]
        cohort["item_count"] = 12
        source_document = next(
            value
            for value in document["sources"]
            if value["workspace_id"] == cohort["workspace_id"]
        )
        plan = source_document["plan"]
        plan_cohort = next(
            value
            for value in plan["cohorts"]
            if value["cohort_id"] == cohort["cohort_id"]
        )
        original_item = plan_cohort["items"][0]
        plan_cohort["items"] = [
            {
                **original_item,
                "queue_id": sample["queue_id"],
                "line_id": sample["line_id"],
                "sampled": True,
            }
            for sample in cohort["samples"]
        ]
        plan_cohort["items"].extend(
            {
                **original_item,
                "queue_id": f"catalog-unsampled-{index}",
                "sampled": False,
            }
            for index in range(9)
        )
        plan_cohort["sample_queue_ids"] = [
            sample.item.queue_id for sample in cohort_samples
        ]
        plan_cohort["item_count"] = plan["pending_item_count"] = 12
        plan["sample_item_count"] = len(cohort_samples)
    document["pending_item_count"] = 24
    document["sample_item_count"] = len(samples)
    return replace(bundle, document=document), tuple(samples)


def _cohort_catalog_operations(
    bundle: CohortReviewBundle, samples: tuple[CohortBundleSample, ...], state: str
) -> tuple[SampleLoader, DecisionExecutor, PlaybackPreparer]:
    from vntts.authoring.cohort_bundle import CohortBundleProjection, CohortReviewBundle

    load_attempts = 0

    def load(
        _bundle: CohortReviewBundle | Path,
    ) -> tuple[CohortReviewBundle, tuple[CohortBundleSample, ...]]:
        nonlocal load_attempts
        load_attempts += 1
        if load_attempts == 1 and state in {
            "load-failure",
            "compact-load-failure-end",
            "recovered",
        }:
            raise ValueError(COHORT_CATALOG_ERROR)
        if (
            isinstance(_bundle, CohortReviewBundle)
            and _bundle.document["cohort_count"] == 0
        ):
            return _bundle, ()
        return bundle, tuple(samples)

    save_attempts = 0

    def save(*_arguments: object) -> CohortBundleProjection:
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1 and state.removeprefix("compact-") in {
            "save-failure",
            "save-recovered",
        }:
            raise OSError(COHORT_CATALOG_ERROR)
        empty = replace(
            bundle,
            document={
                **bundle.document,
                "cohorts": [],
                "cohort_count": 0,
                "pending_item_count": 0,
                "sample_item_count": 0,
            },
        )
        current = samples[0]
        return CohortBundleProjection(
            bundle.bundle_id,
            current.workspace_id,
            current.cohort_id,
            (),
            "approved",
            empty,
        )

    def prepare(sample: CohortBundleSample) -> tuple[CohortBundleSample, bytes]:
        assert sample.item.audio is not None
        return sample, sample.item.audio.read_bytes()

    return load, save, prepare


def _cohort_review_story(resources: ExitStack, state: str) -> CohortReviewBundleDialog:
    from PySide6.QtWidgets import QApplication

    from tests.qt_task_fixtures import ManualThreadPool
    from vntts.async_ui import LatestTaskRunner
    from vntts.authoring import cohort_bundle_ui as ui

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    mode = state.removeprefix("compact-")
    mode = {"bad-reasons": "bad", "provenance-end": "technical-details"}.get(mode, mode)
    if state.startswith("compact"):
        font = app.font()
        resources.callback(app.setFont, app.font())
        font.setPointSize(22)
        app.setFont(font)
    root = Path(
        resources.enter_context(temporary_directory(prefix="vntts-ui-catalog-"))
    )
    bundle, samples = _cohort_catalog_bundle(root)
    pool = ManualThreadPool()
    resources.enter_context(
        patch.object(
            ui,
            "LatestTaskRunner",
            lambda parent, **_kwargs: LatestTaskRunner(
                parent, thread_pool=cast("QThreadPool", pool)
            ),
        )
    )
    media_status = ui.QMediaPlayer.MediaStatus
    player = resources.enter_context(patch.object(ui, "QMediaPlayer"))
    player.MediaStatus = media_status
    player.return_value.play_bytes.return_value = object()
    resources.enter_context(
        patch.object(
            ui.QMessageBox,
            "warning",
            return_value=ui.QMessageBox.StandardButton.Discard,
        )
    )
    load, save, prepare = _cohort_catalog_operations(bundle, samples, state)
    dialog = ui.CohortReviewBundleDialog(
        bundle,
        sample_loader=load,
        playback_preparer=prepare,
        decision_executor=save,
        confirmer=lambda *_args: True,
    )

    def finish() -> None:
        pool.run_next()
        app.processEvents()

    finish()
    if state == "recovered":
        dialog.retry_load.click()
        finish()
    _set_cohort_playback_story(dialog, mode, finish)
    _set_cohort_write_story(dialog, mode, finish, root)
    if mode in {"technical-details", "technical-table-end"}:
        dialog.technical_details.setChecked(True)
        dialog.decision_context.technical_toggle.click()
    if mode == "second-cohort":
        dialog.cohort_choice.setCurrentIndex(1)
    if mode in {"table", "second-cohort"}:
        dialog.table.selectRow(1)
    if state == "quality-baseline":
        dialog.quality_gate_context = ui._QualityGateContext(
            "catalog-accepted-voice-gate",
            "Rhiannon",
            "The Keeper of the Northern Library",
            2,
        )
        dialog._apply_loaded_bundle(bundle, samples)
    if state.startswith("compact"):
        dialog.resize(dialog.minimumSize())
    return dialog


def _set_cohort_playback_story(
    dialog: CohortReviewBundleDialog, mode: str, finish: Callable[[], None]
) -> None:
    from PySide6.QtWidgets import QApplication

    from vntts.authoring.cohort_bundle_ui import QMediaPlayer

    if mode in {
        "heard",
        "bad",
        "saving",
        "save-failure",
        "save-recovered",
        "complete",
        "close-pending",
        "observation-failure",
        "observation-recovered",
        "table",
    }:
        for row in range(1 if mode == "table" else dialog.table.rowCount()):
            dialog.table.selectRow(row)
            dialog.replay.click()
            finish()
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            QApplication.processEvents()
        dialog.table.selectRow(0)
    if mode == "bad":
        dialog.mark_bad.click()
        dialog.defect_toggle.setChecked(True)
    if mode in {"preparing", "playback", "stopped", "audio-failure"}:
        dialog.replay.click()
        if mode != "preparing":
            finish()
        if mode == "stopped":
            dialog.stop.click()
        elif mode == "audio-failure":
            dialog._media_error(None, COHORT_CATALOG_ERROR)


def _set_cohort_write_story(
    dialog: CohortReviewBundleDialog, mode: str, finish: Callable[[], None], root: Path
) -> None:
    if mode in {
        "saving",
        "save-failure",
        "save-recovered",
        "complete",
        "close-pending",
    }:
        dialog.accept_button.click()
        if mode == "close-pending":
            dialog.close()
        elif mode != "saving":
            finish()
            if mode == "save-recovered":
                dialog.retry_load.click()
                finish()
                dialog.accept_button.click()
                finish()
            if mode in {"complete", "save-recovered"}:
                finish()
    if mode.startswith("observation-"):
        dialog._checkpoint_observations_enabled = True
        dialog.observation_writer = Mock(
            side_effect=[OSError(COHORT_CATALOG_ERROR), root / "progress.json"]
        )
        dialog._checkpoint_observations()
        finish()
        if mode == "observation-recovered":
            dialog.retry_progress.click()
            finish()


def _prepare_cohort_screenshot(
    widget: QWidget, story_id: str, app: QApplication
) -> None:
    if not story_id.startswith("cohort-review."):
        return
    from vntts.authoring.cohort_bundle_ui import CohortReviewBundleDialog

    assert isinstance(widget, CohortReviewBundleDialog)
    app.processEvents()
    scroll_target = {
        "cohort-review.compact-actions": widget.next,
        "cohort-review.compact-bad": widget.defect_toggle,
        "cohort-review.compact-bad-reasons": tuple(widget.defect_checks.values())[-1],
        "cohort-review.compact-save-failure": widget.error_details,
        "cohort-review.quality-baseline": widget.quality_baseline,
    }.get(story_id)
    if scroll_target is not None:
        widget.review_scroll.ensureWidgetVisible(scroll_target)
    elif story_id.endswith(("compact-table", "compact-technical-table-end")):
        widget.review_scroll.ensureWidgetVisible(widget.table)
        widget.review_scroll.verticalScrollBar().setValue(widget.table.y() - 12)
        if story_id.endswith("compact-technical-table-end"):
            widget.table.horizontalScrollBar().setValue(
                widget.table.horizontalScrollBar().maximum()
            )
    elif story_id.endswith("compact-load-failure-end"):
        widget.review_scroll.ensureWidgetVisible(widget.error_details)
        widget.error_details.verticalScrollBar().setValue(
            widget.error_details.verticalScrollBar().maximum()
        )
    elif story_id.endswith(("technical-details", "compact-provenance-end")):
        widget.review_scroll.verticalScrollBar().setValue(
            widget.review_scroll.verticalScrollBar().maximum()
        )
        if story_id.endswith("compact-technical-details"):
            widget.table.horizontalScrollBar().setValue(
                widget.table.horizontalScrollBar().maximum()
            )
        if story_id.endswith("compact-provenance-end"):
            widget.decision_context.technical.verticalScrollBar().setValue(
                widget.decision_context.technical.verticalScrollBar().maximum()
            )
    app.processEvents()


def _set_long_authoring_review(dialog: AuthoringWorkbenchDialog, state: str) -> None:
    if not state.startswith("review-long-names"):
        return
    first = dialog._selected_review_item()
    assert first is not None
    assert dialog.summary is not None
    dialog._all_reviews = (
        replace(
            first,
            speaker="The Keeper of the Northern Library",
            voice_character="Centurion in ceremonial attire",
            text="The storm has passed, but we cannot leave before dawn.",
        ),
        replace(
            first,
            queue_id="catalog-long-review-2",
            line_id="catalog-long-line-2",
            speaker="The Traveling Hotelier",
            voice_character="Narrator voice, selected for this story",
            text="You do have shillings, do you not, miss?",
        ),
    )
    dialog._apply_review_filters()
    dialog.summary = replace(dialog.summary, generated=2)
    dialog._show_counts()
    if state.startswith("review-long-names-second"):
        dialog.review_table.setCurrentCell(1, 7)


AUTHORING_CATALOG_ERROR = (
    "The saved workspace could not be read. <b>Keep this diagnostic literal.</b>\n"
    + "A workspace snapshot changed while saved generation results were being checked. "
    * 60
    + "\nEND OF ERROR: retry loading the workspace after checking the diagnostic."
)


def _authoring_catalog_workspace(root: Path, state: str) -> Path:
    from tests.authoring_fixtures import create_test_workspace
    from vntts.authoring.generation_lease import process_started_at
    from vntts.document_identity import sha256_file
    from vntts.generation_database import read_state, write_state

    workspace = create_test_workspace(root)[2].directory
    state_path = workspace / "generated-audio" / "generation-state.sqlite3"
    document = read_state(state_path)
    items = document["items"]
    assert isinstance(items, dict)
    if state == "ready" or state.startswith("selection-refresh-"):
        document["active"] = None
        document["items"] = {}
    elif state.startswith(("review", "compact", "load-failure")) or state in {
        "recovered",
        "technical-details",
    }:
        document["active"] = None
        item = next(iter(items.values()))
        assert isinstance(item, dict)
        item["status"] = "generated"
        item["review_status"] = "pending_review"
    elif state == "approved":
        document["active"] = None
    elif state == "failed":
        document["active"] = None
        queue_id = next(iter(items))
        items[queue_id] = {
            "status": "failed",
            "attempts": 3,
            "seed": 2,
            "last_error": "Voice synthesis ended without usable speech",
            "updated_at": "2026-08-17T00:00:00+00:00",
        }
    write_state(state_path, document)
    if state == "running-elsewhere" or state.startswith("stopping"):
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
    return workspace


def _hold_authoring_collection_refresh(
    dialog: AuthoringWorkbenchDialog, state: str, resources: ExitStack
) -> None:
    from PySide6.QtCore import Qt

    dialog._synchronous_projection = False
    resources.enter_context(patch.object(dialog._projection_runner, "start"))
    if state == "selection-refresh-pending":
        resources.enter_context(patch.object(dialog, "_refresh_collection_selection"))
    dialog.choose_collections.click()
    main = next(
        item
        for index in range(dialog.collection_tree.topLevelItemCount())
        if (item := dialog.collection_tree.topLevelItem(index)) is not None
        and item.data(0, Qt.ItemDataRole.UserRole) == "main"
    )
    main.setCheckState(0, Qt.CheckState.Unchecked)


def _authoring_catalog_process(state: str) -> QProcess | None:
    from PySide6.QtCore import QProcess

    if not state.startswith("stopping"):
        return None
    process = Mock(spec=QProcess)
    process.state.return_value = QProcess.ProcessState.Running
    process.processId.return_value = os.getpid()
    process.readAllStandardOutput.return_value.data.return_value = b""
    return cast(QProcess, process)


def _stop_authoring_catalog_process(
    dialog: AuthoringWorkbenchDialog, state: str
) -> None:
    from PySide6.QtWidgets import QMessageBox

    dialog._process_started()
    if state.startswith("stopping-close"):
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ):
            dialog.close()
    else:
        dialog.stop_generation.click()
    dialog.refresh()


def _authoring_workbench_story(
    resources: ExitStack, state: str
) -> AuthoringWorkbenchDialog:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from vntts.authoring.workbench_ui import AuthoringWorkbenchDialog

    root = Path(
        resources.enter_context(temporary_directory(prefix="vntts-ui-catalog-"))
    )
    enlarged = "22pt" in state
    if enlarged:
        font = QApplication.font()
        resources.callback(QApplication.setFont, QApplication.font())
        font.setPointSize(22)
        QApplication.setFont(font)
    workspace = _authoring_catalog_workspace(root, state)
    process = _authoring_catalog_process(state)
    dialog = AuthoringWorkbenchDialog(
        workspace,
        settings=QSettings(str(root / "ui.ini"), QSettings.Format.IniFormat),
        process=process,
        synchronous_projection=True,
    )
    dialog.status_timer.stop()
    _set_long_authoring_review(dialog, state)
    if process is not None:
        _stop_authoring_catalog_process(dialog, state)
    if state in {"filtered-empty", "filters-reset"}:
        dialog.review_status.setCurrentText("Awaiting review")
        if state == "filters-reset":
            dialog.reset_review_filters.click()
    if state.startswith("load-failure") or state == "recovered":
        with patch.object(
            dialog,
            "_projection_loader",
            side_effect=ValueError(AUTHORING_CATALOG_ERROR),
        ):
            dialog.refresh()
        if state == "recovered":
            dialog.reload_authority.click()
    if state.startswith("selection-refresh-"):
        _hold_authoring_collection_refresh(dialog, state, resources)
    if state == "technical-details":
        dialog.technical.header.click()
        dialog.show_technical_columns.click()
        dialog._append_process_log(
            "Generation completed: 1 saved recording awaits review.\n"
            "Review decisions revalidate the exact recording before saving.\n"
        )
    if state in {
        "compact",
        "compact-actions",
        "review-long-names",
        "review-long-names-second",
        "review-long-names-details",
        "review-long-names-second-details",
    }:
        font = dialog.font()
        font.setPointSize(max(font.pointSize() + 3, 16))
        dialog.setFont(font)
        dialog.resize(dialog.minimumSize())
    if enlarged:
        dialog.resize(dialog.minimumSize())
    return dialog


def _prepare_authoring_screenshot(
    dialog: AuthoringWorkbenchDialog, story_id: str, app: QApplication
) -> None:
    for _ in range(3):
        app.processEvents()
    if story_id.endswith("review-play-focus"):
        dialog.review_play.setFocus()
    elif story_id.endswith(
        (
            "compact-actions",
            "review-long-names-details",
            "review-long-names-second-details",
            "stopping-close-22pt-details",
        )
    ):
        target = dialog.approve if "stopping-close" in story_id else dialog.review_play
        dialog.workbench_scroll.ensureWidgetVisible(target)
    elif story_id.endswith("technical-details"):
        dialog._reveal_inspector_control(dialog.copy_diagnostics)
    elif "selection-refresh-" in story_id:
        dialog._reveal_inspector_control(dialog.collection_tree)
    elif story_id.endswith(("stopping-22pt", "stopping-close-22pt")):
        dialog.workbench_scroll.ensureWidgetVisible(dialog.stop_generation)
    elif story_id.endswith("load-failure-22pt-end"):
        dialog.workspace_error.verticalScrollBar().setValue(
            dialog.workspace_error.verticalScrollBar().maximum()
        )
    for _ in range(3):
        app.processEvents()


def _voice_overview_story(
    dialog: OfflineAudioPreparationDialog, state: str
) -> OfflineAudioPreparationDialog:
    compact = state.startswith(("compact-", "many-compact"))
    state = state.removeprefix("compact-").removesuffix("-actions")
    plan = dialog._voice_plan
    assert plan is not None
    first = plan.groups[0]
    if state.startswith("many"):
        speech_lines = _catalog_selected_speech_lines(dialog)
        groups = tuple(
            replace(
                first,
                group_id=f"overview-{index}",
                character=f"Northern Library Keeper {index + 1}"
                if index % 4 == 0
                else f"Traveler {index + 1}",
                routing_role=None,
                source_character="The Knight of the Northern Guard"
                if index % 3 == 0
                else "Centurion",
                line_ids=speech_lines[index::24],
                resolution="saved-player-decision" if index % 2 else first.resolution,
            )
            for index in range(24)
        )
        plan = replace(plan, groups=groups)
    if state in {"draft", "missing", "review", "long-narrator"}:
        dialog.game_narrator_chooser = None
        dialog.narrator_controls.show()
        dialog.confirmed_narrator.hide()
        dialog.edit_confirmed_narrator.hide()
    if state == "review":
        plan = replace(
            plan,
            groups=(
                replace(
                    first,
                    route="needs-audition",
                    resolution="ambiguous-voice-evidence",
                    source_id="preset:alba",
                    reference_sha256s=(),
                    narrator_candidate=None,
                ),
                *plan.groups[1:],
            ),
        )
    if state == "empty-filter":
        plan = replace(plan, groups=(replace(first, character="Centurion"),))
    dialog._voice_plan = plan
    dialog._show_voice_confirmation(plan)
    if state in {"draft", "missing"}:
        dialog.narrator_choice.setCurrentIndex(
            dialog.narrator_choice.findData("preset:marius") if state == "draft" else 0
        )
        dialog.voice_routes.setCurrentRow(dialog.voice_routes.count() - 1)
    if state == "long-narrator":
        dialog.narrator_choice.addItem(
            "The Narrator of the Northern Library and Traveling Company",
            "character:long-narrator",
        )
        dialog.narrator_choice.setCurrentIndex(dialog.narrator_choice.count() - 1)
        dialog.voice_routes.setCurrentRow(dialog.voice_routes.count() - 1)
    if state == "empty-filter":
        dialog.show_all_voice_routes.setChecked(True)
    if state.endswith("details"):
        dialog.preparation_details_toggle.setChecked(True)
        dialog.more_voice_actions_toggle.setChecked(True)
    if compact:
        font = dialog.font()
        font.setPointSize(max(20, font.pointSize() * 2))
        dialog.setFont(font)
        dialog.resize(620, 440)
    return dialog


def _catalog_selected_speech_lines(
    dialog: OfflineAudioPreparationDialog,
) -> tuple[str, ...]:
    job = dialog._job
    assert job is not None
    return tuple(
        line_id
        for content in dialog._content
        for selection in content.selections
        if selection.selection_id in job.selected_story_ids
        for line_id in selection.line_ids[selection.original_audio_lines :]
    )


def _narrator_only_overview_story(
    dialog: OfflineAudioPreparationDialog,
) -> OfflineAudioPreparationDialog:
    plan = dialog._voice_plan
    assert plan is not None
    dialog._voice_plan = replace(
        plan,
        groups=(
            replace(
                plan.groups[0],
                character="Narrator",
                routing_role="Narrator",
                route="narrator",
                source_id="preset:alba",
                source_character=None,
                line_ids=_catalog_selected_speech_lines(dialog),
                reference_sha256s=(),
                resolution="narrator-dialogue",
            ),
        ),
    )
    return _voice_overview_story(dialog, "narrator-only")


def _voice_overview_without_references(
    dialog: OfflineAudioPreparationDialog,
) -> OfflineAudioPreparationDialog:
    plan = dialog._voice_plan
    assert plan is not None
    unavailable = replace(
        plan.groups[0], candidate_inventory=(), candidates=(), narrator_candidate=None
    )
    dialog._voice_plan = replace(plan, groups=(unavailable, *plan.groups[1:]))
    dialog._show_voice_confirmation(dialog._voice_plan)
    return dialog


def _shared_character_overview_story(
    dialog: OfflineAudioPreparationDialog, state: str
) -> OfflineAudioPreparationDialog:
    plan = dialog._voice_plan
    assert plan is not None
    first = plan.groups[0]
    other_lines = {line_id for group in plan.groups[1:] for line_id in group.line_ids}
    shared_lines = tuple(
        line_id
        for line_id in _catalog_selected_speech_lines(dialog)
        if line_id not in other_lines
    )
    dialog.voice_library.select(
        first.character, route="voice", source_id=first.source_id, method="manual"
    )
    dialog.voice_library.link_person(first.character, "Another name")
    dialog._voice_plan = replace(
        plan,
        groups=(
            replace(
                first,
                line_ids=shared_lines,
                resolution="saved-voice-assignment",
            ),
            *plan.groups[1:],
        ),
    )
    return _voice_overview_story(dialog, state)


def _calibration_overlay_story(state: str, scene: Image.Image) -> DialogRegionOverlay:
    from PIL import Image, ImageDraw
    from PySide6.QtCore import QEvent, QPoint, QRect, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QDialog

    from vntts.calibration import DialogRegionOverlay
    from vntts.ocr import DialogRegion
    from vntts.qt_images import pixmap_from_pil

    overlay = DialogRegionOverlay(
        output=Path("/private/tmp/vntts-ui-catalog-unused-region.json"),
        background=scene,
        capture_region=DialogRegion(0.1, 0.6, 0.8, 0.35),
    )
    overlay.resize(800, 450)
    if state == "overlay-top-selected":
        scene = Image.new("RGB", (800, 450), "#272936")
        painter = ImageDraw.Draw(scene)
        painter.rectangle((75, 25, 730, 170), fill="#141822", outline="#7a8198")
        painter.text((105, 50), "SELONE", fill="#e9e9e9", font_size=20)
        painter.text(
            (105, 95), "The dialogue appears at the top.", fill="#e9e9e9", font_size=20
        )
        overlay.background = scene
        overlay.background_pixmap = pixmap_from_pil(scene)
        overlay.origin = QPoint(85, 35)
        overlay.current = QPoint(735, 175)
    if state in {
        "overlay-selected",
        "overlay-save-failure",
        "overlay-compact-save-error",
    }:
        overlay.origin = QPoint(85, 285)
        overlay.current = QPoint(735, 425)
    if state == "overlay-compact-save-error":
        overlay.resize(620, 440)
        overlay.origin = QPoint(66, 278)
        overlay.current = QPoint(570, 415)
    if state == "overlay-keyboard-selection":
        overlay.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.NoModifier,
            )
        )
    if state in {"overlay-save-failure", "overlay-compact-save-error"}:
        overlay.reviewer = Mock(
            return_value=Mock(exec=Mock(return_value=QDialog.DialogCode.Accepted))
        )
        overlay.save_region = Mock(
            side_effect=OSError("The configuration folder is not writable")
        )
        assert overlay.origin is not None and overlay.current is not None
        overlay._review_rectangle(QRect(overlay.origin, overlay.current))
    if state == "overlay-compact-save-error":
        font = overlay.font()
        font.setPointSize(font.pointSize() + 4)
        overlay.setFont(font)
    return overlay


def _calibration_review_story(
    state: str, scene: Image.Image, pool: QThreadPool
) -> CalibrationReviewDialog:
    from PIL import Image, ImageDraw

    from vntts.calibration import CalibrationReviewDialog
    from vntts.ocr import OCRResult

    long_review = state.startswith("review-long-")
    text = "I have returned. We can continue our journey."
    if long_review:
        lines = [
            f"{index + 1}. We crossed the northern library and waited for the storm to pass."
            for index in range(16)
        ]
        text = "\n".join([*lines, "END OF DIALOGUE"])
        scene = Image.new("RGB", (1000, 1100), "#141822")
        painter = ImageDraw.Draw(scene)
        painter.text((40, 30), "SELONE", fill="#e9e9e9", font_size=30)
        painter.multiline_text((40, 90), text, fill="#e9e9e9", spacing=22, font_size=20)
    crop = scene if long_review else scene.crop((85, 285, 735, 425))
    dialog = CalibrationReviewDialog(crop, thread_pool=pool)
    if state in {"review-recognized", "review-compact"} or long_review:
        dialog._recognition_finished(
            OCRResult(
                "Selone",
                text,
                94.5,
                "balanced",
                1,
            ),
            None,
        )
    elif state in {"review-failure", "review-failure-compact"}:
        dialog._recognition_finished(
            None,
            OSError(
                "OCR runtime unavailable while checking the selected dialogue area"
            ),
        )
    elif state == "review-empty":
        dialog._recognition_finished(OCRResult("Narrator", "", 0.0, "none", 0), None)
    elif state == "review-unreadable-name":
        dialog._recognition_finished(
            OCRResult(
                "Narrator",
                "I have returned. We can continue our journey.",
                47.25,
                "balanced",
                1,
                speaker_confidence=0.0,
                dialogue_confidence=94.5,
                speaker_note="Visible name could not be read",
            ),
            None,
        )
    if state in {"review-compact", "review-failure-compact"} or long_review:
        dialog.resize(620, 430)
        font = dialog.font()
        font.setPointSize(18 if long_review else font.pointSize() + 4)
        dialog.setFont(font)
    return dialog


def _scroll_calibration_review_to_end(widget: QWidget, app: QApplication) -> None:
    from PySide6.QtWidgets import QScrollArea

    from vntts.calibration import CalibrationReviewDialog

    if isinstance(widget, CalibrationReviewDialog):
        widget.body_scroll.ensureWidgetVisible(widget.result_text)
        scroll = widget.result_text.verticalScrollBar()
    else:
        image_scroll = widget.findChild(QScrollArea)
        assert image_scroll is not None
        scroll = image_scroll.verticalScrollBar()
    scroll.setValue(scroll.maximum())
    if isinstance(widget, CalibrationReviewDialog):
        cursor = widget.result_text.document().find("END OF DIALOGUE")
        assert not cursor.isNull()
        cursor.clearSelection()
        widget.result_text.setTextCursor(cursor)
        widget.result_text.ensureCursorVisible()
    app.processEvents()


def _settings_story(resources: ExitStack, state: str) -> SettingsDialog:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from vntts.app import SettingsDialog
    from vntts.settings import AppSettings
    from vntts.voice_library import VoiceLibrary

    root = Path(
        resources.enter_context(temporary_directory(prefix="vntts-ui-catalog-"))
    )
    reference = root / "reference.wav"
    reference.touch()
    if state.startswith("compact-"):
        font = QApplication.font()
        resources.callback(QApplication.setFont, QApplication.font())
        font.setPointSize(22)
        QApplication.setFont(font)
    settings = AppSettings(
        onboarding_completed=True,
        capture_mode="window",
        speech_backend="pocket-tts" if "offline-moss" in state else "moss-tts",
        offline_speech_backend="moss-tts",
        tts_profile="stable",
        game_window_title="Reverse: 1999",
        tts_speaker_wav=str(reference),
        screenshot_directory=str(root / "Screenshots"),
        ocr_diagnostics_directory=str(root / "OCR Diagnostics"),
        speech_rate_percent=125 if state.startswith("playback-speed") else 100,
    )
    platform = "win32" if state == "shortcuts-windows" else sys.platform
    with patch("vntts.app.sys", SimpleNamespace(platform=platform)):
        dialog = SettingsDialog(
            settings, voice_library=VoiceLibrary(root / "voice-library")
        )
        section = {
            "shortcuts": 0,
            "shortcuts-windows": 0,
            "capture": 1,
            "capture-window": 1,
            "advanced-capture": 1,
            "compact-capture": 1,
            "compact-shortcuts": 0,
            "compact-playback": 3,
            "compact-application": 4,
            "playback": 3,
            "playback-advanced": 3,
            "playback-speed-reset-needed": 3,
            "playback-speed-reset": 3,
            "application": 4,
            "checkbox-focus": 4,
        }.get(state, 2)
        dialog.section_navigation.setCurrentIndex(section)
    dialog.advanced_settings.setChecked("advanced" in state)
    dialog.resize(760, 600)
    if state.startswith("compact-"):
        dialog.resize(620, 500)
    if state == "capture-window":
        dialog.game_window.setCurrentText(
            "Reverse: 1999 - A Particularly Long Game Window Title"
        )
    if state == "checkbox-focus":
        QTimer.singleShot(0, dialog.warm_up_voices.setFocus)
    if state == "validation-error":
        dialog.narrator_reference.setText("/missing/voice-reference.wav")
        dialog.save_button.click()
    if "validation-errors" in state or state == "validation-recovered":
        _settings_validation_story(dialog, root, state)
    if state == "playback-speed-reset":
        dialog.reset_speech_rate.click()
    return dialog


def _settings_validation_story(dialog: SettingsDialog, root: Path, state: str) -> None:
    blocked = root / "not-a-directory"
    blocked.touch()
    dialog.screenshot_directory.setText(str(blocked))
    dialog.retain_uncertain_frames.setChecked(True)
    dialog.ocr_diagnostics_directory.setText(str(blocked))
    dialog.game_window.setCurrentText("")
    dialog.speech_backend.setCurrentIndex(dialog.speech_backend.findData("coqui-xtts"))
    dialog.xtts_terms.setChecked(False)
    dialog.narrator_reference.setText(str(root / "missing-reference.wav"))
    dialog.live_speaker_corpus.setText(str(root / "missing-corpus.json"))
    dialog.game_pack.setText(str(root / "missing-pack.json"))
    dialog.save_button.click()
    if state == "validation-recovered":
        dialog.screenshot_directory.setText(
            dialog.original_settings.screenshot_directory
        )
        dialog.ocr_diagnostics_directory.setText(
            dialog.original_settings.ocr_diagnostics_directory
        )
        dialog.game_window.setCurrentText(
            dialog.original_settings.game_window_title or ""
        )
        dialog.xtts_terms.setChecked(True)
        dialog.narrator_reference.setText(
            dialog.original_settings.tts_speaker_wav or ""
        )
        dialog.live_speaker_corpus.clear()
        dialog.game_pack.clear()


def _prepare_settings_screenshot(
    widget: QWidget, story_id: str, app: QApplication
) -> None:
    if not story_id.startswith("settings."):
        return
    from vntts.app import SettingsDialog

    assert isinstance(widget, SettingsDialog)
    app.processEvents()
    if story_id.endswith("compact-advanced-speech-scrolled"):
        bar = widget.settings_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
    if "compact-validation-errors" in story_id:
        widget.save_button.click()
        app.processEvents()
        bar = widget.validation_summary.verticalScrollBar()
        for _ in range(3):
            bar.setValue(bar.maximum())
            app.processEvents()
        if not story_id.endswith("-end"):
            bar.setValue(0)
    app.processEvents()


def _prepare_offline_preparation_screenshot(
    widget: QWidget, story_id: str, app: QApplication
) -> None:
    from vntts.pregeneration_ui import OfflineAudioPreparationDialog

    if story_id.startswith(
        "offline-preparation.voice-overview-compact-"
    ) and story_id.endswith("-actions"):
        assert isinstance(widget, OfflineAudioPreparationDialog)
        widget.content_scroll.verticalScrollBar().setValue(
            widget.content_scroll.verticalScrollBar().maximum()
        )
        app.processEvents()
    if story_id in {
        "offline-preparation.voice-overview-many-compact-details-end",
        "offline-preparation.voice-overview-shared-character-compact-actions",
    }:
        assert isinstance(widget, OfflineAudioPreparationDialog)
        widget.content_scroll.verticalScrollBar().setValue(
            widget.content_scroll.verticalScrollBar().maximum()
        )
        app.processEvents()
    if story_id == "offline-preparation.voice-overview-many-compact-more-actions":
        assert isinstance(widget, OfflineAudioPreparationDialog)
        widget.content_scroll.ensureWidgetVisible(widget.more_voice_actions)
        app.processEvents()
    if story_id in {
        "offline-preparation.voice-overview-many-compact-table",
        "offline-preparation.voice-overview-shared-character-compact-table",
        "offline-preparation.voice-overview-compact-long-narrator-table",
    }:
        assert isinstance(widget, OfflineAudioPreparationDialog)
        widget.content_scroll.ensureWidgetVisible(widget.voice_routes)
        app.processEvents()
    if story_id == "offline-preparation.voice-overview-many-compact-details":
        assert isinstance(widget, OfflineAudioPreparationDialog)
        widget.content_scroll.ensureWidgetVisible(widget.voice_engine_details)
        app.processEvents()
    if story_id == "offline-preparation.voice-confirmation-compact-scrolled":
        assert isinstance(widget, OfflineAudioPreparationDialog)
        widget.content_scroll.verticalScrollBar().setValue(
            widget.content_scroll.verticalScrollBar().maximum()
        )
        app.processEvents()
    if story_id == "offline-preparation.voice-confirmation-compact-last-route":
        assert isinstance(widget, OfflineAudioPreparationDialog)
        widget.content_scroll.verticalScrollBar().setValue(
            widget.content_scroll.verticalScrollBar().maximum()
        )
        widget.voice_routes.setCurrentRow(widget.voice_routes.count() - 1)
        widget.voice_routes.verticalScrollBar().setValue(
            widget.voice_routes.verticalScrollBar().maximum()
        )
        app.processEvents()


def _prepare_voice_audition_screenshot(
    widget: QWidget, story_id: str, app: QApplication
) -> None:
    from PySide6.QtWidgets import QScrollArea

    from vntts.game_narrator_ui import GameNarratorDialog

    if story_id in {
        "voice-audition.compact-preview",
        "voice-audition.compact-preview-playing",
        "voice-audition.compact-preview-failure",
        "voice-audition.compact-save-waiting",
        "voice-audition.compact-save-failure",
    }:
        assert isinstance(widget, GameNarratorDialog)
        target = (
            widget.voice_panel.status
            if story_id.endswith(("failure", "waiting"))
            else widget.voice_panel.a_play
        )
        scroll = widget.findChild(QScrollArea)
        assert scroll is not None
        scroll.ensureWidgetVisible(target)
        app.processEvents()


def _prepare_catalog_screenshot(
    widget: QWidget, story_id: str, app: QApplication
) -> None:
    from vntts.authoring.workbench_ui import AuthoringWorkbenchDialog

    _prepare_ocr_review_screenshot(widget, story_id, app)
    _prepare_settings_screenshot(widget, story_id, app)
    _prepare_cohort_screenshot(widget, story_id, app)
    if story_id.startswith("authoring-workbench."):
        assert isinstance(widget, AuthoringWorkbenchDialog)
        _prepare_authoring_screenshot(widget, story_id, app)
    if story_id in {
        "calibration.review-long-compact-scrolled",
        "calibration.review-long-enlarged-scrolled",
    }:
        _scroll_calibration_review_to_end(widget, app)
    _prepare_offline_preparation_screenshot(widget, story_id, app)
    if story_id in {
        "diagnostics.technical",
        "diagnostics.missing-images",
        "diagnostics.compact-technical",
    }:
        from vntts.diagnostics_ui import DiagnosticsDialog

        assert isinstance(widget, DiagnosticsDialog)
        widget.body_scroll.ensureWidgetVisible(widget.speaker_processed_preview)
        app.processEvents()
    _prepare_voice_audition_screenshot(widget, story_id, app)


def _close_catalog_widget(widget: QWidget) -> None:
    from PySide6.QtCore import QCoreApplication, QEvent, QProcess, QTimer
    from PySide6.QtWidgets import QApplication

    from vntts.authoring.workbench_ui import AuthoringWorkbenchDialog
    from vntts.ocr_corrections_ui import OCRCorrectionsDialog

    for timer in widget.findChildren(QTimer):
        timer.stop()
    if isinstance(widget, AuthoringWorkbenchDialog) and isinstance(
        widget.process, Mock
    ):
        widget.process.state.return_value = QProcess.ProcessState.NotRunning
    if isinstance(widget, OCRCorrectionsDialog):
        widget._initial_rows = widget._all_table_rows()
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def _compact_voice_audition(dialog: OfflineAudioPreparationDialog) -> None:
    assert dialog._voice_plan is not None
    first = dialog._voice_plan.groups[0]
    long_text = (
        "The storm has passed, but we cannot leave before dawn. "
        "We must find every missing traveller before the northern road closes again."
    )
    candidate = replace(
        first.candidate_inventory[0],
        source_character="The Keeper of the Northern Library",
        source_excerpts=(long_text,),
    )
    first = replace(
        first,
        character="The Traveling Hotelier of the Northern Province",
        routing_role="The Traveling Hotelier of the Northern Province",
        speakers=("The Traveling Hotelier of the Northern Province",),
        candidate_inventory=(candidate, *first.candidate_inventory[1:]),
        candidates=(candidate, *first.candidates[1:]),
        sample_text=long_text,
    )
    dialog._voice_plan = replace(
        dialog._voice_plan, groups=(first, *dialog._voice_plan.groups[1:])
    )
    dialog._show_voice_confirmation(dialog._voice_plan)
    font = dialog.font()
    font.setPointSizeF(font.pointSizeF() * 2)
    dialog.setFont(font)
    dialog.resize(620, 440)


def _anonymous_voice_plan(plan: VoicePlan) -> VoicePlan:
    group = plan.groups[0]
    candidate = replace(
        group.candidate_inventory[0],
        source_character="Player candidate Rhiannon abcdef123456",
    )
    group = replace(
        group,
        candidate_inventory=(candidate, *group.candidate_inventory[1:]),
        candidates=(candidate, *group.candidates[1:]),
    )
    return replace(plan, groups=(group, *plan.groups[1:]))


def _voice_audition_story(
    state: str,
    dialog: OfflineAudioPreparationDialog,
    resources: ExitStack,
    write_sample: Callable[[Path], None],
) -> GameNarratorDialog:
    from vntts.game_narrator_ui import GameNarratorDialog

    if state.startswith("compact"):
        _compact_voice_audition(dialog)
        state = state.removeprefix("compact-")
    if state == "anonymous-source":
        assert dialog._voice_plan is not None
        dialog._voice_plan = _anonymous_voice_plan(dialog._voice_plan)
    assert dialog._voice_plan is not None
    audition_service = dialog.audition_service
    assert isinstance(audition_service, Mock)
    editor = GameNarratorDialog(
        dialog.settings.updated(pocket_gated_model_accepted=True),
        importer=dialog.importer,
        preview_service=audition_service,
        thread_pool=dialog.voice_runner.thread_pool,
        player=dialog._narrator_player,
        voice_library=dialog.voice_library,
    )
    editor._suppress_auto_discovery = True
    from PySide6.QtWidgets import QApplication

    QApplication.processEvents()
    editor.set_voice_context(
        dialog._voice_plan,
        character=dialog._voice_plan.groups[0].character,
        story_titles=("Chapter 7", "Chapter 8"),
    )
    editor.source.setCurrentIndex(editor.source.findData("game"))
    editor._source_changed()
    assert editor.source.currentData() == "game", editor.status.text()
    assert editor.voice_panel.current_selection is not None, editor.status.text()
    editor.setFont(dialog.font())
    editor.resize(dialog.size())
    dialog.deleteLater()
    editor.show()
    QApplication.processEvents()
    panel = editor.voice_panel
    assert editor.source.currentData() == "game", editor.status.text()
    assert panel._candidate_entries, editor.status.text()
    alternate_phrase = panel.current_group().alternate_sample_text
    assert alternate_phrase is not None
    actions = {
        "alternate": lambda: panel.voice_reference.setCurrentIndex(1),
        "alternate-phrase": lambda: panel.preview_text.setPlainText(alternate_phrase),
        "original-playing": lambda: _voice_original_playback_story(panel),
        "preview-generating": panel.a_play.click,
    }
    if action := actions.get(state):
        action()
    elif state in {"preview-ready", "preview-playing"}:
        _voice_preview_playback_story(panel, state, resources, write_sample)
    elif state == "content-warning":
        _voice_content_check_story(panel, state, resources, write_sample)
    elif state == "preview-failure":
        panel.a_play.click()
        with patch("vntts.support.record_game_import"):
            panel.preview_runner._task_finished(
                panel.preview_runner._serial,
                None,
                RuntimeError("The speech engine stopped before producing audio."),
            )
    elif state in {"saving", "save-failure", "save-waiting"}:
        _voice_original_playback_story(panel)
        panel.a_use.click()
        if state == "save-waiting":
            editor.cancel_button.click()
        if state == "save-failure":
            panel.decision_runner._task_finished(
                panel.decision_runner._serial,
                None,
                OSError("Application data is read-only."),
            )
            assert not panel.active
            assert panel.retry_save_button.isEnabled()
    return editor


def _voice_original_playback_story(panel: VoiceAuditionPanel) -> None:
    panel.a_original.click()
    panel.original_runner._task_finished(
        panel.original_runner._serial,
        panel.preview_service.reference_audio(
            panel._require_plan(),
            panel.current_group(),
            panel._current_entry()[0].source_id,
        ),
        None,
    )
    assert panel._playing_source == "original"


def _voice_preview_playback_story(
    panel: VoiceAuditionPanel,
    state: str,
    resources: ExitStack,
    write_sample: Callable[[Path], None],
) -> None:
    from vntts.document_identity import sha256_file

    preview = (
        Path(resources.enter_context(temporary_directory(prefix="vntts-ui-catalog-")))
        / "preview.wav"
    )
    write_sample(preview)
    panel.a_play.click()
    panel.preview_runner._task_finished(
        panel.preview_runner._serial,
        SimpleNamespace(path=preview, audio_sha256=sha256_file(preview)),
        None,
    )
    assert panel.a_play.isEnabled()
    assert panel.a_play.accessibleName() == "Stop voice preview"
    if state == "preview-ready":
        panel.a_play.click()


def _voice_content_check_story(
    panel: VoiceAuditionPanel,
    state: str,
    resources: ExitStack,
    write_sample: Callable[[Path], None],
) -> None:
    from vntts.document_identity import sha256_file
    from vntts.pregeneration_audition import VoiceAuditionPreview, preview_identity
    from vntts.preview_content_advisor import PreviewContentCheck
    from vntts.speech_content_advice import assess_speech_content

    plan, group = panel._require_plan(), panel.current_group()
    candidate = panel._current_entry()[0]
    text = panel.preview_text.toPlainText()
    preview_path = (
        Path(resources.enter_context(temporary_directory(prefix="vntts-ui-catalog-")))
        / "preview.wav"
    )
    write_sample(preview_path)
    prepared_preview = VoiceAuditionPreview(
        identity=preview_identity(
            candidate,
            text,
            backend=plan.synthesis_backend,
            model=plan.synthesis_model,
            language=plan.synthesis_language,
            profile=plan.synthesis_profile,
        ),
        group_id=group.group_id,
        candidate_source_id=candidate.source_id,
        text=text,
        backend=plan.synthesis_backend,
        model=plan.synthesis_model,
        generation_profile=plan.synthesis_profile,
        seed=0,
        path=preview_path,
        audio_sha256=sha256_file(preview_path),
        sample_rate=24000,
        duration_seconds=1.0,
        reused=True,
    )
    panel._previews[panel._preview_key(candidate)] = prepared_preview
    panel._content_checks[candidate.source_id] = PreviewContentCheck(
        source_id=candidate.source_id,
        preview_identity=prepared_preview.identity,
        audio_sha256=prepared_preview.audio_sha256,
        text=text,
        seed=0,
        advice=assess_speech_content(text, text + " Wait."),
    )
    panel._show_current_candidate()


def _catalog_support_log(state: str) -> RuntimeSupportLog:
    from vntts.support import RuntimeSupportLog

    recorded_at = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    events = RuntimeSupportLog(clock=lambda: recorded_at)
    if state != "empty":
        for index in range(42 if state == "new-events" else 3):
            recorded_at = recorded_at.replace(minute=index)
            events.add(
                "info" if index % 7 else "warning",
                f"Captured dialogue and checked selected voice ({index})",
            )
    recorded_at = datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc)
    return events


def _catalog_voice_manifest(root: Path, original_reference: Path) -> Path:
    from vntts_artifacts.voice_manifest import write_voice_manifest

    manifest = root / "voice-manifest.json"
    write_voice_manifest(
        manifest,
        {
            "version": 2,
            "voices": [
                {
                    "character": name,
                    "speaker": name.casefold().replace(" ", "-"),
                    "references": [original_reference.name],
                }
                for name in ("Centurion", "A Knight")
            ],
        },
    )
    return manifest


def _catalog_offline_content(root: Path) -> GameContent:
    from vntts_artifacts.story_index import write_story_index_document

    from vntts.document_identity import sha256_file
    from vntts.pregeneration_setup import GameContent, StorySelection

    story_index = root / "story-index.jsonl"
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
    write_story_index_document(
        story_index,
        {"game": "Reverse: 1999", "language": "en"},
        [
            {
                "record_type": "line",
                "line_id": line_id,
                "chapter": selection.selection_id,
                "sequence": index + 1,
                "speaker": selection.speakers[index % len(selection.speakers)],
                "text": f"Catalog dialogue {line_id}.",
                "kind": "dialogue",
                "source_audio_status": "available"
                if index < selection.original_audio_lines
                else "absent",
                "speakable": True,
            }
            for selection in chapters
            for index, line_id in enumerate(selection.line_ids)
        ],
    )
    return GameContent(
        "reverse1999",
        "Reverse: 1999",
        "3.7",
        story_index,
        sha256_file(story_index),
        chapters,
    )


def _catalog_generation_input(job: PregenerationJob) -> PregenerationInput:
    from vntts_artifacts.story_index import write_story_index_document
    from vntts_artifacts.voice_generation_queue import (
        expected_voice_generation_queue_id,
        write_voice_generation_queue,
    )
    from vntts_artifacts.voice_manifest import write_voice_manifest

    from vntts.document_identity import canonical_document_sha256, sha256_file
    from vntts.pregeneration_queue import PregenerationInput
    from vntts.pregeneration_setup import load_verified_story_index_document

    source = Path(job.story_index)
    story = load_verified_story_index_document(source, job.story_index_sha256)
    selected = set(job.selected_line_ids)
    records = [record for record in story.records if record.line_id in selected]
    directory = source.parent / "generation-input"
    directory.mkdir()
    story_path = directory / "story-index.jsonl"
    voice_path = directory / "voice-manifest.json"
    queue_path = directory / "queue.jsonl"
    write_story_index_document(
        story_path, story.metadata, [r.to_record() for r in records]
    )
    write_voice_manifest(
        voice_path,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Narrator",
                    "speaker": "alba",
                    "aliases": [],
                    "references": [],
                }
            ],
        },
    )
    items = []
    for record in records:
        if record.source_audio_status != "absent":
            continue
        text_sha256 = hashlib.sha256(record.text.encode()).hexdigest()
        items.append(
            {
                "record_type": "generation_item",
                "queue_id": expected_voice_generation_queue_id(
                    record.line_id, text_sha256
                ),
                "line_id": record.line_id,
                "text": record.text,
                "text_sha256": text_sha256,
                "speaker": record.speaker,
                "voice_character": "Narrator",
                "action": "generate",
                "prompt_adapters": {},
                "sequence": record.sequence,
            }
        )
    write_voice_generation_queue(
        queue_path, {"game": job.game, "language": story.metadata["language"]}, items
    )
    hashes = {
        "story": sha256_file(story_path),
        "voices": sha256_file(voice_path),
        "queue": sha256_file(queue_path),
    }
    return PregenerationInput(
        identity=canonical_document_sha256(hashes),
        directory=directory,
        story_index=story_path,
        voice_manifest=voice_path,
        queue=queue_path,
        queue_sha256=hashes["queue"],
        queue_items=len(items),
        ready_items=len(items),
        narrator_fallback_roles=tuple(
            sorted({r.speaker for r in records if r.speaker != "Narrator"})
        ),
        story_index_sha256=hashes["story"],
        voice_manifest_sha256=hashes["voices"],
        source_audio_semantic_evidence_sha256=None,
    )


def _source_reference_review(
    state: str, resources: ExitStack
) -> SourceReferenceQualityDialog:
    from PySide6.QtMultimedia import QMediaPlayer

    from tests.source_reference_fixtures import write_quality_session
    from vntts.authoring.source_reference_quality_ui import SourceReferenceQualityDialog

    temporary = resources.enter_context(temporary_directory(prefix="vntts-ui-catalog-"))
    session = write_quality_session(Path(temporary))
    dialog = SourceReferenceQualityDialog(session)

    current = dialog.current
    assert current is not None
    if state == "heard":
        for token in (
            "reference",
            *(sample["queue_id"] for sample in current["generated_samples"]),
        ):
            dialog._playing_token = token
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
    elif state == "complete":
        dialog._load_next(
            dialog.decision_recorder(session, current["variant_id"], "accept")
        )
    elif state == "compact":
        font = dialog.font()
        font.setPointSize(max(font.pointSize() + 3, 16))
        dialog.setFont(font)
        dialog.resize(dialog.minimumSize())
    return dialog


def _catalog_surface_ids(catalog: UICatalog, selected_surface: str | None) -> set[str]:
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

    return allowed_surfaces


def _configure_catalog_application() -> QApplication:
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(["vntts-ui-catalog"])
    if not isinstance(app, QApplication):
        raise RuntimeError("UI catalog requires a QApplication instance")
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#272727"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#151515"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#303030"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Link, QColor("#79b8ff"))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor("#d4a5ff"))
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

    return app


def _support_center(state: str) -> QWidget:
    from PySide6.QtGui import QTextCursor
    from PySide6.QtWidgets import QApplication

    from vntts.support_ui import SupportCenterDialog

    events = _catalog_support_log(state)
    dialog = SupportCenterDialog(events)
    dialog.refresh()
    if state == "new-events":
        dialog.show()
        QApplication.processEvents()
        dialog.events.moveCursor(QTextCursor.MoveOperation.Start)
        dialog.events.verticalScrollBar().setValue(0)
        events.add("warning", "Selected game window is no longer available")
        dialog.refresh()
    elif state in {"choosing", "exporting"}:
        dialog.request_export()
        if state == "exporting":
            dialog.set_export_started()
    elif state == "cancelled":
        dialog.request_export()
        dialog.set_export_result(None, "Support report export cancelled.")
    elif state in {"saved", "saved-launcher-failure", "compact-long-saved"}:
        dialog.set_export_result(
            True,
            ("/Volumes/Support reports/" + "session-directory/" * 32 + "support.zip")
            if state == "compact-long-saved"
            else "/Users/player/Documents/Support reports/October session/vntts-support.zip",
        )
        if state == "saved-launcher-failure":
            dialog.set_launcher_result(
                "settings-folder", False, "Unable to open settings folder"
            )
    elif state in {"failure", "compact-failure", "compact-long-failure"}:
        dialog.set_export_result(
            False,
            ("Unable to read log: " + "/long-folder" * 120 + "/last-log.json")
            if state == "compact-long-failure"
            else "The chosen folder is read-only: /Volumes/Game archive/Support",
        )
    if state.startswith("compact"):
        font = dialog.font()
        font.setPointSize(max(font.pointSize() + 3, 16))
        dialog.setFont(font)
        dialog.resize(620, 420)
    return dialog


def _ocr_review_samples(directory: Path, state: str) -> None:
    from textwrap import fill

    from PIL import Image, ImageDraw, ImageFont

    from vntts.ocr import OCRResult, UncertainFrameRecorder

    long_text = (
        "<b>Stay here</b> & wait for the keeper of the northern library. "
        "The storm has passed, but the roads through the valley remain closed. "
        "We must find every missing traveller before the last train leaves. "
    ) * 3
    samples = [
        ("Mareus", "Hello tiniekeeper."),
        ("N0rthern Keeper", "We cannot leave before dawn."),
        ("Narrator", "The library doors closed behind them."),
    ]
    if state == "long-text" or state.startswith("compact"):
        samples[0] = ("The Keeper of the Northern Library <b>IV</b>", long_text)
    elif state == "shared-source-conflict":
        samples[0] = ("No", "No")
    elif state == "blank-source":
        samples[0] = ("Narrator", "")
    if state in {"complete", "enlarged-screenshot", "enlarged-long"}:
        samples = samples[:1]
    if state == "empty":
        samples = []
    for index, (speaker, dialogue) in enumerate(samples):
        wrapped_dialogue = fill(dialogue or "[No dialogue detected]", width=65)
        height = 130 + len(wrapped_dialogue.splitlines()) * 34
        size = (1200, 1000) if state == "enlarged-long" else (1000, max(210, height))
        image = Image.new("RGB", size, "#282b33")
        draw = ImageDraw.Draw(image)
        draw.text((24, 25), speaker, fill="white", font=ImageFont.load_default(size=34))
        draw.multiline_text(
            (24, 85),
            wrapped_dialogue,
            fill="white",
            font=ImageFont.load_default(size=26),
            spacing=8,
        )
        if state == "enlarged-long":
            draw.text(
                (720, 870),
                "The last words remain visible.",
                fill="white",
                font=ImageFont.load_default(size=26),
            )
        image_path = UncertainFrameRecorder(directory).record(
            image, OCRResult(speaker, dialogue, 42.5 + index, "balanced", 3), 60
        )
        assert image_path is not None
        image_path.with_suffix(".json").rename(
            directory / f"uncertain-{99 - index}.json"
        )


def _ocr_review_failure(dialog: OCRReviewDialog, state: str) -> None:
    from vntts.ocr_corrections import OCRCorrectionStore
    from vntts.versioned_json import StaleDocumentError

    compact = state.startswith("compact-")
    state = state.removeprefix("compact-").removesuffix("-end")
    detail = (
        "Application data is read-only: /Volumes/Long game recordings/Chapter 7/ " * 24
        + "END OF ERROR"
        if compact
        else "Application data is read-only"
    )
    if state == "save-failure":
        dialog.correction_store.upsert_entries = Mock(  # type: ignore[method-assign]
            side_effect=OSError(detail)
        )
    elif state == "partial-save-failure":
        dialog.review_store.mark_resolved = Mock(  # type: ignore[method-assign]
            side_effect=OSError(detail)
        )
    elif state == "stale-save" and compact:
        dialog.correction_store.upsert_entries = Mock(  # type: ignore[method-assign]
            side_effect=StaleDocumentError(
                "The corrections file changed elsewhere: " + detail
            )
        )
    elif state == "stale-save":
        external = OCRCorrectionStore.load(dialog.correction_store.path)
        external.upsert_entries({"Vertln": "Vertin"})


def _ocr_review_write_state(
    dialog: OCRReviewDialog, state: str, finish_write: Callable[[], None]
) -> None:
    from PySide6.QtWidgets import QApplication

    _ocr_review_failure(dialog, state)
    state = state.removeprefix("compact-").removesuffix("-end")
    sample = dialog.current_sample()
    speaker_draft = dialog.corrected_character.text()
    dialog.save_correction()
    assert dialog._write_active
    assert not dialog.sample_list.isEnabled()
    assert not dialog.scope.isEnabled()
    assert not dialog.save_button.isEnabled()
    if state == "close-pending":
        dialog.close()
        assert dialog._close_pending
    if state in {"saving", "close-pending"}:
        return
    finish_write()
    QApplication.processEvents()
    assert not dialog._write_active
    assert dialog.sample_list.isEnabled()
    if state in {"save-failure", "partial-save-failure", "stale-save"}:
        assert dialog.current_sample() == sample
        assert dialog.corrected_character.text() == speaker_draft
        expected_status = (
            "changed elsewhere" if state == "stale-save" else "Could not finish review"
        )
        assert expected_status in dialog.status.toPlainText()
    elif state == "next-sample":
        assert dialog.current_sample() != sample
        assert len(dialog.samples) == 2
    elif state == "complete":
        assert not dialog.samples


def _ocr_review_edit_state(dialog: OCRReviewDialog, state: str) -> None:
    from PySide6.QtCore import QTimer

    if state.startswith("compact"):
        font = dialog.font()
        font.setPointSize(22)
        dialog.setFont(font)
        dialog.resize(730, 560)
    if state == "compact-scrolled":
        QTimer.singleShot(
            0, lambda: dialog.details_scroll.ensureWidgetVisible(dialog.scope)
        )
    if state not in {"pending", "long-text", "empty", "unavailable-image"}:
        dialog.corrected_character.setText("Marcus")
        replacement = "Hello timekeeper."
        if state.startswith("compact"):
            dialog.corrected_character.setText("The Keeper of the Northern Library IV")
            replacement = dialog.corrected_text.toPlainText().replace(
                "Stay here", "Stay with us"
            )
        dialog.corrected_text.setPlainText(replacement)
    if state in {"all-games", "draft-switched"}:
        dialog.scope.setCurrentIndex(0)
    if state == "draft-switched":
        dialog.sample_list.setCurrentRow(1)
        dialog.corrected_character.setText("Northern Keeper")
        dialog.sample_list.setCurrentRow(0)
        assert dialog.corrected_character.text() == "Marcus"
        assert dialog.scope.currentData() is None
    elif state == "shared-source-conflict":
        dialog.corrected_character.setText("Narrator")
        dialog.corrected_text.setPlainText("Yes")
        assert not dialog.save_button.isEnabled()
    elif state == "blank-source":
        dialog.corrected_character.setText("Narrator")
        assert not dialog.save_button.isEnabled()
    elif state == "unavailable-image":
        sample = dialog.current_sample()
        assert sample is not None
        sample.image_path.write_bytes(b"This is a damaged screenshot")
        dialog.show_sample(dialog.sample_list.currentRow())
        assert not dialog.zoom_button.isEnabled()


def _ocr_review_story(
    resources: ExitStack,
    state: str,
    thread_pool: QThreadPool,
    finish_write: Callable[[], None],
) -> QWidget:
    from vntts.ocr_corrections import OCRCorrectionStore
    from vntts.ocr_review_ui import OCRReviewDialog

    directory = Path(resources.enter_context(temporary_directory()))
    _ocr_review_samples(directory, state)
    dialog = OCRReviewDialog(
        directory,
        OCRCorrectionStore(directory / "corrections.json"),
        "game",
        "Reverse: 1999",
        thread_pool=thread_pool,
    )
    if state in {"enlarged-screenshot", "enlarged-long", "resolve-confirm"}:
        popup = (
            dialog._dismissal_dialog()
            if state == "resolve-confirm"
            else dialog._screenshot_dialog()
        )
        assert popup is not None
        resources.enter_context(
            cleanup_on_exit(
                lambda: _close_catalog_widget(dialog),
                description="Catalog popup owner cleanup",
            )
        )
        return popup
    _ocr_review_edit_state(dialog, state)
    write_state = state.removeprefix("compact-").removesuffix("-end")
    if write_state in {
        "saving",
        "save-failure",
        "partial-save-failure",
        "stale-save",
        "close-pending",
        "next-sample",
        "complete",
    }:
        _ocr_review_write_state(dialog, state, finish_write)
    return dialog


def _prepare_ocr_review_screenshot(
    widget: QWidget, story_id: str, app: QApplication
) -> None:
    from vntts.ocr_review_ui import OCRReviewDialog

    if not story_id.startswith("ocr-review."):
        return
    for _ in range(3):
        app.processEvents()
    if not story_id.endswith("-end"):
        return
    assert isinstance(widget, OCRReviewDialog)
    for _ in range(3):
        app.processEvents()
        for bar in (
            widget.details_scroll.verticalScrollBar(),
            widget.status.verticalScrollBar(),
        ):
            bar.setValue(bar.maximum())
        app.processEvents()


def _ocr_corrections_story(resources: ExitStack, state: str) -> QWidget:
    from vntts.ocr_corrections import OCRCorrectionStore
    from vntts.ocr_corrections_ui import OCRCorrectionsDialog
    from vntts.versioned_json import StaleDocumentError

    temporary = resources.enter_context(temporary_directory())
    directory = Path(temporary)
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

    if state.startswith("compact"):
        font = dialog.font()
        font.setPointSize(max(font.pointSize() + 3, 16))
        dialog.setFont(font)
        dialog.resize(560, 440)
    if state == "compact-validation":
        for index in range(30):
            dialog._append_row(dialog.profile_table, f"Unclear {index}", "")
        dialog.save()
    elif state == "validation":
        dialog._append_row(dialog.global_table, "Unclear", "")
        dialog._append_row(dialog.profile_table, "Mareus", "Different")
        dialog.save()
    elif state == "saving":
        dialog._save_active = True
        dialog.tabs.setEnabled(False)
        dialog.buttons.setEnabled(False)
        dialog.status.setPlainText("Saving OCR corrections in the background...")
    elif state in {"stale-save", "compact-failure", "save-failure"}:
        errors = {
            "stale-save": StaleDocumentError("OCR corrections changed on disk"),
            "compact-failure": OSError("Application data is read-only. " * 12),
            "save-failure": OSError("Application data is read-only"),
        }
        dialog._append_row(dialog.profile_table, "Vertln", "Vertin")
        dialog._save_finished(None, errors[state])
    elif state == "unsaved-close":
        dialog._append_row(dialog.profile_table, "Vertln", "Vertin")
        confirmation = dialog._discard_dialog()

        resources.enter_context(
            cleanup_on_exit(
                lambda: _close_catalog_widget(dialog),
                description="Catalog popup owner cleanup",
            )
        )
        return confirmation
    return dialog


def _macos_permissions_story(state: str) -> QWidget:
    from vntts.macos_ui import MacOSPermissionsDialog, PermissionStatus

    compact = state.startswith("compact")
    state = state.removeprefix("compact-")
    statuses: dict[str, PermissionStatus] = {
        "granted": {"screen_capture": True, "accessibility": True},
        "unavailable": {"screen_capture": None, "accessibility": None},
        "capture-ready": {"screen_capture": True, "accessibility": False},
        "accessibility-unknown": {"screen_capture": True, "accessibility": None},
    }
    status = statuses.get(state, {"screen_capture": False, "accessibility": False})
    error = RuntimeError("The native permission service is unavailable. " * 12)
    dialog = MacOSPermissionsDialog(
        status_provider=Mock(
            return_value=status,
            side_effect=error if state == "status-failure" else None,
        ),
        screen_request=Mock(side_effect=error if state == "request-failure" else None),
        accessibility_request=Mock(),
        url_opener=Mock(
            return_value=True,
            side_effect=error if state == "settings-failure" else None,
        ),
    )
    if compact:
        font = dialog.font()
        font.setPointSize(max(font.pointSize() + 4, 16))
        dialog.setFont(font)
        dialog.resize(560, 320)
    if state == "requesting":
        dialog._set_request_active(True)
        dialog.operation_status.setPlainText(
            "Requesting Screen Recording permission..."
        )
    elif state == "request-failure":
        dialog.request_screen()
    elif state in {"settings-failure", "settings-opened"}:
        dialog.open_settings("accessibility")
    return dialog


def _voice_import_story(state: str) -> QWidget:
    from vntts.asset_ui import VoiceImportDialog, build_voice_replacement_prompt

    dialog: QWidget
    compact = state.startswith("compact-") or state == "compact"
    state = state.removeprefix("compact-")
    if state == "replacement":
        dialog = build_voice_replacement_prompt(
            "Believer IV, Guardian of the Long Forgotten Eastern Sanctuary"
            if compact
            else "Believer IV"
        )
    else:
        dialog = VoiceImportDialog()
        dialog.resize(580, 220)
        if state != "empty":
            dialog.character.setText(
                "???" if state == "invalid-name" else "Believer IV"
            )
            dialog.aliases.setText(
                "Believer, !!!" if state == "invalid-alias" else "Believer, Faithful"
            )
            references = {
                "long-values": [
                    "/Users/Player/voices/Chapter_7_Believer_IV_They_at_least_paid_with_their_lives.wav",
                    "/Users/Player/voices/Chapter_7_Believer_IV_What_about_you.wav",
                ],
                "duplicate-filenames": [
                    "/Users/Player/Chapter_7/line.wav",
                    "/Users/Player/Chapter_8/line.wav",
                ],
            }.get(
                state,
                [
                    "/Users/Player/voices/Believer_01.wav",
                    "/Users/Player/voices/Believer_02.wav",
                ],
            )
            dialog.set_references(references)
        if state in {"overflow", "overflow-end"}:
            dialog.character.setText(
                "Believer IV, Guardian of the Long Forgotten Eastern Sanctuary"
            )
            dialog.set_references(
                [
                    f"/Users/Player/voices/Chapter_7_Believer_IV_Long_dialogue_recording_{index:02d}.wav"
                    for index in range(20)
                ]
            )
            dialog.character.setCursorPosition(0)
            if state == "overflow-end":
                from PySide6.QtCore import QTimer

                scrollbar = dialog.reference_files.verticalScrollBar()
                QTimer.singleShot(0, lambda: scrollbar.setValue(scrollbar.maximum()))
                dialog.character.setCursorPosition(len(dialog.character.text()))
    if compact:
        dialog.resize(520, 250)
        font = dialog.font()
        font.setPointSize(font.pointSize() + 4)
        dialog.setFont(font)
    return dialog


def _compact_controller_story(state: str) -> QWidget:
    from PySide6.QtCore import QTimer

    from vntts.controller import LiveSequenceStatus
    from vntts.dashboard_ui import CompactController, RuntimeControlState

    scaled = state.startswith("scaled-")
    at_end = state.endswith("-end")
    state = state.removeprefix("scaled-").replace("warning-end", "warning")
    speaker = (
        "Guardian of the Long Forgotten Eastern Sanctuary"
        if scaled
        else "<b>Unknown visitor</b>"
        if state == "warning"
        else "Marcus"
    )
    reason = "Speech model failed to load. Open Full controls, then Check readiness."
    states = {
        "starting": RuntimeControlState(),
        "starting-reading": RuntimeControlState(
            ready=True, starting=True, transition="starting"
        ),
        "reading": RuntimeControlState(ready=True, live=True),
        "paused": RuntimeControlState(ready=True, live=True, paused=True),
        "speaking": RuntimeControlState(ready=True, speaking=True),
        "stopping": RuntimeControlState(ready=True, live=True, transition="stopping"),
        "unavailable": RuntimeControlState(unavailable_reason=reason),
        "long-error": RuntimeControlState(unavailable_reason=reason),
        "long-error-end": RuntimeControlState(unavailable_reason=reason),
        "sequence-recovery": RuntimeControlState(ready=True, live=True, paused=True),
        "sequence-choice": RuntimeControlState(ready=True, live=True, paused=True),
        "warning": RuntimeControlState(ready=True, live=True),
    }
    dialog = CompactController(platform="win32")
    dialog.set_runtime_controls(states.get(state, RuntimeControlState(ready=True)))
    messages = {
        "starting": "Loading the speech model...",
        "ready": "Ready to read the game dialogue.",
        "reading": "Following dialogue in the game.",
        "paused": "Waiting for the game window to regain focus.",
        "speaking": "Speaking the visible dialogue once.",
        "starting-reading": "Loading the selected story...",
        "stopping": "Stopping reading and speech...",
        "unavailable": reason,
        "sequence-recovery": "Paused at an unmatched story line.",
        "sequence-choice": "Paused at an unmatched story line.",
        "warning": f"Narrator used for {speaker}.",
        "long-error": "Unable to open recording: "
        + "/very-long-audio-directory" * 30
        + "/missing.wav",
        "long-error-end": "Unable to open recording: "
        + "/very-long-audio-directory" * 30
        + "/missing.wav",
    }
    dialog.set_status(messages[state])
    if state == "warning":
        dialog.set_warning(messages[state])
    if state in {
        "reading",
        "paused",
        "speaking",
        "warning",
        "sequence-recovery",
        "sequence-choice",
    }:
        dialog.set_dialogue(speaker, "The current dialogue.")
    if state in {"sequence-recovery", "sequence-choice"}:
        dialog.set_sequence_status(
            LiveSequenceStatus(
                "audio-manual",
                "desynchronized",
                recovery_required=True,
                expected_candidate_count=2 if state == "sequence-choice" else 0,
            )
        )
    if scaled:
        font = dialog.font()
        font.setPointSize(22)
        dialog.setFont(font)
    if at_end:
        bar = dialog.status.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
        speaker_bar = dialog.speaker.verticalScrollBar()
        QTimer.singleShot(0, lambda: speaker_bar.setValue(speaker_bar.maximum()))
    return dialog


def _onboarding_diagnostics_story(
    wizard: OnboardingWizard, state: str, long_error: str
) -> None:
    from PySide6.QtCore import QRunnable, QThreadPool
    from PySide6.QtWidgets import QApplication

    from vntts.onboarding import DiagnosticResult

    class CapturedPool(QThreadPool):
        def __init__(self) -> None:
            super().__init__()
            self.tasks: list[QRunnable] = []

        def start(
            self, runnable: QRunnable | Callable[[], object], priority: int | None = 0
        ) -> None:
            assert isinstance(runnable, QRunnable)
            self.tasks.append(runnable)

    diagnostics = Mock()
    diagnostics.run.return_value = {
        "diagnostics-complete": (
            DiagnosticResult("Capture, OCR and audio", "ok", "Ready"),
        ),
        "diagnostics-warning": (
            DiagnosticResult(
                "Speech model",
                "warning",
                "Not cached yet; it will be downloaded before the final test",
            ),
        ),
        "diagnostics-multiple": (
            DiagnosticResult("Capture source", "ok", "Reverse: 1999"),
            DiagnosticResult(
                "Audio output", "error", "No output device", "external-audio"
            ),
            DiagnosticResult(
                "Tesseract OCR",
                "error",
                "Tesseract executable was not found.",
                "external-ocr",
            ),
            DiagnosticResult(
                "Voice manifest",
                "error",
                "Voice manifest not found: /voices/character-voices.json",
                "voices",
            ),
        ),
        "diagnostics-audio-error": (
            DiagnosticResult(
                "Audio output", "error", "No output device", "external-audio"
            ),
        ),
    }.get(
        state.removesuffix("-last"),
        (
            DiagnosticResult("Capture source", "ok", "Reverse: 1999"),
            DiagnosticResult(
                "Tesseract OCR",
                "error",
                "Tesseract executable was not found.",
                "external-ocr",
            ),
        ),
    )
    if state in {"diagnostics-exception", "diagnostics-exception-end"}:
        diagnostics.run.side_effect = RuntimeError(long_error)
    page = wizard.diagnostics_page
    page.diagnostics = diagnostics
    pool = CapturedPool()
    page.runner.thread_pool = pool
    wizard.show_page(wizard.pages.index(page))
    if state == "diagnostics-cancelled":
        page.cancel_checks()
    elif state in {"diagnostics-install", "diagnostics-disk-space"}:
        page._request_moss_installation(
            2_000_000_000,
            4_000_000_000,
            20_000_000_000 if state == "diagnostics-install" else 1_000_000_000,
        )
    elif state != "diagnostics-loading":
        pool.tasks.pop().run()
        QApplication.processEvents()
        if state.endswith("-last"):
            page.results.setCurrentRow(page.results.count() - 1)


def _onboarding_test_story(
    wizard: OnboardingWizard, state: str, long_error: str
) -> None:
    wizard.calibration_page.calibrated = True
    wizard.show_page(len(wizard.pages) - 1)
    if state == "test":
        return
    page = wizard.test_page
    page.run_test()
    page.set_progress(55, "Reading the detected line...")
    if state == "test-cancelling":
        page.request_cancel()
    elif state != "test-running":
        outcomes = {
            "test-success": (True, "Recognized Marcus: The storm has passed."),
            "test-prepared-success": (
                True,
                "Success. Recognized Marcus: The storm has passed.\n"
                "Audio: Prepared recording · no generation",
            ),
            "test-failure": (
                False,
                "No dialogue detected. Keep dialogue visible and try again.",
            ),
            "test-cancelled": (False, "OCR-to-speech test cancelled."),
            "test-long-error": (False, long_error),
            "test-long-error-end": (False, long_error),
        }
        page.set_result(*outcomes[state])


def _onboarding_story(state: str) -> QWidget:
    from vntts.ocr import DialogRegion
    from vntts.onboarding_ui import OnboardingWizard
    from vntts.settings import AppSettings

    first_run = state.startswith("first-run-")
    state = state.removeprefix("first-run-")
    long_error = (
        "Unable to open voice recording: "
        + "/long-audio-directory" * 60
        + "/missing.wav"
    )
    wizard = OnboardingWizard(
        AppSettings(
            game_window_title="Reverse: 1999",
            speech_backend="pocket-tts" if first_run else "moss-tts",
        ),
        diagnostics=Mock(),
        window_loader=lambda: (),
        auto_discover_windows=False,
        reading_setup=not first_run,
    )
    if state.startswith("game-window-error"):
        wizard.configuration_page.game_window.setCurrentText("")
        wizard.configuration_page.update_validation_summary()
    elif state == "screen-region":
        mode = wizard.configuration_page.capture_mode
        mode.setCurrentIndex(mode.findData("screen"))
    elif state.startswith("diagnostics") or state.startswith("compact-error"):
        _onboarding_diagnostics_story(wizard, state, long_error)
    elif state.startswith("calibration"):
        wizard.show_page(wizard.pages.index(wizard.calibration_page))
        if state == "calibration-complete":
            wizard.calibration_page.finish_calibration(DialogRegion(0.1, 0.6, 0.8, 0.3))
    elif state.startswith("test"):
        _onboarding_test_story(wizard, state, long_error)
    if state in {
        "game-window-error-compact",
        "compact-error",
        "diagnostics-exception",
        "diagnostics-exception-end",
        "compact-error-end",
        "test-long-error",
        "test-long-error-end",
    }:
        font = wizard.font()
        font.setPointSize(22)
        wizard.setFont(font)
        wizard.resize(560, 440)
    return wizard


def _asset_manager_voice_import(
    dialog: AssetManagerDialog,
    state: str,
    voice_manager: Mock,
    finish: Callable[[], None],
) -> None:
    voice_manager.import_voice.return_value = Path(
        "/Users/Player/VNTTS/voices/manifest.json"
    )
    if state == "voice-import-failure":
        voice_manager.import_voice.side_effect = OSError(
            "Voice reference checksum failed: voices/Centurion/line-03.wav"
        )
    dialog._voice_import_draft = (
        "Centurion",
        ["/Users/Player/voices/Centurion/line-03.wav"],
        [],
    )
    dialog._start_voice_import(
        voice_manager.import_voice,
        "Centurion",
        ("/Users/Player/voices/Centurion/line-03.wav",),
        aliases=(),
        message="Imported 1 recording(s) for Centurion",
        from_audio=True,
    )
    if state == "voice-import-close-pending":
        from PySide6.QtGui import QCloseEvent

        dialog.closeEvent(QCloseEvent())
    elif state != "voice-importing":
        finish()


def _asset_manager_model_operation(dialog: AssetManagerDialog, state: str) -> None:
    if state == "model-verified":
        dialog.set_operation_running(True, "verify")
        dialog._model_operation_finished(
            Path("/Users/Player/Library/VNTTS/models/xtts_v2"), None
        )
    elif state == "model-verifying":
        dialog.verify_model()
    elif state == "model-download":
        dialog.set_operation_running(True, "download")
        dialog.update_progress(45, "Downloading selected speech model: 45%")
    elif state == "model-cancel-pending":
        dialog.download_model()
        dialog.cancel_download()
    elif state == "model-failure":
        dialog.set_operation_running(True, "download")
        dialog.model_finished(False, "Model download failed: Network unavailable")


def _asset_manager_compact_state(dialog: AssetManagerDialog, state: str) -> None:
    dialog.resize(700, 320)
    font = dialog.font()
    font.setPointSize(font.pointSize() + 4)
    dialog.setFont(font)
    if state in {"compact-voices-long-error", "compact-model-long-error"}:
        long_voice_error = (
            "Voice reference checksum failed for "
            + "/".join(["voices", "The Northern Library and Traveling Company"] * 8)
            + "/chapter-seven-character-reference.wav. Choose another manifest, "
            "then verify every referenced file before saving."
        )
        if state == "compact-voices-long-error":
            dialog._manifest_validation_finished(None, ValueError(long_voice_error))
        else:
            dialog.set_operation_running(True, "verify")
            dialog.model_finished(
                False,
                "Verification failed: Model checksum failed: "
                "tts_models/multilingual/multi-dataset/xtts_v2/"
                + "model-layer/" * 7
                + "model.pth",
            )


def _render_stories(
    catalog: UICatalog, output: Path, selected_surface: str | None
) -> dict[str, str]:
    allowed_surfaces = _catalog_surface_ids(catalog, selected_surface)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PIL import Image, ImageDraw, ImageFont
    from PySide6.QtCore import (
        QCoreApplication,
        QEvent,
        QRunnable,
        QThreadPool,
        QTimer,
    )
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QDialog, QMessageBox
    from r1999extractor.story_voice_candidates import REPORT_SCHEMA, REPORT_VERSION
    from r1999extractor.story_voice_review_ui import StoryVoiceReviewDialog

    from tests.authoring_fixtures import (
        _legacy_bad_fixture,
        create_failed_reference_workspace,
    )
    from tests.listening_fixtures import FakePlayback, write_model_reports
    from tests.missing_voice_reuse_fixtures import (
        create_missing_voice_reuse_review_fixture,
    )
    from tests.terminal_conflict_fixtures import create_terminal_conflict_review
    from vntts.app import (
        build_story_match_recovery_prompt,
        build_unknown_speaker_prompt,
    )
    from vntts.asset_ui import AssetManagerDialog
    from vntts.authoring.failure_reference_audit import publish_failure_reference_audit
    from vntts.authoring.failure_reference_audit_ui import FailureReferenceAuditDialog
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
    from vntts.authoring.terminal_conflict_review import (
        record_terminal_conflict_decision,
    )
    from vntts.authoring.terminal_conflict_review_ui import (
        TerminalConflictReviewDialog,
    )
    from vntts.controller import LiveSequenceStatus
    from vntts.dashboard_ui import (
        ControlDashboard,
        RuntimeControlState,
    )
    from vntts.diagnostics import DiagnosticSnapshot
    from vntts.diagnostics_ui import DiagnosticsDialog
    from vntts.document_identity import sha256_file
    from vntts.game_content_importer import ImporterAvailability
    from vntts.history import DialogueHistory
    from vntts.history_ui import DialogueHistoryDialog
    from vntts.ocr import DialogRegion
    from vntts.onboarding import DiagnosticResult, OnboardingDiagnostics
    from vntts.pregeneration_generation import OfflineGenerationProgress
    from vntts.pregeneration_setup import (
        ContentDiscovery,
        PregenerationJobStore,
    )
    from vntts.pregeneration_ui import OfflineAudioPreparationDialog
    from vntts.pregeneration_voices import VoiceCandidate, VoiceGroup, VoicePlan
    from vntts.profiles import GameProfile, GameProfileStore
    from vntts.profiles_ui import GameProfilesDialog
    from vntts.readiness_ui import ReadinessDialog
    from vntts.settings import AppSettings
    from vntts.voice_library import VoiceLibrary

    app = _configure_catalog_application()

    settings = AppSettings(
        onboarding_completed=True,
        capture_mode="window",
        speech_backend="moss-tts",
        tts_profile="stable",
        game_window_title="Reverse: 1999",
    )
    resources: ExitStack
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

    def dashboard_stories() -> ControlDashboard:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.set_status("Ready. Choose a prepared story or start live reading.")
        dashboard.set_ready(True)
        dashboard.show_stories()
        return dashboard

    def dashboard_reading() -> ControlDashboard:
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
        dashboard.set_diagnostic(
            DiagnosticSnapshot(
                None,
                character="Believer IV",
                confidence=97.0,
                voice="Believer IV voice",
                audio_source="MOSS fresh generation (voice Believer IV)",
            )
        )
        dashboard.speech_runtime.setText("Compute: Apple GPU · model loaded")
        dashboard.moss_runtime_button.setText("Unload OpenMOSS")
        dashboard.moss_runtime_button.setEnabled(False)
        dashboard.show_reading()
        return dashboard

    def dashboard_empty() -> ControlDashboard:
        dashboard = ControlDashboard(settings)
        dashboard.show_reading()
        dashboard.set_ready(False)
        return dashboard

    def dashboard_setup_error() -> ControlDashboard:
        dashboard = dashboard_empty()
        dashboard.set_runtime_controls(
            RuntimeControlState(
                unavailable_reason="Screen capture permission is missing. Select Check readiness.",
                needs_readiness_check=True,
            )
        )
        return dashboard

    def dashboard_live_tts() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.set_configuration(replace(settings, speech_backend="pocket-tts"))
        dashboard.set_diagnostic(
            DiagnosticSnapshot(
                None,
                character="Believer IV",
                voice="Alba",
                audio_source="Live TTS (pocket-tts)",
            )
        )
        return dashboard

    def dashboard_setup() -> ControlDashboard:
        dashboard = dashboard_stories()
        dashboard.setup_more_button.setChecked(True)
        return dashboard

    def dashboard_loading() -> ControlDashboard:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.show_reading()
        dashboard.set_status("Loading the reading engine and voices.")
        dashboard.set_loading(True)
        dashboard.set_auto_advance_configuration(settings, busy=True)
        return dashboard

    def dashboard_waiting() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.set_status("Waiting for dialogue in the selected game window.")
        dashboard.set_runtime_controls(RuntimeControlState(ready=True, live=True))
        return dashboard

    def dashboard_auto_advance_off() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.set_configuration(
            replace(settings, auto_advance_enabled=False, auto_advance_key="right")
        )
        return dashboard

    def dashboard_auto_advance_unavailable() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.set_configuration(replace(settings, capture_mode="screen"))
        return dashboard

    def dashboard_auto_advance_manual() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.set_configuration(
            replace(settings, live_sequence_mode="audio-manual")
        )
        return dashboard

    def dashboard_auto_advance_unavailable_compact() -> ControlDashboard:
        dashboard = dashboard_auto_advance_unavailable()
        dashboard.resize(620, 440)
        font = dashboard.font()
        font.setPointSize(font.pointSize() + 4)
        dashboard.setFont(font)
        return dashboard

    def dashboard_auto_advance_focus() -> ControlDashboard:
        dashboard = dashboard_long_values()
        QTimer.singleShot(0, dashboard.auto_advance_check.setFocus)
        return dashboard

    def dashboard_auto_advance_settings_route() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.resize(620, 440)
        font = dashboard.font()
        font.setPointSize(font.pointSize() + 4)
        dashboard.setFont(font)
        return dashboard

    def dashboard_paused() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.set_status(
            "Speech paused during speaker recovery. Stop reading ends speech now."
        )
        dashboard.set_runtime_controls(
            RuntimeControlState(
                ready=True, live=True, paused=True, queued=True, replayable=True
            )
        )
        return dashboard

    def dashboard_stopped() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.set_status("Reading stopped. The last dialogue remains available.")
        dashboard.set_runtime_controls(RuntimeControlState(ready=True, replayable=True))
        dashboard.set_live(False)
        return dashboard

    def dashboard_technical() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.details_toggle.setChecked(True)
        return dashboard

    def dashboard_story_recovery() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.set_configuration(
            replace(settings, live_sequence_mode="audio-manual")
        )
        dashboard.set_sequence_status(
            LiveSequenceStatus(
                "audio-manual",
                "desynchronized",
                chapter="7",
                story_title="The Fourth Story",
                sequence=12,
                recovery_required=True,
                guidance="Current line does not match the saved story position.",
                expected_candidate_count=1,
            )
        )
        return dashboard

    def dashboard_story_manual() -> ControlDashboard:
        dashboard = dashboard_reading()
        dashboard.set_configuration(
            replace(settings, live_sequence_mode="audio-manual")
        )
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

    def dashboard_long_values() -> ControlDashboard:
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

    def dashboard_long_capture() -> ControlDashboard:
        dashboard = dashboard_long_values()
        dashboard.set_configuration(
            replace(
                settings,
                game_window_title="Reverse: 1999 - A Very Long Selected Game Window Title",
            )
        )
        dashboard.set_speech_identity(settings, narrator="Centurion")
        return dashboard

    def dashboard_long_values_scrolled() -> ControlDashboard:
        dashboard = dashboard_long_capture()
        dashboard.set_diagnostic(
            DiagnosticSnapshot(
                None,
                character="The Very Long Name of the Fourth Believer from the Northern District",
                voice="Believer IV voice from a long user-provided reference recording",
                audio_source="MOSS fresh generation with a long user-provided reference recording",
            )
        )
        QTimer.singleShot(
            0,
            lambda: dashboard.content_scroll.verticalScrollBar().setValue(
                max(0, dashboard.content_scroll.verticalScrollBar().maximum() - 45)
            ),
        )
        return dashboard

    def dashboard_voice_saved() -> ControlDashboard:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.set_status(
            "Voice saved for Narrator: Believer IV. Prepared recordings are unchanged."
        )
        dashboard.set_speech_identity(settings, narrator="Believer IV")
        dashboard.set_ready(True)
        dashboard.show_voices()
        return dashboard

    def settings_story(state: str) -> QWidget:
        return _settings_story(resources, state)

    class IdlePool(QThreadPool):
        def start(
            self, task: QRunnable | Callable[..., object], /, priority: int | None = 0
        ) -> None:
            # Catalog snapshots intentionally retain a pending operation without dispatch.
            pass

    class CapturedPool(QThreadPool):
        def __init__(self) -> None:
            super().__init__()
            self.tasks: list[QRunnable | Callable[..., object]] = []

        def start(
            self, task: QRunnable | Callable[..., object], /, priority: int | None = 0
        ) -> None:
            self.tasks.append(task)

        def finish_next(self) -> None:
            task = self.tasks.pop(0)
            if isinstance(task, QRunnable):
                task.run()
            else:
                task()
            app.processEvents()

    def readiness(state: str) -> ReadinessDialog:
        dialog = ReadinessDialog(
            settings.updated(ocr_language="jpn")
            if state == "non-english-ocr"
            else settings,
            OnboardingDiagnostics(),
            thread_pool=IdlePool(),
        )
        if state == "loading":
            return dialog
        if state == "cancelled":
            dialog.cancel_checks()
            return dialog
        dialog.runner.cancel()
        if state == "probe-failed":
            dialog._checks_finished(
                None, RuntimeError("Audio device discovery could not finish.")
            )
            return dialog
        results = (
            DiagnosticResult("Hotkeys", "ok", "Read and live hotkeys are valid"),
            DiagnosticResult("Capture source", "ok", "Reverse: 1999"),
            DiagnosticResult("macOS permissions", "ok", "Screen recording allowed"),
            DiagnosticResult("Tesseract OCR", "ok", "Version 5.5.0"),
            DiagnosticResult("Audio output", "ok", "Built-in speakers"),
            DiagnosticResult("Speech runtime", "ok", "Installed at /app/runtime"),
            DiagnosticResult(
                "Character voices", "ok", "All voice recordings available"
            ),
        )
        if state not in {"ready", "full-details", "warning-only"}:
            results = (
                *results[:3],
                DiagnosticResult(
                    "Tesseract OCR",
                    "error",
                    "Tesseract executable was not found in the application environment."
                    if not state.startswith("long-error")
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
                results[5],
                results[6],
            )
        if state not in {"ready", "full-details"}:
            results = (
                *results[:-1],
                DiagnosticResult(
                    "Character voices",
                    "warning",
                    "Narrator fallback will be used",
                    "voices",
                ),
            )
        dialog._checks_finished(results, None)
        selected_name = {
            "audio-error": "Audio output",
            "warning-selected": "Character voices",
            "full-details": "Speech runtime",
            "long-error": "Tesseract OCR",
            "long-error-expanded": "Tesseract OCR",
        }.get(state)
        if selected_name is not None:
            dialog.table.selectRow(
                next(
                    row
                    for row, result in enumerate(dialog._results)
                    if result.name == selected_name
                )
            )
        if state in {"full-details", "long-error-expanded"}:
            dialog.details_toggle.setChecked(True)
        if state.startswith("long-error"):
            dialog.resize(620, 440)
            font = dialog.font()
            font.setPointSize(font.pointSize() + 4)
            dialog.setFont(font)
        return dialog

    def macos_permissions(state: str) -> QWidget:
        return _macos_permissions_story(state)

    def asset_manager(state: str) -> AssetManagerDialog:
        model_manager = Mock()
        model_manager.model_path.return_value = Path(
            "/Users/Player/Library/Application Support/VNTTS/models/xtts_v2"
        )
        voice_manager = Mock()
        model_state = state.startswith("model-") or state.startswith("compact-model")
        manifest = (
            None if state == "voices-empty" else "/Users/Player/voices/manifest.json"
        )
        pool = CapturedPool()
        dialog = AssetManagerDialog(
            settings.updated(
                speech_backend="coqui-xtts" if model_state else "moss-tts",
                voice_manifest=manifest,
                xtts_terms_accepted=True,
            ),
            model_manager=model_manager,
            voice_manager=voice_manager,
            thread_pool=pool,
        )
        if state == "voices-validation":
            dialog._set_manifest_validation_pending(True)
            dialog.voice_status.setPlainText(
                "Checksum-validating the selected manifest and voice files..."
            )
        elif state == "voices-error":
            dialog._manifest_validation_finished(
                None,
                ValueError(
                    "Voice reference checksum failed: voices/Centurion/line-03.wav"
                ),
            )
        elif state in {
            "voice-importing",
            "voice-import-failure",
            "voice-import-success",
            "voice-import-close-pending",
        }:
            _asset_manager_voice_import(dialog, state, voice_manager, pool.finish_next)
        elif state in {
            "model-verified",
            "model-verifying",
            "model-download",
            "model-cancel-pending",
            "model-failure",
        }:
            _asset_manager_model_operation(dialog, state)
        elif state in {
            "compact-voices-long-error",
            "compact-model-long-error",
            "compact-voices",
            "compact-model",
        }:
            _asset_manager_compact_state(dialog, state)
        return dialog

    def game_profiles(state: str) -> GameProfilesDialog:
        region = DialogRegion(0.1, 0.6, 0.8, 0.3)
        active = GameProfile.from_settings(
            "Reverse: 1999",
            settings.updated(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                audio_source_policy="live-tts-only",
            ),
            region=region,
            profile_id="active",
        )
        other = GameProfile.from_settings(
            "A much longer second game profile name",
            settings.updated(
                capture_mode="screen",
                game_window_title=None,
                story_index="/catalog/stories/second-game.jsonl",
                voice_manifest="/catalog/voices/second-game.json",
                generated_audio_manifest="/catalog/audio/second-game.json",
                audio_source_policy="prefer-generated",
            ),
            region=region,
            profile_id="other",
        )
        if state == "missing-window":
            other = replace(other, capture_mode="window", game_window_title=None)
        elif state == "long-values":
            other = replace(
                other,
                name="The Northern Library and Traveling Company: extended story profile",
                capture_mode="window",
                game_window_title="The Northern Library and Traveling Company - Chapter Seven: The Storm Has Passed",
                story_index="/catalog/stories/northern-library-traveling-company-extended-chapter-seven-story-index.jsonl",
                voice_manifest="/catalog/voices/northern-library-traveling-company-confirmed-character-voices.json",
                generated_audio_manifest="/catalog/audio/northern-library-traveling-company-prepared-chapter-seven-recordings.json",
            )
        elif state == "pack-backed":
            other = replace(
                other,
                name="Packaged game",
                game_pack="/catalog/packs/packaged-game.json",
            )
        profiles = [] if state == "empty" else [active, other]
        store = GameProfileStore(
            Path("/private/tmp/vntts-ui-catalog-unused-profiles.json"), profiles
        )
        dialog = GameProfilesDialog(
            settings.updated(active_profile_id=None if state == "empty" else "active"),
            store,
        )
        if state in {
            "other-selected",
            "compact",
            "missing-window",
            "long-values",
            "pack-backed",
        }:
            dialog.profiles.setCurrentIndex(dialog.profiles.findData("other"))
        if state in {"compact", "long-values"}:
            dialog.resize(600, 410)
            font = dialog.font()
            font.setPointSize(font.pointSize() + 4)
            dialog.setFont(font)
        return dialog

    def calibration(state: str) -> DialogRegionOverlay | QDialog:
        scene = Image.new("RGB", (800, 450), "#272936")
        painter = ImageDraw.Draw(scene)
        painter.rectangle((75, 275, 730, 420), fill="#141822", outline="#7a8198")
        painter.text((105, 300), "SELONE", fill="#e9e9e9")
        painter.text(
            (105, 345), "I have returned. We can continue our journey.", fill="#e9e9e9"
        )
        if state.startswith("overlay-"):
            return _calibration_overlay_story(state, scene)
        dialog = _calibration_review_story(state, scene, IdlePool())
        if state.startswith("review-long-enlarged"):
            dialog.show_enlarged_capture()
            enlarged = dialog.capture_preview
            assert enlarged is not None
            enlarged.setParent(None)
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
            return enlarged
        return dialog

    def voice_editor(
        role: str,
        *,
        generating: bool = False,
        failed: bool = False,
        compact_long_values: bool = False,
        narrator_fallback: bool = False,
        resume_live: bool = True,
    ) -> ControlDashboard | GameNarratorDialog:
        story = offline_preparation("confirmation")
        assert story._voice_plan is not None
        selected_role = (
            "The Extremely Long Character Name Used to Verify Layout"
            if compact_long_values
            else role
        )
        first = replace(
            story._voice_plan.groups[0],
            character=selected_role,
            routing_role=selected_role,
            speakers=(selected_role,),
        )
        story._voice_plan = replace(story._voice_plan, groups=(first,))
        editor = _voice_audition_story(
            "compact"
            if compact_long_values
            else "preview-generating"
            if generating
            else "preview-failure"
            if failed
            else "reference",
            story,
            resources,
            write_silent_wav,
        )
        if role == "Selone":
            editor.set_recovery_context(role, resume_live=resume_live)
        if narrator_fallback:
            editor.source.setCurrentIndex(editor.source.findData("narrator"))
            editor.resize(940, 620)
            return editor
        dashboard = ControlDashboard(settings)
        dashboard.resize(
            820 if compact_long_values else 1180, 720 if compact_long_values else 900
        )
        dashboard.embed_narrator(editor)
        dashboard.set_status("Choose and preview a voice. Changes apply when saved.")
        return dashboard

    def unknown_speaker_prompt(*, long_name: bool = False) -> QMessageBox:
        speaker = "The Keeper of the Moonlit Observatory" if long_name else "Selone"
        prompt, _choose, _continue, _cancel = build_unknown_speaker_prompt(speaker)
        if long_name:
            font = prompt.font()
            font.setPointSize(font.pointSize() + 3)
            prompt.setFont(font)
        return prompt

    def story_match_recovery(
        *, larger_text: bool = False, can_read_visible_dialogue: bool = True
    ) -> QMessageBox:
        prompt, _read, _stories, _stop = build_story_match_recovery_prompt(
            "The visible dialogue does not match the prepared story at this point.",
            can_read_visible_dialogue=can_read_visible_dialogue,
        )
        if larger_text:
            font = prompt.font()
            font.setPointSize(font.pointSize() + 5)
            prompt.setFont(font)
            prompt.setMaximumWidth(620)
        return prompt

    def offline_preparation(state: str) -> OfflineAudioPreparationDialog:
        temporary = resources.enter_context(
            temporary_directory(prefix="vntts-ui-catalog-")
        )
        root = Path(temporary)
        content = _catalog_offline_content(root)
        importer = Mock()
        importer.availability.return_value = ImporterAvailability(True, "Ready")
        pool = IdlePool()
        discovery = None if state == "loading" else lambda: ContentDiscovery((content,))
        offline_settings = settings.updated(
            speech_backend="pocket-tts",
            offline_speech_backend="pocket-tts",
            tts_model=None,
            tts_profile="default",
            narrator_speaker="alba",
        )
        voice_library = VoiceLibrary(root / "voice-library")
        voice_library.select(
            "Narrator", route="voice", source_id="preset:alba", method="manual"
        )
        audition_service = Mock(
            backend=SimpleNamespace(runtime_status="Apple GPU - model loaded")
        )
        original_reference = root / "original-reference.wav"
        write_silent_wav(original_reference)
        audition_service.reference_audio.return_value = VoiceAuditionReference(
            original_reference, original_reference.read_bytes()
        )
        manifest = _catalog_voice_manifest(root, original_reference)
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

        dialog._job = dialog.job_store.create_or_resume(
            content, ("chapter-7", "chapter-8")
        )
        dialog._story_selection_drafts[content.story_index_sha256] = {
            "chapter-7",
            "chapter-8",
        }
        dialog._populate_stories(content)
        if state.startswith("confirmation"):
            dialog.step.setText("Step 2 of 4 - Choose and confirm voices")
            centurion = VoiceCandidate(
                "character:centurion",
                "Centurion",
                "centurion-v1",
                (sha256_file(original_reference),),
                94,
                "Closest character voice in the selected stories",
                source_excerpts=(
                    "The storm has passed, but the northern road is still closed.",
                ),
                source_line_ids=("chapter-7:10",),
                reference_duration_seconds=12.5,
            )
            knight = VoiceCandidate(
                "character:knight",
                "A Knight",
                "knight-v2",
                (sha256_file(original_reference),),
                88,
                "Second-best character voice match",
                source_excerpts=("We should reach the next village before nightfall.",),
                source_line_ids=("chapter-8:3",),
                reference_duration_seconds=8.7,
            )
            plan = VoicePlan(
                dialog._job.job_id,
                "2026-09-22T00:00:00+00:00",
                content.story_index_sha256,
                str(manifest),
                sha256_file(manifest),
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
            saved = replace(
                plan.groups[0],
                group_id="saved-centurion",
                character="Centurion",
                routing_role="Centurion",
                line_ids=("chapter-8:14",),
                resolution="saved-player-decision",
            )
            plan = replace(plan, groups=(*plan.groups, saved))
            if state in {"confirmation-portraits", "confirmation-mixed-portraits"}:
                portraits = []
                for index, group in enumerate(plan.groups):
                    if state == "confirmation-mixed-portraits" and index > 0:
                        portraits.append(group)
                        continue
                    portrait = root / f"portrait-{index}.png"
                    picture = Image.new("RGB", (64, 64), (68 + 40 * index, 83, 102))
                    draw = ImageDraw.Draw(picture)
                    draw.ellipse((16, 8, 48, 40), fill="#c5b89b")
                    draw.rounded_rectangle((11, 34, 53, 64), 10, fill="#56677b")
                    picture.save(portrait)
                    portraits.append(
                        replace(
                            group,
                            portrait_image=str(portrait),
                            portrait_image_sha256=sha256_file(portrait),
                        )
                    )
                plan = replace(plan, groups=tuple(portraits))
            if state.startswith("confirmation-compact"):
                long_name = "The Extremely Long Character Name Used to Verify Layout"
                long_voice = "The Likewise Very Long Original Game Voice Name"
                groups = (
                    replace(
                        plan.groups[0],
                        character=long_name,
                        routing_role=long_name,
                        source_character=long_voice,
                    ),
                    *plan.groups[1:],
                )
                plan = replace(plan, groups=groups)
                font = dialog.font()
                font.setPointSize(max(font.pointSize() + 3, 16))
                dialog.setFont(font)
                dialog.resize(760, 620)
            dialog._voice_plan = plan
            dialog._show_voice_confirmation(plan)
            dialog.content_scroll.show()
            dialog.continue_button.setFocus()
            return dialog

        dialog._generation_input = _catalog_generation_input(dialog._job)
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
        dialog._stop_generation_progress()
        dialog._render_generation_progress(OfflineGenerationProgress(generated=34))
        dialog._show_final_handoff(
            original=dialog._job.estimate.original_audio_lines,
            approved=34,
            live_fallbacks=0,
            story_lines=42,
            omissions=0,
        )
        dialog.defer_activation("Reading settings changed after preparation started.")
        dialog.continue_button.setFocus()
        return dialog

    def voice_audition(state: str) -> GameNarratorDialog:
        return _voice_audition_story(
            state, offline_preparation("confirmation"), resources, write_silent_wav
        )

    def ocr_review(state: str) -> QWidget:
        pool = CapturedPool()
        return _ocr_review_story(resources, state, pool, pool.finish_next)

    def ocr_corrections(state: str = "profile-rules") -> QWidget:
        return _ocr_corrections_story(resources, state)

    def dialogue_history(state: str) -> DialogueHistoryDialog:
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
            if state in {"long-text", "compact-failure"}:
                history.add(
                    "A very long character name from the distant island",
                    "The narration continues beyond the edge of the list. " * 8,
                )
        dialog = DialogueHistoryDialog(
            history, lambda *_args: None, stop_handler=lambda: None
        )
        dialog._spoken_character = "Believer IV"
        if state == "filtered":
            dialog.search.setText("storm")
        elif state == "no-matches":
            dialog.search.setText("missing line")
        elif state in {"speaking", "speaking-filtered", "stopping"}:
            if state == "speaking-filtered":
                dialog.search.setText("missing line")
            dialog.replay_button.setEnabled(False)
            dialog.stop_button.setEnabled(state != "stopping")
            dialog.status.setPlainText(
                "Stopping speech for Believer IV..."
                if state == "stopping"
                else "Speaking as Believer IV with the current voice..."
            )
        elif state in {"failure", "compact-failure", "compact-long-failure"}:
            dialog._spoken_character = dialog.visible_entries[-1].character
            dialog._replay_finished(
                None,
                RuntimeError(
                    "The selected voice could not be loaded. The voice file is missing "
                    "from the configured library"
                    if state == "compact-failure"
                    else "The speech engine is unavailable"
                ),
            )
        if state in {"compact", "compact-failure"}:
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(600, 420)
        return dialog

    def diagnostics(state: str) -> DiagnosticsDialog:
        dialog = DiagnosticsDialog()
        if state != "empty":
            capture = Image.new("RGB", (760, 190), "#171a20")
            ImageDraw.Draw(capture).text(
                (24, 72),
                "Believer IV: They at least paid with their lives. What about you?",
                fill="white",
                font=ImageFont.load_default(size=25),
            )
            processed_dialogue = Image.new("RGB", (420, 96), "#101216")
            ImageDraw.Draw(processed_dialogue).text(
                (12, 16),
                "THEY AT LEAST PAID WITH THEIR\nLIVES. WHAT ABOUT YOU?",
                fill="#f1f1f1",
                font=ImageFont.load_default(size=20),
            )
            processed_speaker = Image.new("RGB", (280, 64), "#101216")
            ImageDraw.Draw(processed_speaker).text(
                (12, 20),
                "BELIEVER IV",
                fill="#f1f1f1",
                font=ImageFont.load_default(size=20),
            )
            snapshot = DiagnosticSnapshot(
                capture,
                character="Believer IV",
                text="They at least paid with their lives. What about you?",
                confidence=91.2,
                dialogue_confidence=91.2,
                speaker_confidence=87.4,
                speaker_note="Speaker from prepared story; raw OCR: Beliver IV",
                preprocessing_profile="sauvola",
                voice="Believer IV; voice ID: game-believer-iv",
                capture_ms=12.3,
                ocr_ms=45.6,
                synthesis_ms=789.0,
                playback_ms=321.0,
                capture_interval_ms=600.0,
                game_focused=True,
                corrections=("Beliver -> Believer",),
                speech_queue_depth=1,
                max_speech_queue_depth=3,
                last_first_audio_ms=427.0,
                captured_at=datetime(2026, 9, 22, 12, 34, tzinfo=timezone.utc),
                processed_image=processed_dialogue,
                speaker_image=processed_speaker,
            )
            dialog.set_snapshot(snapshot)
            if state == "missing-images":
                dialog.set_snapshot(
                    replace(snapshot, processed_image=None, speaker_image=None)
                )
        if state in {"technical", "missing-images", "compact-technical"}:
            dialog.technical_toggle.setChecked(True)
        if state == "refreshing":
            dialog.request_refresh()
        elif state in {"warning", "long-warning", "compact"}:
            dialog.set_warning(
                (
                    "The capture area no longer matches this game window after its layout changed. "
                    "Open Settings, select the current game window, then choose Change capture area "
                    "in Live diagnostics and select the whole dialogue box, including its name and continue indicator. "
                    "Refresh after saving the capture area."
                    if state == "long-warning"
                    else "Screen Recording permission is missing. Allow VNTTS in macOS System Settings, then reopen the app."
                    if state == "compact"
                    else "The selected game window is unavailable. Restore the game or choose another window."
                ),
                remediation=(
                    "macos-permissions",
                    "Open macOS permissions",
                )
                if state == "compact"
                else ("settings", "Open Settings"),
            )
        elif state == "timeout":
            dialog.request_refresh()
            dialog._refresh_timed_out(dialog.refresh_generation)
        if state in {"compact", "compact-technical"}:
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.warning.setFont(font)
            dialog.resize(620, 460)
        return dialog

    def diagnostics_enlarged(state: str) -> QDialog:
        parent = diagnostics("snapshot")
        enlarged = parent._capture_dialog(
            processed=state == "enlarged-dialogue", speaker=state == "enlarged-speaker"
        )
        assert enlarged is not None
        enlarged.setParent(None)
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(parent, QEvent.Type.DeferredDelete)
        return enlarged

    def authoring_workbench(state: str) -> AuthoringWorkbenchDialog:
        return _authoring_workbench_story(resources, state)

    def cohort_review(state: str) -> CohortReviewBundleDialog:
        return _cohort_review_story(resources, state)

    def legacy_reason_review(state: str) -> LegacyReasonReviewDialog:
        temporary = resources.enter_context(
            temporary_directory(prefix="vntts-ui-catalog-")
        )
        root = Path(temporary)
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

    def character_story_review(state: str) -> StoryVoiceReviewDialog:
        temporary = resources.enter_context(
            temporary_directory(prefix="vntts-ui-catalog-")
        )
        root = Path(temporary)
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

    def missing_voice_review(state: str) -> MissingVoiceReuseReviewDialog:
        temporary = resources.enter_context(
            temporary_directory(prefix="vntts-ui-catalog-")
        )
        root = Path(temporary)
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

    def failed_reference_review(state: str) -> FailureReferenceAuditDialog:
        temporary = resources.enter_context(
            temporary_directory(prefix="vntts-ui-catalog-")
        )
        root = Path(temporary)
        workspace, _queue_id = create_failed_reference_workspace(root)
        audit = root / "audit"
        publish_failure_reference_audit(workspace, audit)
        dialog = FailureReferenceAuditDialog(audit)

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

    def blind_listening(state: str) -> ModelListeningDialog:
        temporary = resources.enter_context(
            temporary_directory(prefix="vntts-ui-catalog-")
        )
        root = Path(temporary)
        session = create_listening_session_from_reports(
            write_model_reports(root, item_count=1), root / "session"
        )
        playback = FakePlayback()
        dialog = ModelListeningDialog(
            session, auto_play=False, playback_factory=lambda: playback
        )

        if state == "heard":
            for side in ("a", "b"):
                dialog.play(side)
                playback.finish()
                dialog.poll_playback()
        elif state == "complete":
            current = dialog.current_trial
            assert current is not None
            record_trial_preference(session, current["trial_id"], "a")
            dialog.load_next_trial()
        elif state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        return dialog

    def terminal_conflict_review(state: str) -> TerminalConflictReviewDialog:
        temporary = resources.enter_context(
            temporary_directory(prefix="vntts-ui-catalog-")
        )
        directory = create_terminal_conflict_review(Path(temporary))
        dialog = TerminalConflictReviewDialog(directory)

        if state == "heard":
            dialog._heard = {
                candidate["candidate_id"] for candidate in dialog._display_candidates
            }
            dialog._update_decision_buttons()
        elif state == "complete":
            current = dialog._current
            assert current is not None
            record_terminal_conflict_decision(
                directory, current["case_id"], "neither_acceptable"
            )
            dialog._load_next()
        elif state == "compact":
            font = dialog.font()
            font.setPointSize(max(font.pointSize() + 3, 16))
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
        return dialog

    renderers: dict[str, Callable[[], QWidget]] = {
        "dashboard.stories-ready": dashboard_stories,
        "dashboard.reading-loading": dashboard_loading,
        "dashboard.reading-active": dashboard_reading,
        "dashboard.reading-empty": dashboard_empty,
        "dashboard.reading-setup-error": dashboard_setup_error,
        "dashboard.reading-live-tts": dashboard_live_tts,
        "dashboard.auto-advance-off": dashboard_auto_advance_off,
        "dashboard.auto-advance-unavailable": dashboard_auto_advance_unavailable,
        "dashboard.auto-advance-manual": dashboard_auto_advance_manual,
        "dashboard.auto-advance-unavailable-compact": dashboard_auto_advance_unavailable_compact,
        "dashboard.auto-advance-focus": dashboard_auto_advance_focus,
        "dashboard.auto-advance-settings-route": dashboard_auto_advance_settings_route,
        "dashboard.reading-waiting": dashboard_waiting,
        "dashboard.reading-paused": dashboard_paused,
        "dashboard.reading-stopped": dashboard_stopped,
        "dashboard.reading-technical": dashboard_technical,
        "dashboard.story-position-recovery": dashboard_story_recovery,
        "dashboard.story-position-manual": dashboard_story_manual,
        "dashboard.reading-long-values": dashboard_long_values,
        "dashboard.reading-long-capture": dashboard_long_capture,
        "dashboard.reading-long-values-scrolled": dashboard_long_values_scrolled,
        "dashboard.setup-expanded": dashboard_setup,
        "compact-controller.sequence-recovery": lambda: _compact_controller_story(
            "sequence-recovery"
        ),
        "compact-controller.starting": lambda: _compact_controller_story("starting"),
        "compact-controller.ready": lambda: _compact_controller_story("ready"),
        "compact-controller.reading": lambda: _compact_controller_story("reading"),
        "compact-controller.paused": lambda: _compact_controller_story("paused"),
        "compact-controller.speaking": lambda: _compact_controller_story("speaking"),
        "compact-controller.starting-reading": lambda: _compact_controller_story(
            "starting-reading"
        ),
        "compact-controller.stopping": lambda: _compact_controller_story("stopping"),
        "compact-controller.unavailable": lambda: _compact_controller_story(
            "unavailable"
        ),
        "compact-controller.warning": lambda: _compact_controller_story("warning"),
        "compact-controller.long-error": lambda: _compact_controller_story(
            "long-error"
        ),
        "compact-controller.long-error-end": lambda: _compact_controller_story(
            "long-error-end"
        ),
        "compact-controller.sequence-choice": lambda: _compact_controller_story(
            "sequence-choice"
        ),
        "compact-controller.scaled-warning": lambda: _compact_controller_story(
            "scaled-warning"
        ),
        "compact-controller.scaled-warning-end": lambda: _compact_controller_story(
            "scaled-warning-end"
        ),
        "compact-controller.scaled-sequence-recovery": lambda: (
            _compact_controller_story("scaled-sequence-recovery")
        ),
        **{
            f"settings.{state}": partial(settings_story, state)
            for state in (
                "speech-and-voices",
                "shortcuts",
                "shortcuts-windows",
                "capture",
                "capture-window",
                "advanced-capture",
                "playback",
                "playback-advanced",
                "application",
                "checkbox-focus",
                "advanced-speech",
                "compact-capture",
                "compact-shortcuts",
                "compact-playback",
                "compact-application",
                "compact-advanced-speech",
                "compact-advanced-speech-scrolled",
                "validation-error",
                "basic-offline-moss",
                "advanced-offline-moss",
                "compact-validation-errors",
                "compact-validation-errors-end",
                "validation-recovered",
                "playback-speed-reset-needed",
                "playback-speed-reset",
            )
        },
        "readiness.loading": lambda: readiness("loading"),
        "readiness.cancelled": lambda: readiness("cancelled"),
        "readiness.probe-failed": lambda: readiness("probe-failed"),
        "readiness.blocked": lambda: readiness("blocked"),
        "readiness.audio-error": lambda: readiness("audio-error"),
        "readiness.warning-selected": lambda: readiness("warning-selected"),
        "readiness.warning-only": lambda: readiness("warning-only"),
        "readiness.non-english-ocr": lambda: readiness("non-english-ocr"),
        "readiness.ready": lambda: readiness("ready"),
        "readiness.full-details": lambda: readiness("full-details"),
        "readiness.long-error": lambda: readiness("long-error"),
        "readiness.long-error-expanded": lambda: readiness("long-error-expanded"),
        "onboarding.game-window": lambda: _onboarding_story("game-window"),
        "onboarding.screen-region": lambda: _onboarding_story("screen-region"),
        "onboarding.game-window-error": lambda: _onboarding_story("game-window-error"),
        "onboarding.game-window-error-compact": lambda: _onboarding_story(
            "game-window-error-compact"
        ),
        "onboarding.diagnostics-loading": lambda: _onboarding_story(
            "diagnostics-loading"
        ),
        "onboarding.diagnostics-error": lambda: _onboarding_story("diagnostics-error"),
        "onboarding.calibration": lambda: _onboarding_story("calibration"),
        "onboarding.calibration-complete": lambda: _onboarding_story(
            "calibration-complete"
        ),
        "onboarding.diagnostics-multiple": lambda: _onboarding_story(
            "diagnostics-multiple"
        ),
        "onboarding.diagnostics-multiple-last": lambda: _onboarding_story(
            "diagnostics-multiple-last"
        ),
        "onboarding.test": lambda: _onboarding_story("test"),
        "onboarding.test-success": lambda: _onboarding_story("test-success"),
        "onboarding.test-prepared-success": lambda: _onboarding_story(
            "test-prepared-success"
        ),
        "onboarding.compact-error": lambda: _onboarding_story("compact-error"),
        "onboarding.compact-error-end": lambda: _onboarding_story("compact-error-end"),
        "onboarding.diagnostics-exception-end": lambda: _onboarding_story(
            "diagnostics-exception-end"
        ),
        "onboarding.diagnostics-audio-error": lambda: _onboarding_story(
            "diagnostics-audio-error"
        ),
        "onboarding.diagnostics-complete": lambda: _onboarding_story(
            "diagnostics-complete"
        ),
        "onboarding.diagnostics-warning": lambda: _onboarding_story(
            "diagnostics-warning"
        ),
        "onboarding.diagnostics-cancelled": lambda: _onboarding_story(
            "diagnostics-cancelled"
        ),
        "onboarding.diagnostics-exception": lambda: _onboarding_story(
            "diagnostics-exception"
        ),
        "onboarding.diagnostics-install": lambda: _onboarding_story(
            "diagnostics-install"
        ),
        "onboarding.diagnostics-disk-space": lambda: _onboarding_story(
            "diagnostics-disk-space"
        ),
        "onboarding.test-running": lambda: _onboarding_story("test-running"),
        "onboarding.test-cancelling": lambda: _onboarding_story("test-cancelling"),
        "onboarding.test-failure": lambda: _onboarding_story("test-failure"),
        "onboarding.test-cancelled": lambda: _onboarding_story("test-cancelled"),
        "onboarding.test-long-error": lambda: _onboarding_story("test-long-error"),
        "onboarding.test-long-error-end": lambda: _onboarding_story(
            "test-long-error-end"
        ),
        "onboarding.first-run-welcome": lambda: _onboarding_story("first-run-welcome"),
        "onboarding.first-run-test-long-error": lambda: _onboarding_story(
            "first-run-test-long-error"
        ),
        "onboarding.first-run-test-long-error-end": lambda: _onboarding_story(
            "first-run-test-long-error-end"
        ),
        "macos-permissions.denied": lambda: macos_permissions("denied"),
        "macos-permissions.granted": lambda: macos_permissions("granted"),
        "macos-permissions.unavailable": lambda: macos_permissions("unavailable"),
        "macos-permissions.capture-ready": lambda: macos_permissions("capture-ready"),
        "macos-permissions.accessibility-unknown": lambda: macos_permissions(
            "accessibility-unknown"
        ),
        "macos-permissions.requesting": lambda: macos_permissions("requesting"),
        "macos-permissions.status-failure": lambda: macos_permissions("status-failure"),
        "macos-permissions.request-failure": lambda: macos_permissions(
            "request-failure"
        ),
        "macos-permissions.settings-failure": lambda: macos_permissions(
            "settings-failure"
        ),
        "macos-permissions.settings-opened": lambda: macos_permissions(
            "settings-opened"
        ),
        "macos-permissions.compact-status-failure": lambda: macos_permissions(
            "compact-status-failure"
        ),
        "macos-permissions.compact-requesting": lambda: macos_permissions(
            "compact-requesting"
        ),
        "macos-permissions.compact-request-failure": lambda: macos_permissions(
            "compact-request-failure"
        ),
        "macos-permissions.compact-settings-failure": lambda: macos_permissions(
            "compact-settings-failure"
        ),
        "macos-permissions.compact-settings-opened": lambda: macos_permissions(
            "compact-settings-opened"
        ),
        "macos-permissions.compact": lambda: macos_permissions("compact"),
        "asset-manager.voices-empty": lambda: asset_manager("voices-empty"),
        "asset-manager.voices-manifest": lambda: asset_manager("voices-manifest"),
        "asset-manager.voices-validation": lambda: asset_manager("voices-validation"),
        "asset-manager.voices-error": lambda: asset_manager("voices-error"),
        "asset-manager.voice-importing": lambda: asset_manager("voice-importing"),
        "asset-manager.voice-import-failure": lambda: asset_manager(
            "voice-import-failure"
        ),
        "asset-manager.voice-import-success": lambda: asset_manager(
            "voice-import-success"
        ),
        "asset-manager.voice-import-close-pending": lambda: asset_manager(
            "voice-import-close-pending"
        ),
        "asset-manager.model-unchecked": lambda: asset_manager("model-unchecked"),
        "asset-manager.model-verified": lambda: asset_manager("model-verified"),
        "asset-manager.model-verifying": lambda: asset_manager("model-verifying"),
        "asset-manager.model-download": lambda: asset_manager("model-download"),
        "asset-manager.model-cancel-pending": lambda: asset_manager(
            "model-cancel-pending"
        ),
        "asset-manager.model-failure": lambda: asset_manager("model-failure"),
        "asset-manager.compact-voices": lambda: asset_manager("compact-voices"),
        "asset-manager.compact-model": lambda: asset_manager("compact-model"),
        "asset-manager.compact-voices-long-error": lambda: asset_manager(
            "compact-voices-long-error"
        ),
        "asset-manager.compact-model-long-error": lambda: asset_manager(
            "compact-model-long-error"
        ),
        "game-profiles.empty": lambda: game_profiles("empty"),
        "game-profiles.active": lambda: game_profiles("active"),
        "game-profiles.other-selected": lambda: game_profiles("other-selected"),
        "game-profiles.compact": lambda: game_profiles("compact"),
        "game-profiles.missing-window": lambda: game_profiles("missing-window"),
        "game-profiles.long-values": lambda: game_profiles("long-values"),
        "game-profiles.pack-backed": lambda: game_profiles("pack-backed"),
        "voice-import.empty": lambda: _voice_import_story("empty"),
        "voice-import.selected": lambda: _voice_import_story("selected"),
        "voice-import.long-values": lambda: _voice_import_story("long-values"),
        "voice-import.compact": lambda: _voice_import_story("compact"),
        "voice-import.compact-overflow": lambda: _voice_import_story(
            "compact-overflow"
        ),
        "voice-import.compact-overflow-end": lambda: _voice_import_story(
            "compact-overflow-end"
        ),
        "voice-import.duplicate-filenames": lambda: _voice_import_story(
            "duplicate-filenames"
        ),
        "voice-import.invalid-name": lambda: _voice_import_story("invalid-name"),
        "voice-import.invalid-alias": lambda: _voice_import_story("invalid-alias"),
        "voice-import.compact-invalid-alias": lambda: _voice_import_story(
            "compact-invalid-alias"
        ),
        "voice-import.replacement": lambda: _voice_import_story("replacement"),
        "voice-import.compact-replacement": lambda: _voice_import_story(
            "compact-replacement"
        ),
        "calibration.overlay-empty": lambda: calibration("overlay-empty"),
        "calibration.overlay-selected": lambda: calibration("overlay-selected"),
        "calibration.overlay-top-selected": lambda: calibration("overlay-top-selected"),
        "calibration.overlay-save-failure": lambda: calibration("overlay-save-failure"),
        "calibration.overlay-keyboard-selection": lambda: calibration(
            "overlay-keyboard-selection"
        ),
        "calibration.overlay-compact-save-error": lambda: calibration(
            "overlay-compact-save-error"
        ),
        "calibration.review-loading": lambda: calibration("review-loading"),
        "calibration.review-recognized": lambda: calibration("review-recognized"),
        "calibration.review-empty": lambda: calibration("review-empty"),
        "calibration.review-unreadable-name": lambda: calibration(
            "review-unreadable-name"
        ),
        "calibration.review-failure-compact": lambda: calibration(
            "review-failure-compact"
        ),
        "calibration.review-failure": lambda: calibration("review-failure"),
        "calibration.review-compact": lambda: calibration("review-compact"),
        "calibration.review-long-compact": lambda: calibration("review-long-compact"),
        "calibration.review-long-compact-scrolled": lambda: calibration(
            "review-long-compact-scrolled"
        ),
        "calibration.review-long-enlarged": lambda: calibration("review-long-enlarged"),
        "calibration.review-long-enlarged-scrolled": lambda: calibration(
            "review-long-enlarged-scrolled"
        ),
        "unknown-speaker-prompt.awaiting-choice": unknown_speaker_prompt,
        "story-match-recovery.awaiting-choice": story_match_recovery,
        "story-match-recovery.sequence-mode": lambda: story_match_recovery(
            can_read_visible_dialogue=False
        ),
        "story-match-recovery.sequence-larger-text": lambda: story_match_recovery(
            can_read_visible_dialogue=False, larger_text=True
        ),
        "story-match-recovery.larger-text": lambda: story_match_recovery(
            larger_text=True
        ),
        "unknown-speaker-prompt.long-name": lambda: unknown_speaker_prompt(
            long_name=True
        ),
        "voice-editor.narrator": lambda: voice_editor("Narrator"),
        "voice-editor.live-recovery": lambda: voice_editor("Selone"),
        "voice-editor.one-shot-assignment": lambda: voice_editor(
            "Selone", resume_live=False
        ),
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
        "offline-preparation.voice-confirmation-portraits": lambda: offline_preparation(
            "confirmation-portraits"
        ),
        "offline-preparation.voice-confirmation-mixed-portraits": lambda: (
            offline_preparation("confirmation-mixed-portraits")
        ),
        "offline-preparation.voice-confirmation-compact": lambda: offline_preparation(
            "confirmation-compact"
        ),
        "offline-preparation.voice-confirmation-compact-scrolled": lambda: (
            offline_preparation("confirmation-compact-scrolled")
        ),
        "offline-preparation.voice-confirmation-compact-last-route": lambda: (
            offline_preparation("confirmation-compact-last-route")
        ),
        "offline-preparation.voice-overview-narrator-only": lambda: (
            _narrator_only_overview_story(offline_preparation("confirmation"))
        ),
        "offline-preparation.voice-overview-no-references": lambda: (
            _voice_overview_without_references(offline_preparation("confirmation"))
        ),
        "offline-preparation.voice-overview-details": lambda: _voice_overview_story(
            offline_preparation("confirmation"), "details"
        ),
        "offline-preparation.voice-overview-many": lambda: _voice_overview_story(
            offline_preparation("confirmation"), "many"
        ),
        "offline-preparation.voice-overview-many-compact": lambda: (
            _voice_overview_story(offline_preparation("confirmation"), "many-compact")
        ),
        "offline-preparation.voice-overview-many-compact-table": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "many-compact-table"
            )
        ),
        "offline-preparation.voice-overview-many-compact-details": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "many-compact-details"
            )
        ),
        "offline-preparation.voice-overview-draft": lambda: _voice_overview_story(
            offline_preparation("confirmation"), "draft"
        ),
        "offline-preparation.voice-overview-missing": lambda: _voice_overview_story(
            offline_preparation("confirmation"), "missing"
        ),
        "offline-preparation.voice-overview-review": lambda: _voice_overview_story(
            offline_preparation("confirmation"), "review"
        ),
        "offline-preparation.voice-overview-empty-filter": lambda: (
            _voice_overview_story(offline_preparation("confirmation"), "empty-filter")
        ),
        "offline-preparation.voice-overview-compact-draft": lambda: (
            _voice_overview_story(offline_preparation("confirmation"), "compact-draft")
        ),
        "offline-preparation.voice-overview-compact-missing": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "compact-missing"
            )
        ),
        "offline-preparation.voice-overview-compact-review": lambda: (
            _voice_overview_story(offline_preparation("confirmation"), "compact-review")
        ),
        "offline-preparation.voice-overview-compact-draft-actions": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "compact-draft-actions"
            )
        ),
        "offline-preparation.voice-overview-compact-missing-actions": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "compact-missing-actions"
            )
        ),
        "offline-preparation.voice-overview-compact-review-actions": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "compact-review-actions"
            )
        ),
        "offline-preparation.voice-overview-compact-long-narrator": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "compact-long-narrator"
            )
        ),
        "offline-preparation.voice-overview-compact-long-narrator-actions": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "compact-long-narrator-actions"
            )
        ),
        "offline-preparation.voice-overview-many-compact-details-end": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "many-compact-details"
            )
        ),
        "offline-preparation.voice-overview-many-compact-more-actions": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "many-compact-details"
            )
        ),
        "offline-preparation.voice-overview-compact-long-narrator-table": lambda: (
            _voice_overview_story(
                offline_preparation("confirmation"), "compact-long-narrator"
            )
        ),
        "offline-preparation.voice-overview-shared-character": lambda: (
            _shared_character_overview_story(
                offline_preparation("confirmation"), "shared-character"
            )
        ),
        "offline-preparation.voice-overview-shared-character-compact": lambda: (
            _shared_character_overview_story(
                offline_preparation("confirmation"), "compact-shared-character"
            )
        ),
        "offline-preparation.voice-overview-shared-character-compact-table": lambda: (
            _shared_character_overview_story(
                offline_preparation("confirmation"), "compact-shared-character"
            )
        ),
        "offline-preparation.voice-overview-shared-character-compact-actions": lambda: (
            _shared_character_overview_story(
                offline_preparation("confirmation"), "compact-shared-character"
            )
        ),
        "offline-preparation.voice-overview-shared-character-details": lambda: (
            _shared_character_overview_story(
                offline_preparation("confirmation"), "shared-character-details"
            )
        ),
        "offline-preparation.partial-ready": lambda: offline_preparation(
            "partial-ready"
        ),
        "offline-preparation.failure": lambda: offline_preparation("failure"),
        "offline-preparation.completed": lambda: offline_preparation("completed"),
        "voice-audition.reference": lambda: voice_audition("reference"),
        "voice-audition.content-warning": lambda: voice_audition("content-warning"),
        "voice-audition.anonymous-source": lambda: voice_audition("anonymous-source"),
        "voice-audition.original-playing": lambda: voice_audition("original-playing"),
        "voice-audition.preview-generating": lambda: voice_audition(
            "preview-generating"
        ),
        "voice-audition.saving": lambda: voice_audition("saving"),
        "voice-audition.save-waiting": lambda: voice_audition("save-waiting"),
        "voice-audition.preview-playing": lambda: voice_audition("preview-playing"),
        "voice-audition.compact": lambda: voice_audition("compact"),
        "voice-audition.compact-preview-playing": lambda: voice_audition(
            "compact-preview-playing"
        ),
        "voice-audition.compact-preview-failure": lambda: voice_audition(
            "compact-preview-failure"
        ),
        "voice-audition.compact-save-waiting": lambda: voice_audition(
            "compact-save-waiting"
        ),
        "voice-audition.compact-save-failure": lambda: voice_audition(
            "compact-save-failure"
        ),
        "voice-audition.compact-preview": lambda: voice_audition("compact-preview"),
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
        "ocr-review.draft-switched": lambda: ocr_review("draft-switched"),
        "ocr-review.long-text": lambda: ocr_review("long-text"),
        "ocr-review.compact-scrolled": lambda: ocr_review("compact-scrolled"),
        "ocr-review.blank-source": lambda: ocr_review("blank-source"),
        "ocr-review.stale-save": lambda: ocr_review("stale-save"),
        "ocr-review.partial-save-failure": lambda: ocr_review("partial-save-failure"),
        "ocr-review.close-pending": lambda: ocr_review("close-pending"),
        "ocr-review.next-sample": lambda: ocr_review("next-sample"),
        "ocr-review.complete": lambda: ocr_review("complete"),
        "ocr-review.unavailable-image": lambda: ocr_review("unavailable-image"),
        "ocr-review.compact-save-failure": lambda: ocr_review("compact-save-failure"),
        "ocr-review.compact-save-failure-end": lambda: ocr_review(
            "compact-save-failure-end"
        ),
        "ocr-review.compact-partial-save-failure": lambda: ocr_review(
            "compact-partial-save-failure"
        ),
        "ocr-review.compact-partial-save-failure-end": lambda: ocr_review(
            "compact-partial-save-failure-end"
        ),
        "ocr-review.compact-stale-save": lambda: ocr_review("compact-stale-save"),
        "ocr-review.compact-stale-save-end": lambda: ocr_review(
            "compact-stale-save-end"
        ),
        "ocr-corrections.profile-rules": ocr_corrections,
        "ocr-corrections.empty": lambda: ocr_corrections("empty"),
        "ocr-corrections.validation": lambda: ocr_corrections("validation"),
        "ocr-corrections.compact-validation": lambda: ocr_corrections(
            "compact-validation"
        ),
        "ocr-corrections.compact-failure": lambda: ocr_corrections("compact-failure"),
        "ocr-corrections.stale-save": lambda: ocr_corrections("stale-save"),
        "ocr-corrections.saving": lambda: ocr_corrections("saving"),
        "ocr-corrections.save-failure": lambda: ocr_corrections("save-failure"),
        "ocr-corrections.unsaved-close": lambda: ocr_corrections("unsaved-close"),
        "ocr-corrections.long-values": lambda: ocr_corrections("long-values"),
        "ocr-corrections.compact": lambda: ocr_corrections("compact"),
        "dialogue-history.empty": lambda: dialogue_history("empty"),
        "dialogue-history.populated": lambda: dialogue_history("populated"),
        "dialogue-history.filtered": lambda: dialogue_history("filtered"),
        "dialogue-history.no-matches": lambda: dialogue_history("no-matches"),
        "dialogue-history.speaking-filtered": lambda: dialogue_history(
            "speaking-filtered"
        ),
        "dialogue-history.stopping": lambda: dialogue_history("stopping"),
        "dialogue-history.speaking": lambda: dialogue_history("speaking"),
        "dialogue-history.failure": lambda: dialogue_history("failure"),
        "dialogue-history.long-text": lambda: dialogue_history("long-text"),
        "dialogue-history.compact": lambda: dialogue_history("compact"),
        "dialogue-history.compact-failure": lambda: dialogue_history("compact-failure"),
        "diagnostics.empty": lambda: diagnostics("empty"),
        "diagnostics.snapshot": lambda: diagnostics("snapshot"),
        "diagnostics.missing-images": lambda: diagnostics("missing-images"),
        "diagnostics.technical": lambda: diagnostics("technical"),
        "diagnostics.refreshing": lambda: diagnostics("refreshing"),
        "diagnostics.warning": lambda: diagnostics("warning"),
        "diagnostics.long-warning": lambda: diagnostics("long-warning"),
        "diagnostics.timeout": lambda: diagnostics("timeout"),
        "diagnostics.compact": lambda: diagnostics("compact"),
        "diagnostics.compact-technical": lambda: diagnostics("compact-technical"),
        "diagnostics.enlarged-dialogue": lambda: diagnostics_enlarged(
            "enlarged-dialogue"
        ),
        "diagnostics.enlarged-speaker": lambda: diagnostics_enlarged(
            "enlarged-speaker"
        ),
        "support-center.empty": lambda: _support_center("empty"),
        "support-center.events": lambda: _support_center("events"),
        "support-center.new-events": lambda: _support_center("new-events"),
        "support-center.exporting": lambda: _support_center("exporting"),
        "support-center.choosing": lambda: _support_center("choosing"),
        "support-center.cancelled": lambda: _support_center("cancelled"),
        "support-center.saved": lambda: _support_center("saved"),
        "support-center.saved-launcher-failure": lambda: _support_center(
            "saved-launcher-failure"
        ),
        "support-center.compact-failure": lambda: _support_center("compact-failure"),
        "support-center.compact-long-saved": lambda: _support_center(
            "compact-long-saved"
        ),
        "support-center.compact-long-failure": lambda: _support_center(
            "compact-long-failure"
        ),
        "support-center.failure": lambda: _support_center("failure"),
        "support-center.compact": lambda: _support_center("compact"),
        "authoring-workbench.ready": lambda: authoring_workbench("ready"),
        **{
            f"cohort-review.{state}": partial(cohort_review, state)
            for state in (
                "pending",
                "second-cohort",
                "heard",
                "bad",
                "preparing",
                "saving",
                "load-failure",
                "compact",
                "compact-table",
                "compact-technical-details",
                "compact-technical-table-end",
                "compact-provenance-end",
                "compact-bad",
                "compact-bad-reasons",
                "compact-save-failure",
                "compact-actions",
                "technical-details",
                "quality-baseline",
                "playback",
                "stopped",
                "compact-load-failure-end",
                "recovered",
                "save-failure",
                "save-recovered",
                "audio-failure",
                "complete",
                "close-pending",
                "observation-failure",
                "observation-recovered",
            )
        },
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
        "source-reference-review.pending": lambda: _source_reference_review(
            "pending", resources
        ),
        "source-reference-review.heard": lambda: _source_reference_review(
            "heard", resources
        ),
        "source-reference-review.complete": lambda: _source_reference_review(
            "complete", resources
        ),
        "source-reference-review.compact": lambda: _source_reference_review(
            "compact", resources
        ),
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
        "authoring-workbench.review-play-focus": lambda: authoring_workbench(
            "review-play-focus"
        ),
        "authoring-workbench.review-long-names": lambda: authoring_workbench(
            "review-long-names"
        ),
        "authoring-workbench.review-long-names-second": lambda: authoring_workbench(
            "review-long-names-second"
        ),
        "authoring-workbench.review-long-names-details": lambda: authoring_workbench(
            "review-long-names-details"
        ),
        "authoring-workbench.review-long-names-second-details": lambda: (
            authoring_workbench("review-long-names-second-details")
        ),
        "authoring-workbench.approved": lambda: authoring_workbench("approved"),
        "authoring-workbench.interrupted": lambda: authoring_workbench("interrupted"),
        "authoring-workbench.failed": lambda: authoring_workbench("failed"),
        "authoring-workbench.running-elsewhere": lambda: authoring_workbench(
            "running-elsewhere"
        ),
        "authoring-workbench.filters-reset": lambda: authoring_workbench(
            "filters-reset"
        ),
        "authoring-workbench.filtered-empty": lambda: authoring_workbench(
            "filtered-empty"
        ),
        "authoring-workbench.compact": lambda: authoring_workbench("compact"),
        "authoring-workbench.compact-actions": lambda: authoring_workbench(
            "compact-actions"
        ),
        "authoring-workbench.load-failure": lambda: authoring_workbench("load-failure"),
        "authoring-workbench.load-failure-22pt": lambda: authoring_workbench(
            "load-failure-22pt"
        ),
        "authoring-workbench.load-failure-22pt-end": lambda: authoring_workbench(
            "load-failure-22pt-end"
        ),
        "authoring-workbench.recovered": lambda: authoring_workbench("recovered"),
        "authoring-workbench.selection-refresh-pending": lambda: authoring_workbench(
            "selection-refresh-pending"
        ),
        "authoring-workbench.selection-refresh-active": lambda: authoring_workbench(
            "selection-refresh-active"
        ),
        "authoring-workbench.stopping": lambda: authoring_workbench("stopping"),
        "authoring-workbench.stopping-22pt": lambda: authoring_workbench(
            "stopping-22pt"
        ),
        "authoring-workbench.stopping-close": lambda: authoring_workbench(
            "stopping-close"
        ),
        "authoring-workbench.stopping-close-22pt": lambda: authoring_workbench(
            "stopping-close-22pt"
        ),
        "authoring-workbench.stopping-close-22pt-details": lambda: authoring_workbench(
            "stopping-close-22pt-details"
        ),
        "authoring-workbench.technical-details": lambda: authoring_workbench(
            "technical-details"
        ),
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
            with ExitStack() as resources:
                widget = renderer()
                with cleanup_on_exit(
                    lambda: _close_catalog_widget(widget),
                    description="Catalog widget cleanup",
                ):
                    widget.show()
                    app.processEvents()
                    _prepare_catalog_screenshot(widget, story_id, app)
                    if story_id.startswith("onboarding.") and story_id.endswith("-end"):
                        from PySide6.QtWidgets import QScrollArea

                        from vntts.onboarding_ui import OnboardingWizard

                        assert isinstance(widget, OnboardingWizard)
                        scroll = widget.stack.currentWidget()
                        assert isinstance(scroll, QScrollArea)
                        scroll.verticalScrollBar().setValue(
                            scroll.verticalScrollBar().maximum()
                        )
                        app.processEvents()
                    image_name = f"{story_id}.png"
                    image_path = screenshots / image_name
                    if not widget.grab().save(str(image_path)):
                        raise RuntimeError(f"could not save {image_path}")
                    captured[story_id] = f"screenshots/{image_name}"
    return captured


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
    captured = _render_stories(catalog, arguments.output, arguments.surface)
    _write_catalog(catalog, arguments.output, captured)
    print(
        f"Rendered {len(captured)} stories across {len(catalog['surfaces'])} mapped surfaces to {arguments.output / 'index.html'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
