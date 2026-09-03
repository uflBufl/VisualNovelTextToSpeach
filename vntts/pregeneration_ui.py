"""Guided player UI for selecting content to prepare for offline speech."""

from threading import Event

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vntts.application_directories import get_local_data_directory
from vntts.async_ui import LatestTaskRunner
from vntts.game_content_importer import (
    GameContentImportCancelled,
    GameContentImportError,
    Reverse1999GameImporter,
)
from vntts.pregeneration_acceptance import OfflineAcceptanceWorker
from vntts.pregeneration_audition_ui import VoiceAuditionPanel, VoiceAuditionUIError
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationProgress,
    OfflineGenerationWorker,
)
from vntts.pregeneration_pack import OfflinePackPublisher
from vntts.pregeneration_queue import (
    PregenerationInputStore,
    PregenerationQueueCancelled,
)
from vntts.pregeneration_recovery import OfflineRecoveryWorker
from vntts.pregeneration_setup import (
    ContentDiscovery,
    PregenerationJobStore,
    PregenerationSetupError,
    discover_game_content,
    estimate_preparation,
    inspect_story_index,
)
from vntts.pregeneration_voices import (
    PregenerationVoiceCancelled,
    VoiceDecisionStore,
    VoicePlanStore,
    resolve_pregeneration_settings,
)
from vntts.voices import find_default_voice_manifest


class OfflineAudioPreparationDialog(QDialog):
    def __init__(
        self,
        settings,
        *,
        discovery=None,
        job_store=None,
        voice_plan_store=None,
        voice_decisions=None,
        audition_service=None,
        preview_player=None,
        input_store=None,
        generator=None,
        recovery=None,
        acceptance=None,
        publisher=None,
        importer=None,
        thread_pool=None,
        parent=None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.job_store = job_store or PregenerationJobStore()
        self._background_discovery = discovery is None
        self.discovery = discovery or (
            lambda: discover_game_content(
                settings,
                extra_paths=self.job_store.source_story_indexes(),
            )
        )
        self.voice_decisions = voice_decisions or VoiceDecisionStore(
            get_local_data_directory() / "pregeneration" / "voice-decisions.json"
        )
        self.voice_plan_store = voice_plan_store or VoicePlanStore(
            self.job_store,
            decisions=self.voice_decisions,
        )
        self.input_store = input_store or PregenerationInputStore(self.job_store)
        self.generator = generator or OfflineGenerationWorker()
        self.recovery = recovery or OfflineRecoveryWorker(self.generator)
        self.acceptance = acceptance or OfflineAcceptanceWorker(self.generator)
        self.publisher = publisher or OfflinePackPublisher(base_pack=settings.game_pack)
        self.importer = importer or Reverse1999GameImporter()
        self.discovery_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.discovery_runner.finished.connect(self._discovery_finished)
        self.import_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.import_runner.finished.connect(self._import_finished)
        self.voice_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.voice_runner.finished.connect(self._voice_plan_finished)
        self.voice_panel = VoiceAuditionPanel(
            self.voice_decisions,
            preview_service=audition_service,
            thread_pool=thread_pool,
            player=preview_player,
            parent=self,
        )
        self.voice_panel.completed.connect(self._voice_auditions_completed)
        self.voice_panel.cancelled.connect(self._voice_auditions_cancelled)
        self.input_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.input_runner.finished.connect(self._generation_input_finished)
        self.generation_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.generation_runner.finished.connect(self._generation_finished)
        self.recovery_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.recovery_runner.finished.connect(self._recovery_finished)
        self.acceptance_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.acceptance_runner.finished.connect(self._acceptance_finished)
        self.publication_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.publication_runner.finished.connect(self._publication_finished)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(500)
        self.progress_timer.timeout.connect(self._poll_generation_progress)
        self.import_cancel_event = Event()
        self.voice_cancel_event = Event()
        self.importing = False
        self.planning_voices = False
        self.auditioning_voices = False
        self.replanning_voice_decisions = False
        self.preparing_inputs = False
        self.generating = False
        self.recovering = False
        self.accepting_audio = False
        self.publishing_pack = False
        self._close_after_voice_cancel = False
        self._content = ()
        self._job = None
        self._voice_plan = None
        self._generation_input = None
        self._generation_result = None
        self._recovery_result = None
        self._acceptance_result = None
        self._pack_result = None
        self.setWindowTitle("Prepare offline audio")
        self.setMinimumSize(700, 500)

        intro = QLabel(
            "Choose the stories you want available offline. VNTTS will reuse "
            "original game voices and ask only about ambiguous character voices."
        )
        intro.setWordWrap(True)

        self.source = QComboBox()
        self.source.setAccessibleName("Detected game content")
        self.source.setAccessibleDescription(
            "Detected local story content available for offline preparation"
        )
        self.source.currentIndexChanged.connect(self._source_changed)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.browse_button = QPushButton("Choose extracted content...")
        self.browse_button.clicked.connect(self.browse)
        self.import_button = QPushButton("Import installed Reverse: 1999")
        self.import_button.clicked.connect(self.import_installed_game)
        self.game_folder_button = QPushButton("Choose game folder...")
        self.game_folder_button.clicked.connect(self.choose_game_folder)
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Game content"))
        source_row.addWidget(self.source, 1)
        source_row.addWidget(self.refresh_button)
        source_row.addWidget(self.browse_button)
        source_row.addWidget(self.import_button)
        source_row.addWidget(self.game_folder_button)

        self.source_status = QLabel()
        self.source_status.setWordWrap(True)
        self.source_status.setAccessibleName("Game content status")
        self.coverage_summary = QLabel("Offline audio coverage is not available yet.")
        self.coverage_summary.setWordWrap(True)
        self.coverage_summary.setAccessibleName("Offline audio coverage by story")
        self.coverage_summary.setStyleSheet("font-weight: 600;")
        self.stories = QListWidget()
        self.stories.setAccessibleName("Stories and chapters")
        self.stories.setAccessibleDescription(
            "Check every story or chapter to prepare for offline speech"
        )
        self.stories.itemChanged.connect(self._selection_changed)

        selection_actions = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self.select_none_button = QPushButton("Select none")
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))
        self.change_voices = QCheckBox("Change saved voice choices")
        self.change_voices.setAccessibleDescription(
            "Review previously saved ambiguous character voices again for this selection"
        )
        selection_actions.addWidget(self.select_all_button)
        selection_actions.addWidget(self.select_none_button)
        selection_actions.addWidget(self.change_voices)
        selection_actions.addStretch()

        self.summary = QLabel("Select game content to continue.")
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Offline preparation estimate")
        self.selection_status = QLabel()
        self.selection_status.setWordWrap(True)
        self.resume_status = QLabel()
        self.resume_status.setWordWrap(True)
        self.resume_status.setAccessibleName("Offline preparation phase detail")

        self.progress_panel = QGroupBox("Preparation progress")
        self.progress_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.progress_phase = QLabel()
        self.progress_phase.setAccessibleName("Offline preparation phase")
        self.progress_phase.setStyleSheet("font-weight: 600;")
        self.progress_bar = QProgressBar()
        self.progress_bar.setAccessibleName("Durably completed generation items")
        self.progress_bar.setTextVisible(True)
        self.progress_counts = QLabel()
        self.progress_counts.setAccessibleName("Offline generation durable counts")
        self.progress_counts.setWordWrap(True)
        self.progress_guarantee = QLabel()
        self.progress_guarantee.setWordWrap(True)
        self.progress_cancel_consequence = QLabel()
        self.progress_cancel_consequence.setAccessibleName(
            "Cancel and resume consequence"
        )
        self.progress_cancel_consequence.setWordWrap(True)
        self.progress_failures = QLabel()
        self.progress_failures.setAccessibleName("Offline generation recovery status")
        self.progress_failures.setWordWrap(True)
        self.progress_coverage = QLabel()
        self.progress_coverage.setAccessibleName("Final offline audio coverage")
        self.progress_coverage.setWordWrap(True)
        progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.addWidget(self.progress_phase)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_counts)
        progress_layout.addWidget(self.resume_status)
        progress_layout.addWidget(self.progress_guarantee)
        progress_layout.addWidget(self.progress_failures)
        progress_layout.addWidget(self.progress_cancel_consequence)
        progress_layout.addWidget(self.progress_coverage)
        self.progress_panel.hide()

        self.discovery_panel = QGroupBox("Loading game content")
        self.discovery_panel.setAccessibleName("Loading game content")
        discovery_message = QLabel(
            "Please wait while VNTTS finds local game content. Story selection "
            "and preparation controls will be available after loading finishes."
        )
        discovery_message.setWordWrap(True)
        discovery_message.setStyleSheet("font-weight: 600;")
        self.discovery_progress = QProgressBar()
        self.discovery_progress.setRange(0, 0)
        self.discovery_progress.setTextVisible(False)
        self.discovery_progress.setAccessibleName("Finding local game content")
        discovery_layout = QVBoxLayout(self.discovery_panel)
        discovery_layout.addWidget(discovery_message)
        discovery_layout.addWidget(self.discovery_progress)
        self.discovery_panel.hide()

        self.buttons = QDialogButtonBox()
        self.cancel_button = self.buttons.addButton(
            "Cancel",
            QDialogButtonBox.ButtonRole.RejectRole,
        )
        self.continue_button = self.buttons.addButton(
            "Continue",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.continue_button.setDefault(True)
        self.continue_button.setAccessibleDescription(
            "Save this selection and continue or resume offline audio preparation"
        )
        self.continue_button.clicked.connect(self._save_selection)
        self.cancel_button.clicked.connect(self._cancel_or_reject)

        self.selection_panel = QWidget()
        selection_layout = QVBoxLayout(self.selection_panel)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        selection_layout.addWidget(intro)
        selection_layout.addLayout(source_row)
        selection_layout.addWidget(self.source_status)
        selection_layout.addWidget(QLabel("Stories to prepare"))
        selection_layout.addWidget(self.stories, 1)
        selection_layout.addLayout(selection_actions)
        selection_layout.addWidget(self.summary)
        selection_layout.addWidget(self.selection_status)

        layout = QVBoxLayout(self)
        layout.addWidget(self.discovery_panel)
        layout.addWidget(self.coverage_summary)
        layout.addWidget(self.selection_panel, 1)
        layout.addWidget(self.voice_panel)
        layout.addWidget(self.progress_panel)
        layout.addWidget(self.buttons)
        availability = self.importer.availability()
        self.import_button.setEnabled(availability.available)
        self.import_button.setToolTip(availability.message)
        self.game_folder_button.setEnabled(availability.available)
        self.game_folder_button.setToolTip(availability.message)
        if self._background_discovery:
            self._set_discovery_loading(True)
            QTimer.singleShot(0, self.refresh)
        else:
            self.refresh()

    def refresh(self):
        if self._background_discovery:
            self._set_discovery_loading(True)
            self.discovery_runner.start(self._discover_content)
            return
        self._apply_discovery(self._discover_content())

    def _discover_content(self):
        try:
            discovery = self.discovery()
        except (OSError, PregenerationSetupError) as error:
            discovery = ContentDiscovery((), (str(error),))
        if not isinstance(discovery, ContentDiscovery):
            raise TypeError("discovery must return ContentDiscovery")
        return discovery

    def _discovery_finished(self, discovery, error):
        if error is not None:
            discovery = ContentDiscovery((), (str(error),))
        self._apply_discovery(discovery)
        self._set_discovery_loading(False)

    def _set_discovery_loading(self, loading):
        self.discovery_panel.setVisible(loading)
        self.coverage_summary.setVisible(not loading)
        self.selection_panel.setVisible(not loading)
        self.selection_panel.setEnabled(not loading)
        self.continue_button.setEnabled(not loading and bool(self.selected_story_ids()))

    def _apply_discovery(self, discovery):
        previous = self.current_content()
        previous_sha = previous.story_index_sha256 if previous else None
        self._content = discovery.content
        self.source.blockSignals(True)
        self.source.clear()
        for content in self._content:
            self.source.addItem(
                _content_label(content),
                content.story_index_sha256,
            )
        if previous_sha:
            index = self.source.findData(previous_sha)
            if index >= 0:
                self.source.setCurrentIndex(index)
        self.source.blockSignals(False)
        if self._content:
            self.source_status.setText(
                f"Found {len(self._content)} local game source(s)."
            )
            self._source_changed(self.source.currentIndex())
        else:
            detail = f" {discovery.errors[0]}" if discovery.errors else ""
            self.source_status.setText(
                "No extracted game content was found. Run the supported game "
                "importer or choose an existing story-index file." + detail
            )
            self._populate_stories(None)

    def browse(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose extracted story content",
            "",
            "VNTTS story indexes (*.jsonl);;All files (*)",
        )
        if not path:
            return
        try:
            content = inspect_story_index(path, provider_id="selected-story-index")
        except PregenerationSetupError as error:
            self.source_status.setText(str(error))
            return
        existing = next(
            (
                index
                for index, value in enumerate(self._content)
                if value.story_index_sha256 == content.story_index_sha256
            ),
            None,
        )
        if existing is None:
            self._content = (*self._content, content)
            self.source.addItem(
                _content_label(content),
                content.story_index_sha256,
            )
            existing = len(self._content) - 1
        self.source.setCurrentIndex(existing)
        self.source_status.setText("Selected extracted game content is ready.")

    def import_installed_game(self):
        self._start_import(None)

    def choose_game_folder(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "Choose the Reverse: 1999 installation folder",
        )
        if path:
            self._start_import(path)

    def _start_import(self, installation_root):
        if self.importing:
            return
        availability = self.importer.availability()
        if not availability.available:
            self.source_status.setText(availability.message)
            return
        self.importing = True
        self.import_cancel_event.clear()
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel import")
        self.cancel_button.setEnabled(True)
        self.source_status.setText(
            "Finding the installed game and importing story content..."
        )
        self.import_runner.start(
            self.importer.import_installed,
            self.import_cancel_event,
            installation_root,
        )

    def current_content(self):
        index = self.source.currentIndex()
        return self._content[index] if 0 <= index < len(self._content) else None

    def selected_story_ids(self):
        return tuple(
            self.stories.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.stories.count())
            if self.stories.item(row).checkState() == Qt.CheckState.Checked
        )

    def job(self):
        return self._job

    def voice_plan(self):
        return self._voice_plan

    def generation_input(self):
        return self._generation_input

    def generation_result(self):
        return self._generation_result

    def recovery_result(self):
        return self._recovery_result

    def acceptance_result(self):
        return self._acceptance_result

    def pack_result(self):
        return self._pack_result

    def _show_phase(self, phase, detail, cancel_consequence):
        self.progress_panel.show()
        self.progress_phase.setText(phase)
        self.resume_status.setText(detail)
        self.progress_cancel_consequence.setText(cancel_consequence)

    def _show_waiting_phase(self, phase, detail, cancel_consequence):
        self._show_phase(phase, detail, cancel_consequence)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.progress_counts.setText("Generation has not started yet.")
        self.progress_guarantee.setText(
            "Your selected stories and completed voice choices are saved for restart."
        )
        self.progress_failures.clear()
        self.progress_coverage.clear()

    def _start_generation_progress(self):
        total = self._generation_input.ready_items
        self._show_phase(
            "Generating offline audio",
            f"Generating {total} offline lines.",
            "Cancel stops generation and closes this window. Finished lines stay "
            "saved; reopen and select Continue to generate only unfinished lines.",
        )
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat(f"0 of {total} durable items")
        self.progress_counts.setText(f"0 of {total} generation items durably finished.")
        self.progress_guarantee.setText(
            "Each finished item is saved on disk; cancellation does not discard it."
        )
        self.progress_failures.clear()
        self.progress_coverage.clear()
        self._poll_generation_progress()
        self.progress_timer.start()

    def _poll_generation_progress(self):
        if self._generation_input is None or not (self.generating or self.recovering):
            return
        inspect = getattr(self.generator, "inspect_progress", None)
        if not callable(inspect):
            return
        try:
            progress = inspect(self._generation_input)
        except Exception:
            return
        if isinstance(progress, OfflineGenerationProgress):
            self._render_generation_progress(progress)

    def _render_generation_progress(self, progress):
        total = self._generation_input.ready_items
        completed = min(progress.completed, total)
        if self.generating:
            durable_phase = {
                "generating": "Generating offline audio",
                "validating": "Checking generated audio",
                "publishing": "Saving generated audio",
            }.get(progress.active_phase)
            if durable_phase is not None:
                self.progress_phase.setText(durable_phase)
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(completed)
        self.progress_bar.setFormat(f"{completed} of {total} durable items")
        self.progress_counts.setText(
            f"{completed} of {total} generation items durably finished: "
            f"{progress.generated} prepared, {progress.failed} failed, "
            f"{progress.other_terminal} routed without prepared audio."
        )
        if self.recovering:
            self.progress_failures.setText(
                f"Automatic recovery is working on {progress.failed} failed "
                f"item{'s' if progress.failed != 1 else ''}."
            )
        elif progress.failed:
            self.progress_failures.setText(
                f"{progress.failed} failure{'s' if progress.failed != 1 else ''} "
                "found so far; safe automatic recovery starts after generation."
            )
        else:
            self.progress_failures.clear()

    def _render_generation_result(self, result):
        if self._generation_input is None or result is None:
            return
        values = tuple(
            value if isinstance(value, int) and not isinstance(value, bool) else 0
            for value in (
                getattr(result, "generated", 0),
                getattr(result, "failed", 0),
                getattr(result, "other_terminal", 0),
            )
        )
        self._render_generation_progress(OfflineGenerationProgress(*values))

    def _show_final_handoff(self, result):
        self.progress_timer.stop()
        self.selection_panel.show()
        self.voice_panel.hide()
        self._render_generation_result(self._generation_result)
        original = self._job.estimate.original_audio_lines
        prepared = getattr(result, "approved", 0)
        live = getattr(result, "live_fallbacks", 0)
        story_lines = getattr(result, "story_lines", 0)
        omissions = getattr(result, "omissions", 0)
        prepared = prepared if isinstance(prepared, int) else 0
        live = live if isinstance(live, int) else 0
        story_lines = story_lines if isinstance(story_lines, int) else 0
        omissions = omissions if isinstance(omissions, int) else 0
        self._show_phase(
            "Offline audio is ready",
            "The validated offline pack is saved and ready to activate.",
            "Close leaves the current audio setup unchanged; the saved pack can be "
            "activated by reopening this preparation later.",
        )
        self.progress_coverage.setText(
            f"Final coverage: {original} original-game-audio lines in this selection; "
            f"the saved pack has {prepared} prepared lines and {live} live "
            f"fallbacks across {story_lines or self._job.estimate.selected_lines} "
            f"story lines"
            + (f", with {omissions} explicit omissions." if omissions else ".")
        )
        self.progress_failures.setText(
            "Automatic recovery finished before this pack was validated."
            if self._recovery_result is not None
            else "No automatic recovery was needed."
        )
        self.continue_button.setText("Use prepared audio")
        self.continue_button.setAccessibleDescription(
            "Close this preparation and activate the saved offline audio pack"
        )
        self.continue_button.setEnabled(True)
        self.continue_button.setDefault(True)
        self.continue_button.setFocus()
        self.cancel_button.setText("Close")
        self.cancel_button.setEnabled(True)

    def _source_changed(self, _index):
        self._populate_stories(self.current_content())

    def _populate_stories(self, content):
        self.stories.blockSignals(True)
        self.stories.clear()
        if content is not None:
            resumed = self.job_store.latest_for_content(content)
            resumed_ids = set(resumed.selected_story_ids) if resumed else set()
            story_statuses = self.job_store.story_statuses(content)
            for selection in content.selections:
                status = story_statuses.get(selection.selection_id)
                speech_status = {
                    "ready": "Ready offline",
                    "in_progress": "Preparation incomplete - Continue to finish",
                }.get(status, f"Needs speech: {selection.generation_lines} lines")
                item = QListWidgetItem(
                    f"{selection.title} - {selection.line_count} lines; {speech_status}"
                )
                item.setData(Qt.ItemDataRole.UserRole, selection.selection_id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if not resumed_ids or selection.selection_id in resumed_ids
                    else Qt.CheckState.Unchecked
                )
                self.stories.addItem(item)
            ready = sum(status == "ready" for status in story_statuses.values())
            in_progress = sum(
                status == "in_progress" for status in story_statuses.values()
            )
            remaining = len(content.selections) - ready - in_progress
            self.coverage_summary.setText(
                f"Offline audio: {ready} ready, {in_progress} incomplete, "
                f"{remaining} not started ({len(content.selections)} stories total)."
            )
            message = (
                "Saved offline audio found. Previous selection restored."
                if ready
                else "Previous selection restored. Continue resumes the same preparation."
                if resumed
                else "Your selection will be saved and can be resumed after restart."
            )
            self.selection_status.setText(message)
            self.resume_status.setText(message)
        else:
            self.coverage_summary.setText(
                "Offline audio coverage is not available yet."
            )
            self.selection_status.clear()
            self.resume_status.clear()
        self.stories.blockSignals(False)
        self._selection_changed()

    def _set_all_checked(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.stories.blockSignals(True)
        for row in range(self.stories.count()):
            self.stories.item(row).setCheckState(state)
        self.stories.blockSignals(False)
        self._selection_changed()

    def _selection_changed(self, _item=None):
        content = self.current_content()
        selected = self.selected_story_ids()
        if content is None:
            self.summary.setText("Choose or import game content to continue.")
            self.continue_button.setEnabled(False)
            return
        if not selected:
            self.summary.setText("Select at least one story or chapter.")
            self.continue_button.setEnabled(False)
            return
        estimate = estimate_preparation(content, selected)
        disk_megabytes = max(1, round(estimate.estimated_disk_bytes / 1_000_000))
        self.summary.setText(
            f"{estimate.selected_lines} dialogue lines selected. "
            f"{estimate.original_audio_lines} already use original game voices; "
            f"up to {estimate.generation_lines} need speech across about "
            f"{estimate.speaker_count} voices. Rough estimate: "
            f"{estimate.estimated_generation_minutes} minutes and "
            f"{disk_megabytes} MB."
        )
        self.continue_button.setEnabled(True)

    def _save_selection(self):
        if self._pack_result is not None:
            self.accept()
            return
        content = self.current_content()
        if content is None:
            return
        try:
            self._job = self.job_store.create_or_resume(
                content,
                self.selected_story_ids(),
            )
        except (OSError, PregenerationSetupError) as error:
            self.resume_status.setText(f"Unable to save preparation: {error}")
            return
        self.planning_voices = True
        self.replanning_voice_decisions = False
        self._close_after_voice_cancel = False
        self.voice_cancel_event.clear()
        self._set_import_controls(False)
        self.selection_panel.setVisible(False)
        self.cancel_button.setText("Cancel voice matching")
        self.cancel_button.setEnabled(True)
        self._show_waiting_phase(
            "Matching character voices",
            "Preparing and matching character voices...",
            "Cancel stops voice matching and closes this window. Reopen it to "
            "reuse the saved story selection.",
        )
        self.voice_runner.start(
            self._create_voice_plan,
            self._job,
            self.change_voices.isChecked(),
        )

    def _create_voice_plan(self, job, ignore_decisions=False):
        manifest = None
        if not self.settings.voice_manifest and find_default_voice_manifest() is None:
            try:
                manifest = self.importer.prepare_voice_candidates(
                    job,
                    self.voice_cancel_event,
                )
            except GameContentImportCancelled as error:
                raise PregenerationVoiceCancelled(
                    "Voice candidate preparation was cancelled"
                ) from error
            except GameContentImportError:
                manifest = None
        options = {
            "cancellation": self.voice_cancel_event,
            "ignore_decisions": ignore_decisions,
        }
        if manifest is not None:
            options["manifest_path"] = manifest
        return self.voice_plan_store.create(
            job,
            resolve_pregeneration_settings(self.settings),
            **options,
        )

    def _voice_plan_finished(self, plan, error):
        self.planning_voices = False
        if self._close_after_voice_cancel:
            self.reject()
            return
        if error is not None:
            self.cancel_button.setText("Cancel")
            self.cancel_button.setEnabled(True)
            self._set_import_controls(True)
            self.selection_panel.setVisible(True)
            self.progress_phase.setText("Voice matching paused")
            self.progress_cancel_consequence.setText(
                "Change the selection or choose Continue to retry."
            )
            if isinstance(error, PregenerationVoiceCancelled):
                if self._close_after_voice_cancel:
                    self.reject()
                else:
                    self.resume_status.setText("Voice matching cancelled.")
                return
            self.resume_status.setText(f"Unable to match character voices: {error}")
            return
        self._voice_plan = plan
        audition_count = getattr(plan, "audition_count", 0)
        if isinstance(audition_count, int) and audition_count > 0:
            if self.replanning_voice_decisions:
                self.replanning_voice_decisions = False
                self.cancel_button.setText("Cancel")
                self.cancel_button.setEnabled(True)
                self._set_import_controls(True)
                self.selection_panel.setVisible(True)
                self.resume_status.setText(
                    "Saved voice choices could not be applied. Nothing was generated."
                )
                return
            try:
                self.voice_panel.start(plan)
            except VoiceAuditionUIError as error:
                self.cancel_button.setText("Cancel")
                self.cancel_button.setEnabled(True)
                self._set_import_controls(True)
                self.selection_panel.setVisible(True)
                self.resume_status.setText(
                    f"Unable to choose character voices: {error}"
                )
                return
            self.auditioning_voices = True
            self.cancel_button.setText("Cancel voice selection")
            self.cancel_button.setEnabled(True)
            self._show_phase(
                "Choose character voices",
                f"Listen to {audition_count} ambiguous character voice"
                f"{'s' if audition_count != 1 else ''}.",
                "Cancel stops voice selection and closes this window. Reopen it "
                "to reuse the saved story selection and any completed choices.",
            )
            self.progress_bar.setRange(0, audition_count)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Voice choices remain")
            self.progress_counts.setText(
                f"{audition_count} voice choice"
                f"{'s' if audition_count != 1 else ''} require input before generation."
            )
            return
        self.replanning_voice_decisions = False
        self.voice_panel.shutdown()
        self._start_generation_input(plan)

    def _voice_auditions_completed(self):
        self.auditioning_voices = False
        self.replanning_voice_decisions = True
        self.planning_voices = True
        self.cancel_button.setText("Cancel voice matching")
        self.cancel_button.setEnabled(True)
        self._show_waiting_phase(
            "Applying voice choices",
            "Applying your saved voice choices...",
            "Cancel stops voice matching and closes this window. Reopen it to "
            "reuse the saved choices.",
        )
        self.voice_runner.start(
            self._create_voice_plan,
            self._job,
            False,
        )

    def _voice_auditions_cancelled(self):
        self.auditioning_voices = False
        if self._close_after_voice_cancel:
            self.reject()
            return
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        self.selection_panel.setVisible(True)
        self.selection_status.setText("Voice selection cancelled.")
        self.resume_status.setText("Voice selection cancelled.")
        self.progress_panel.hide()

    def _start_generation_input(self, plan):
        self.preparing_inputs = True
        self.cancel_button.setText("Cancel preparation")
        self._show_waiting_phase(
            "Preparing selected stories",
            "Preparing the selected stories for generation...",
            "Cancel stops preparation and closes this window. Reopen it and choose "
            "Continue to resume from the saved selection and voice choices.",
        )
        self.input_runner.start(
            self.input_store.materialize,
            self._job,
            plan,
            cancellation=self.voice_cancel_event,
        )

    def _generation_input_finished(self, generation_input, error):
        self.preparing_inputs = False
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if self._close_after_voice_cancel:
            self.reject()
            return
        if error is not None:
            self.selection_panel.setVisible(True)
            self.progress_phase.setText("Preparation paused")
            self.progress_cancel_consequence.setText(
                "Choose Continue to retry from the saved selection."
            )
            if isinstance(error, PregenerationQueueCancelled):
                if self._close_after_voice_cancel:
                    self.reject()
                else:
                    self.resume_status.setText("Offline preparation cancelled.")
                return
            self.resume_status.setText(f"Unable to prepare generation: {error}")
            return
        self._generation_input = generation_input
        self.generating = True
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel generation")
        self.cancel_button.setEnabled(True)
        self._start_generation_progress()
        self.generation_runner.start(
            self.generator.generate,
            generation_input,
            self._voice_plan,
            self.voice_cancel_event,
        )

    def _generation_finished(self, result, error):
        if error is not None:
            self._poll_generation_progress()
        self.generating = False
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if self._close_after_voice_cancel:
            self.reject()
            return
        if error is not None:
            self.progress_timer.stop()
            self.selection_panel.setVisible(True)
            self.progress_phase.setText("Generation paused")
            self.progress_cancel_consequence.setText(
                "Finished lines remain saved. Choose Continue to generate only "
                "unfinished lines, or Close to resume later."
            )
            if isinstance(error, OfflineGenerationCancelled):
                self.resume_status.setText(
                    "Generation cancelled. Continue later to resume saved lines."
                )
                return
            self.resume_status.setText(f"Unable to generate offline audio: {error}")
            return
        self._generation_result = result
        self._render_generation_result(result)
        if result.failed < 1:
            self.progress_timer.stop()
            self._start_acceptance(result)
            return
        self.recovering = True
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel automatic recovery")
        self.cancel_button.setEnabled(True)
        self._show_phase(
            "Recovering failed lines",
            f"Trying safe automatic fixes for {result.failed} unfinished lines...",
            "Cancel stops recovery and closes this window. Finished lines remain "
            "saved; reopen and choose Continue to retry unfinished lines.",
        )
        self.progress_failures.setText(
            f"Automatic recovery is working on {result.failed} failed "
            f"item{'s' if result.failed != 1 else ''}."
        )
        self.recovery_runner.start(
            self.recovery.recover,
            self._generation_input,
            self._voice_plan,
            result,
            self.voice_cancel_event,
        )

    def _recovery_finished(self, result, error):
        if error is not None:
            self._poll_generation_progress()
        self.recovering = False
        self.progress_timer.stop()
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if self._close_after_voice_cancel:
            self.reject()
            return
        if error is not None:
            self.selection_panel.setVisible(True)
            self.progress_phase.setText("Automatic recovery paused")
            self.progress_cancel_consequence.setText(
                "Finished lines remain saved. Choose Continue to retry unfinished "
                "lines, or Close to resume later."
            )
            if isinstance(error, OfflineGenerationCancelled):
                self.resume_status.setText(
                    "Automatic recovery cancelled. Continue later to resume saved lines."
                )
                return
            self.resume_status.setText(f"Unable to recover offline audio: {error}")
            return
        self._recovery_result = result
        self._generation_result = result.generation
        self._render_generation_result(result.generation)
        self.progress_failures.setText(
            f"Recovery repaired {result.recovered} item"
            f"{'s' if result.recovered != 1 else ''}; "
            f"{result.live_fallbacks} will use live fallback and "
            f"{result.remaining_failed} remain failed."
        )
        if result.remaining_failed:
            ready = max(0, self._generation_input.ready_items - result.remaining_failed)
            self.progress_bar.setValue(ready)
            self.progress_bar.setFormat(
                f"{ready} of {self._generation_input.ready_items} ready"
            )
            self.selection_panel.setVisible(True)
            self.progress_phase.setText("Automatic recovery paused")
            self.progress_cancel_consequence.setText(
                "Prepared lines remain saved. Choose Continue to retry only the "
                "unfinished lines, or Close to resume later."
            )
            self.resume_status.setText(
                f"Offline audio is not complete: {result.remaining_failed} line"
                f"{'s' if result.remaining_failed != 1 else ''} still need a safe "
                "automatic repair."
            )
            return
        self._start_acceptance(result.generation)

    def _start_acceptance(self, generation_result):
        self.accepting_audio = True
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel final checks")
        self.cancel_button.setEnabled(True)
        self._show_phase(
            "Checking prepared audio",
            "Finishing technical checks and saving prepared audio...",
            "Cancel stops final checks and closes this window. Prepared lines stay "
            "saved; reopen and choose Continue to rerun the checks.",
        )
        self.acceptance_runner.start(
            self.acceptance.accept,
            self._generation_input,
            generation_result,
            self.voice_cancel_event,
        )

    def _acceptance_finished(self, result, error):
        self.accepting_audio = False
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if self._close_after_voice_cancel:
            self.reject()
            return
        if error is not None:
            self.selection_panel.setVisible(True)
            self.progress_phase.setText("Final checks paused")
            if isinstance(error, OfflineGenerationCancelled):
                self.resume_status.setText(
                    "Final checks cancelled. Continue later to resume saved lines."
                )
                return
            self.resume_status.setText(f"Unable to finish offline audio: {error}")
            return
        self._acceptance_result = result
        self._generation_result = result.generation
        self._start_publication(result.generation)

    def _start_publication(self, generation_result):
        self.publishing_pack = True
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel final save")
        self.cancel_button.setEnabled(True)
        self._show_phase(
            "Saving offline pack",
            "Creating and checking your offline game pack...",
            "Cancel stops the final save and closes this window. Prepared lines stay "
            "saved; reopen and choose Continue to retry the pack save.",
        )
        self.publication_runner.start(
            self.publisher.publish,
            self._job,
            self._generation_input,
            generation_result,
            self.voice_cancel_event,
        )

    def _publication_finished(self, result, error):
        self.publishing_pack = False
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if self._close_after_voice_cancel:
            self.reject()
            return
        if error is not None:
            self.selection_panel.setVisible(True)
            self.progress_phase.setText("Final save paused")
            if isinstance(error, OfflineGenerationCancelled):
                self.resume_status.setText(
                    "Final save cancelled. Continue later to reuse prepared lines."
                )
                return
            self.resume_status.setText(f"Unable to create offline game pack: {error}")
            return
        self._pack_result = result
        status_error = None
        try:
            self._job = self.job_store.mark_prepared(self._job)
            self._populate_stories(self.current_content())
        except (OSError, PregenerationSetupError) as error:
            status_error = error
        self._show_final_handoff(result)
        if status_error is not None:
            self.resume_status.setText(
                "Offline audio was saved, but its story status could not be updated: "
                f"{status_error}"
            )

    def _import_finished(self, content, error):
        self.importing = False
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if error is not None:
            self.source_status.setText(
                "Game import cancelled."
                if isinstance(error, GameContentImportCancelled)
                else f"Unable to import the installed game: {error}"
            )
            return
        existing = next(
            (
                index
                for index, value in enumerate(self._content)
                if value.story_index_sha256 == content.story_index_sha256
            ),
            None,
        )
        if existing is None:
            self._content = (*self._content, content)
            self.source.addItem(
                _content_label(content),
                content.story_index_sha256,
            )
            existing = len(self._content) - 1
        self.source.setCurrentIndex(existing)
        self.source_status.setText("Installed game content imported successfully.")

    def _set_import_controls(self, enabled):
        self.source.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.browse_button.setEnabled(enabled)
        self.import_button.setEnabled(
            enabled and self.importer.availability().available
        )
        self.game_folder_button.setEnabled(
            enabled and self.importer.availability().available
        )
        self.stories.setEnabled(enabled)
        self.select_all_button.setEnabled(enabled)
        self.select_none_button.setEnabled(enabled)
        self.change_voices.setEnabled(enabled)
        self.continue_button.setEnabled(enabled and bool(self.selected_story_ids()))

    def _cancel_or_reject(self):
        if (
            self.planning_voices
            or self.auditioning_voices
            or self.preparing_inputs
            or self.generating
            or self.recovering
            or self.accepting_audio
            or self.publishing_pack
        ):
            self._close_after_voice_cancel = True
            self.voice_cancel_event.set()
            if self.auditioning_voices:
                self.voice_panel.cancel()
            self.cancel_button.setEnabled(False)
            stage = (
                "automatic recovery"
                if self.recovering
                else "final save"
                if self.publishing_pack
                else "final checks"
                if self.accepting_audio
                else "generation"
                if self.generating
                else "offline preparation"
                if self.preparing_inputs
                else "voice selection"
                if self.auditioning_voices
                else "voice matching"
            )
            self.resume_status.setText(f"Cancelling {stage}...")
            return
        if not self.importing:
            self.reject()
            return
        self.import_cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.source_status.setText("Cancelling game import...")

    def closeEvent(self, event: QCloseEvent):
        if (
            self.planning_voices
            or self.auditioning_voices
            or self.preparing_inputs
            or self.generating
            or self.recovering
            or self.accepting_audio
            or self.publishing_pack
        ):
            self._cancel_or_reject()
            event.ignore()
            return
        if self.importing:
            self._cancel_or_reject()
            event.ignore()
            return
        self.import_runner.cancel()
        self.discovery_runner.cancel()
        self.voice_runner.cancel()
        self.input_runner.cancel()
        self.generation_runner.cancel()
        self.recovery_runner.cancel()
        self.acceptance_runner.cancel()
        self.publication_runner.cancel()
        self.progress_timer.stop()
        self.voice_panel.shutdown()
        super().closeEvent(event)

    def done(self, result):
        self.discovery_runner.cancel()
        self.progress_timer.stop()
        if not self.voice_panel.active:
            self.voice_panel.shutdown()
        super().done(result)


def _content_label(content):
    count = len(content.selections)
    return (
        f"{content.display_name} - {count} "
        f"{'story' if count == 1 else 'stories'} - {content.story_index.name}"
    )


__all__ = ["OfflineAudioPreparationDialog"]
