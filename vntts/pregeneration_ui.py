"""Guided player UI for selecting content to prepare for offline speech."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import monotonic, process_time
from typing import TypeAlias, TypeGuard, cast

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QSignalBlocker,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap, QResizeEvent, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vntts.application_directories import get_local_data_directory
from vntts.async_ui import LatestTaskRunner
from vntts.document_identity import sha256_file
from vntts.game_audio_decoder import DecoderSetupRequired, confirm_decoder_setup
from vntts.game_content_importer import (
    GameContentImportCancelled,
    Reverse1999GameImporter,
)
from vntts.game_narrator import load_original_reference
from vntts.person_link_suggestions import PersonLinkSuggestion
from vntts.pregeneration_audition_ui import (
    VoiceAuditionPanel,
    VoiceAuditionUIError,
    _VoiceAuditionPreviewer,
)
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationProgress,
    OfflineGenerationResult,
    OfflineGenerationWorker,
    runtime_progress_manifest_path,
)
from vntts.pregeneration_pack import (
    OfflinePackError,
    OfflinePackPublisher,
    OfflinePackResult,
    OfflinePreparationChanges,
    StoryAudioCoverage,
    inspect_story_audio,
    load_saved_pack,
)
from vntts.pregeneration_queue import (
    PregenerationInput,
    PregenerationInputStore,
    PregenerationQueueCancelled,
)
from vntts.pregeneration_recovery import OfflineRecoveryResult, OfflineRecoveryWorker
from vntts.pregeneration_setup import (
    ContentDiscovery,
    GameContent,
    GenerationResourceEstimate,
    PregenerationJob,
    PregenerationJobStore,
    PregenerationSetupError,
    StoryContentChanged,
    StorySelection,
    discover_game_content,
    estimate_generation_resources,
    estimate_preparation,
    inspect_story_index,
)
from vntts.pregeneration_voices import (
    PregenerationVoiceCancelled,
    PregenerationVoiceError,
    VoiceCandidate,
    VoiceDecisionStore,
    VoiceGroup,
    VoicePlan,
    VoicePlanStore,
    player_voice_catalog_is_current,
    pregeneration_narrator_source_id,
    resolve_pregeneration_settings,
)
from vntts.prepared_sequence import (
    PreparedSequenceError,
    prepare_sequence_for_prepared_story,
)
from vntts.qt_audio import QtPcmPlayer
from vntts.release_backends import speech_backend_options
from vntts.settings import AppSettings
from vntts.source_audio_progress import SourceAudioProgress
from vntts.source_audio_semantics import source_audio_semantics_need_migration
from vntts.speech_presentation import (
    compact_runtime_label,
    engine_model_label,
    speech_configuration_label,
    speech_configuration_rows,
)
from vntts.support import record_background_operation
from vntts.ui_text import (
    copy_text_button,
    make_text_copyable,
    plain_label_text,
    set_labeled_text,
)
from vntts.voice_candidate_cache import prune_obsolete_voice_candidate_caches
from vntts.voice_default_impact import StoryVoiceImpact
from vntts.voice_library import VoiceBindingRollback, VoiceLibrary, VoiceLibraryError
from vntts.voices import (
    CharacterVoiceRegistry,
    VoiceChoice,
    application_voice_library,
    find_default_voice_manifest,
    is_narrator,
    normalize_character_name,
    pocket_tts_preset_voices,
    remember_voice_binding,
    voice_binding_label,
    voice_binding_source_id,
)

StoryAudioKey: TypeAlias = tuple[str | None, str, str | None]
StoryAudioCheck: TypeAlias = tuple[
    StoryAudioCoverage | None,
    StoryAudioCoverage | None,
    Exception | str | None,
]


@dataclass(frozen=True)
class _SemanticVoicePlan:
    plan: VoicePlan
    job: PregenerationJob
    content: GameContent


def _prepare_runtime_sequence(
    generation_input: PregenerationInput, cancellation: Event
) -> Path | None:
    """Worker-safe optional playback setup; never makes preparation fail."""
    try:
        plan = prepare_sequence_for_prepared_story(
            generation_input.story_index,
            generation_input.directory / "live-sequence.json",
            cancellation=cancellation,
        )
    except PreparedSequenceError:
        return None
    return Path(plan.path)


class _StoryTree(QTreeWidget):
    """Address selectable rows separately from category and story headings."""

    def __init__(self) -> None:
        super().__init__()
        self._stories: list[QTreeWidgetItem] = []

    def clear(self) -> None:
        super().clear()
        self._stories.clear()

    def add_story(self, group: QTreeWidgetItem, story: QTreeWidgetItem) -> None:
        group.addChild(story)
        self._stories.append(story)

    def count(self) -> int:
        return len(self._stories)

    def item(self, row: int) -> QTreeWidgetItem:
        return self._stories[row]

    def setCurrentRow(self, row: int) -> None:
        self.setCurrentItem(self.item(row))

    def current_story_item(self) -> QTreeWidgetItem | None:
        item = self.currentItem()
        return (
            item
            if item is not None
            and isinstance(item.data(0, Qt.ItemDataRole.UserRole), str)
            else None
        )

    def story_descendants(self, item: QTreeWidgetItem | None) -> list[QTreeWidgetItem]:
        if item is None:
            return []
        if isinstance(item.data(0, Qt.ItemDataRole.UserRole), str):
            return [item]
        return [
            story
            for row in range(item.childCount())
            for story in self.story_descendants(item.child(row))
        ]


class _VoiceRouteList(QListWidget):
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.resize_rows()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self.resize_rows()

    def resize_rows(self) -> None:
        width = self.viewport().width()
        tallest = 90
        for row in range(self.count()):
            item = self.item(row)
            card = self.itemWidget(item)
            if card is not None and (layout := card.layout()) is not None:
                height = layout.totalHeightForWidth(width)
                if not item.icon().isNull():
                    height = max(height, self.iconSize().height() + 12)
                item.setSizeHint(QSize(width, height))
                tallest = max(tallest, height + 4)
        self.setMinimumHeight(tallest)


class OfflineAudioPreparationDialog(QDialog):
    cancel_button: QPushButton
    continue_button: QPushButton

    decoderProgress = Signal(str)
    sourceAudioProgress = Signal(object)
    phaseChanged = Signal(str)
    activityChanged = Signal(bool)
    preparationRequested = Signal()
    offlinePreferencesChanged = Signal(object)
    packReady = Signal()
    readingRequested = Signal()
    readingLineObserved = Signal(str, str)

    def __init__(
        self,
        settings: AppSettings,
        *,
        discovery: Callable[[], ContentDiscovery] | None = None,
        job_store: PregenerationJobStore | None = None,
        voice_plan_store: VoicePlanStore | None = None,
        voice_decisions: VoiceDecisionStore | None = None,
        audition_service: _VoiceAuditionPreviewer | None = None,
        preview_player: QtPcmPlayer | None = None,
        input_store: PregenerationInputStore | None = None,
        generator: OfflineGenerationWorker | None = None,
        recovery: OfflineRecoveryWorker | None = None,
        publisher: OfflinePackPublisher | None = None,
        importer: Reverse1999GameImporter | None = None,
        game_narrator_chooser: Callable[..., AppSettings | None] | None = None,
        automatic_activation: bool = False,
        thread_pool: QThreadPool | None = None,
        voice_library: VoiceLibrary | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.voice_library = voice_library or application_voice_library()
        self.automatic_activation = automatic_activation
        self.job_store = job_store or PregenerationJobStore()
        self._background_discovery = discovery is None
        self.discovery = discovery or (
            lambda: discover_game_content(
                settings,
                extra_paths=self.job_store.source_story_indexes(),
            )
        )
        self.voice_decisions = voice_decisions or VoiceDecisionStore(
            get_local_data_directory() / "pregeneration" / "voice-decisions.json",
            voice_library=self.voice_library,
        )
        self.voice_plan_store = voice_plan_store or VoicePlanStore(
            self.job_store,
            decisions=self.voice_decisions,
            voice_library=self.voice_library,
        )
        if self.voice_decisions.voice_library is None:
            self.voice_decisions.voice_library = self.voice_library
        if self.voice_plan_store.voice_library is None:
            self.voice_plan_store.voice_library = self.voice_library
        self.input_store = input_store or PregenerationInputStore(self.job_store)
        self.generator = generator or OfflineGenerationWorker()
        self.recovery = recovery or OfflineRecoveryWorker(self.generator)
        self.publisher = publisher or OfflinePackPublisher(base_pack=settings.game_pack)
        self.importer = importer or Reverse1999GameImporter()
        self.game_narrator_chooser = game_narrator_chooser
        self._initialize_task_runners(audition_service, preview_player, thread_pool)
        self._initialize_state(preview_player)
        self.readingLineObserved.connect(self._reading_line_observed)
        self._configure_window()
        narrator_row = self._build_voice_controls(game_narrator_chooser)
        source_row = self._build_source_controls()
        story_filters, selection_actions = self._build_story_selection_controls()
        self._build_progress_panel()
        self._build_voice_confirmation(game_narrator_chooser)
        self._build_discovery_panel()
        self._build_buttons()
        self._build_layout(narrator_row, source_row, story_filters, selection_actions)
        self._finish_setup()

    def _initialize_task_runners(
        self,
        audition_service: _VoiceAuditionPreviewer | None,
        preview_player: QtPcmPlayer | None,
        thread_pool: QThreadPool | None,
    ) -> None:
        self.discovery_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.discovery_runner.finished.connect(self._discovery_finished)
        self.coverage_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.coverage_runner.finished.connect(self._story_audio_finished)
        self.saved_pack_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.saved_pack_runner.finished.connect(self._saved_pack_finished)
        self.import_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.import_runner.finished.connect(self._import_finished)
        self.voice_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decoderProgress.connect(self._decoder_progress)
        self.sourceAudioProgress.connect(self._source_audio_progress)
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
        self.voice_panel.saveFailed.connect(lambda: self.cancel_button.setEnabled(True))
        self.input_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.input_runner.finished.connect(self._generation_input_finished)
        self.sequence_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.sequence_runner.finished.connect(self._prepared_sequence_finished)
        self.generation_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.generation_runner.finished.connect(self._generation_finished)
        self.recovery_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.recovery_runner.finished.connect(self._recovery_finished)
        self.publication_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.publication_runner.finished.connect(self._publication_finished)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(500)
        self.progress_timer.timeout.connect(self._poll_generation_progress)
        self.progress_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.progress_runner.finished.connect(self._progress_finished)
        for runner in (
            self.discovery_runner,
            self.import_runner,
            self.voice_runner,
            self.input_runner,
            self.sequence_runner,
            self.generation_runner,
            self.recovery_runner,
            self.publication_runner,
            self.saved_pack_runner,
        ):
            runner.activeChanged.connect(self.activityChanged.emit)

    def _initialize_state(self, preview_player: QtPcmPlayer | None) -> None:
        self._progress_baseline: tuple[float, int] | None = None
        self._progress_snapshot: OfflineGenerationProgress | None = None
        self._reading_line_id: str | None = None
        self._progress_changed_at = monotonic()
        self._progress_error: str | None = None
        self.import_cancel_event = Event()
        self.voice_cancel_event = Event()
        self.importing = False
        self._automatic_import = False
        self.planning_voices = False
        self.auditioning_voices = False
        self.inspecting_voice_plan = False
        self.replanning_voice_decisions = False
        self._fast_voice_choice_pending = False
        self._semantic_preparation_enabled = False
        self._generate_after_input = False
        self.preparing_inputs = False
        self.generating = False
        self.recovering = False
        self.publishing_pack = False
        self.activating_saved = False
        self._close_after_voice_cancel = False
        self._content: tuple[GameContent, ...] = ()
        self._story_selection_drafts: dict[str, set[str]] = {}
        self._story_groups: dict[str, QTreeWidgetItem] = {}
        self._story_group_expansion: dict[tuple[str, str], bool] = {}
        self._unsaved_story_selections: dict[
            str, tuple[GameContent, tuple[str, ...]]
        ] = {}
        self._story_audio_checks: dict[StoryAudioKey, StoryAudioCheck] = {}
        self._story_playback_speakers: dict[StoryAudioKey, tuple[str, ...]] = {}
        self._checking_story: StoryAudioKey | None = None
        self._story_job_statuses: dict[str, str] = {}
        self._job: PregenerationJob | None = None
        self._voice_plan: VoicePlan | None = None
        self._prepared_voice_manifest: str | Path | None = None
        self._prepared_voice_job: str | None = None
        self._generation_input: PregenerationInput | None = None
        self._prepared_original_audio_lines = 0
        self._prepared_sequence_plan: Path | None = None
        self._generation_result: OfflineGenerationResult | None = None
        self._recovery_result: OfflineRecoveryResult | None = None
        self._pack_result: OfflinePackResult | None = None
        self._awaiting_voice_confirmation = False
        self._generation_configuration: tuple[AppSettings, str | None] | None = None
        self._pending_voice_rematch = False
        self._stale_job_retry_attempted = False
        self._recovering_stale_job = False
        self._provisional_binding_rollback: VoiceBindingRollback | None = None
        self._changes_rows: tuple[tuple[str, str], ...] = ()
        self._resume_error_details = ""
        self._narrator_player = preview_player

    def _configure_window(self) -> None:
        self.setWindowTitle("Prepare offline audio")
        self.setMinimumSize(620, 440)
        self.resize(860, 720)
        self.step = QLabel("Step 1 of 4 - Choose stories")
        self.step.setStyleSheet("font-weight: 700;")
        self.step.setAccessibleName("Story preparation step")
        self.story_context = QLabel("Select the stories you want to read.")
        self.story_context.setWordWrap(True)
        self.story_context.setAccessibleName("Selected stories")
        self.story_context.hide()

    def _build_voice_controls(
        self, game_narrator_chooser: Callable[..., AppSettings | None] | None
    ) -> QHBoxLayout:
        self.engine_choice = QComboBox()
        self.engine_choice.setAccessibleName("Offline generation engine")
        for label, backend, available in speech_backend_options(
            self.settings.offline_speech_backend
        ):
            self.engine_choice.addItem(label, backend)
            model = self.engine_choice.model()
            if isinstance(model, QStandardItemModel):
                model.item(self.engine_choice.count() - 1).setEnabled(available)
        self.engine_choice.setCurrentIndex(
            max(0, self.engine_choice.findData(self.settings.offline_speech_backend))
        )
        self.model_choice = QLineEdit(self.settings.offline_tts_model or "")
        self.model_choice.setPlaceholderText("Default model shown above")
        self.model_choice.setAccessibleName("Offline generation model")
        self.model_choice.setEnabled(
            self.settings.offline_speech_backend in {"coqui-xtts", "moss-tts"}
        )
        self.model_choice.setVisible(self.model_choice.isEnabled())
        self.engine_choice.currentIndexChanged.connect(self._engine_changed)
        self.model_choice.textChanged.connect(self._model_changed)
        self.model_choice.editingFinished.connect(
            lambda: self.offlinePreferencesChanged.emit(self.settings)
        )
        self.engine_controls = QWidget()
        engine_row = QHBoxLayout(self.engine_controls)
        engine_row.setContentsMargins(0, 0, 0, 0)
        engine_row.addWidget(QLabel("Generate with"))
        engine_row.addWidget(self.engine_choice, 1)
        engine_row.addWidget(self.model_choice, 1)
        self.engine_controls.show()

        self.narrator_status = QLabel()
        self.narrator_status.setAccessibleName("Selected narrator voice")
        self.narrator_status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        narrator_row = QHBoxLayout()
        narrator_row.addWidget(self.narrator_status, 1)
        self.copy_narrator_details = copy_text_button(
            "Copy details", self._full_narrator_configuration, self
        )
        self.copy_narrator_details.setAccessibleName("Copy narrator configuration")
        narrator_row.addWidget(self.copy_narrator_details)
        self.game_narrator_button = QPushButton("Edit in Voices...")
        self.game_narrator_button.setAccessibleDescription(
            "Choose a narrator voice; the generation engine is selected above"
        )
        self.game_narrator_button.setVisible(game_narrator_chooser is not None)
        self.game_narrator_button.clicked.connect(self._choose_game_narrator)
        narrator_row.addWidget(self.game_narrator_button)
        self._refresh_narrator_status()

        self.pocket_voice_cloning = QCheckBox(
            "I accepted the Pocket terms; enable game voice cloning"
        )
        self.pocket_voice_cloning.setChecked(self.settings.pocket_gated_model_accepted)
        self.pocket_voice_cloning.setAccessibleDescription(
            "Enable reference-audio voice cloning after accepting the Pocket TTS "
            "model terms and signing in to Hugging Face"
        )
        self.pocket_voice_cloning.toggled.connect(self._pocket_cloning_toggled)
        self.pocket_terms = QLabel(
            'Accept the <a href="https://huggingface.co/kyutai/pocket-tts">Pocket '
            "model terms</a> and sign in to Hugging Face to clone game voices. "
            "Built-in voices need no account."
        )
        self.pocket_terms.setWordWrap(True)
        self.pocket_terms.setOpenExternalLinks(True)
        uses_pocket = (
            game_narrator_chooser is None
            and resolve_pregeneration_settings(self.settings).speech_backend
            == "pocket-tts"
        )
        self.pocket_voice_cloning.setVisible(uses_pocket)
        self.pocket_terms.setVisible(uses_pocket)

        return narrator_row

    def _build_source_controls(self) -> QHBoxLayout:
        self.source = QComboBox()
        self.source.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.source.setMinimumContentsLength(12)
        self.source.setAccessibleName("Detected game content")
        self.source.setAccessibleDescription(
            "Detected local story content available for offline preparation"
        )
        self.source.currentIndexChanged.connect(self._source_changed)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.browse_button = QPushButton("Extracted content...")
        self.browse_button.clicked.connect(self.browse)
        self.import_button = QPushButton("Find installed Reverse: 1999")
        self.import_button.clicked.connect(self.import_installed_game)
        self.game_folder_button = QPushButton("Game folder...")
        self.game_folder_button.clicked.connect(self.choose_game_folder)
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Game content"))
        source_row.addWidget(self.source, 1)
        source_row.addWidget(self.refresh_button)
        self.import_options = QWidget()
        import_options_layout = QVBoxLayout(self.import_options)
        import_options_layout.setContentsMargins(0, 0, 0, 0)
        import_options_layout.setSpacing(4)
        import_options_layout.addWidget(self.import_button)
        import_options_layout.addWidget(self.game_folder_button)
        self.advanced_import = QPushButton("Advanced import...")
        self.advanced_import.setCheckable(True)
        self.advanced_import.toggled.connect(self.browse_button.setVisible)
        import_options_layout.addWidget(self.advanced_import)
        import_options_layout.addWidget(self.browse_button)
        self.browse_button.hide()
        self.import_options_toggle = QPushButton("Add or import content...")
        self.import_options_toggle.setCheckable(True)
        self.import_options_toggle.toggled.connect(self._update_import_options)
        source_row.addWidget(self.import_options_toggle)

        return source_row

    def _build_story_selection_controls(self) -> tuple[QHBoxLayout, QHBoxLayout]:
        self._build_story_list_controls()
        story_filters = self._build_story_filters()
        selection_actions = self._build_story_selection_actions()
        self._build_story_selection_status()
        return story_filters, selection_actions

    def _build_story_list_controls(self) -> None:
        self.source_status = QLabel()
        self.source_status.setWordWrap(True)
        self.source_status.setAccessibleName("Game content status")
        self.stories = _StoryTree()
        self.stories.setHeaderLabels(("Story", "Audio status"))
        self.stories.header().setStretchLastSection(False)
        self.stories.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.stories.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.stories.setIndentation(18)
        self.stories.setAlternatingRowColors(True)
        self.stories.setAccessibleName("Stories, stages, and chapters")
        self.stories.setAccessibleDescription(
            "Expand a type and story with Left and Right. Check a type or story to "
            "select all its stages, or check an individual stage. Audio status is in "
            "the second column."
        )
        self.stories.setMinimumHeight(120)
        self.stories.itemChanged.connect(self._story_item_changed)
        self.stories.currentItemChanged.connect(self._story_audio_changed)
        self.stories.itemExpanded.connect(self._story_group_expansion_changed)
        self.stories.itemCollapsed.connect(self._story_group_expansion_changed)
        self.check_story_audio = QPushButton("Check story audio")
        self.check_story_audio.setEnabled(False)
        self.check_story_audio.clicked.connect(self._check_story_audio)
        self.story_audio_status = QLabel("Highlight a story to check its saved audio.")
        self.story_audio_status.setWordWrap(True)
        self.story_audio_status.setAccessibleName("Highlighted story audio coverage")

    def _build_story_filters(self) -> QHBoxLayout:
        self.story_search = QLineEdit()
        self.story_search.setPlaceholderText("Search stories...")
        self.story_search.setAccessibleName(
            "Search stories by title, character, or episode"
        )
        self.story_search.setClearButtonEnabled(True)
        self.story_search.textChanged.connect(self._filter_stories)
        self.story_filter = QComboBox()
        self.story_filter.setAccessibleName("Filter stories by preparation status")
        for label, status in (
            ("All stories", None),
            ("Not prepared", "not_started"),
            ("Preparing", "preparing"),
            ("Partially prepared", "in_progress"),
            ("Ready", "ready"),
            ("Needs attention", "attention"),
        ):
            self.story_filter.addItem(label, status)
        self.story_filter.currentIndexChanged.connect(self._filter_stories)
        story_filters = QHBoxLayout()
        story_filters.addWidget(self.story_search, 1)
        story_filters.addWidget(self.story_filter)
        self.expand_stories_button = QPushButton("Expand all")
        self.expand_stories_button.setToolTip("Show every story and stage")
        self.expand_stories_button.clicked.connect(self.stories.expandAll)
        story_filters.addWidget(self.expand_stories_button)
        self.collapse_stories_button = QPushButton("Collapse all")
        self.collapse_stories_button.setToolTip("Show only story types")
        self.collapse_stories_button.clicked.connect(self.stories.collapseAll)
        story_filters.addWidget(self.collapse_stories_button)
        self.story_filter_status = QLabel()
        self.story_filter_status.setAccessibleName("Shown and selected story counts")
        return story_filters

    def _build_story_selection_actions(self) -> QHBoxLayout:
        selection_actions = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self.select_none_button = QPushButton("Select none")
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))
        self.change_voices = QCheckBox(
            "Choose character voices again for selected stories"
        )
        self.change_voices.toggled.connect(self._selection_changed)
        self.change_voices.setAccessibleDescription(
            "Clear saved non-narrator choices for characters in the selected stories "
            "and run automatic voice matching again"
        )
        selection_actions.addWidget(self.select_all_button)
        selection_actions.addWidget(self.select_none_button)
        selection_actions.addWidget(self.change_voices)
        selection_actions.addStretch()
        return selection_actions

    def _build_story_selection_status(self) -> None:
        self.summary = QLabel("Select game content to continue.")
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Offline preparation estimate")
        self.prepare_again = QPushButton("Prepare selected stories again")
        self.prepare_again.setAccessibleDescription(
            "Apply current voice defaults only to checked stories, reusing matching recordings and saved voice choices"
        )
        self.prepare_again.clicked.connect(self._prepare_again_requested)
        self.prepare_again.hide()
        self.selection_status = QLabel()
        self.selection_status.setWordWrap(True)
        self.selection_status.setTextFormat(Qt.TextFormat.PlainText)
        self.selection_status.setAccessibleName("Preparation error")
        self.selection_status.setStyleSheet("font-weight: 600;")
        self.selection_status.hide()
        self.resume_status = QLabel()
        self.resume_status.setTextFormat(Qt.TextFormat.PlainText)
        self.resume_status.setWordWrap(True)
        self.resume_status.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.resume_status.setAccessibleName("Offline preparation phase detail")
        self.copy_resume_error = copy_text_button(
            "Copy error details", lambda: self._resume_error_details, self
        )
        self.copy_resume_error.setAccessibleName("Copy full preparation error")
        self.copy_resume_error.hide()

    def _build_progress_panel(self) -> None:
        self.progress_panel = QWidget()
        self.progress_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.progress_phase = QLabel()
        self.progress_phase.setAccessibleName("Offline preparation phase")
        phase_font = self.progress_phase.font()
        phase_font.setBold(True)
        phase_font.setPointSizeF(phase_font.pointSizeF() * 1.2)
        self.progress_phase.setFont(phase_font)
        self.progress_scope = QLabel()
        self.progress_scope.setWordWrap(True)
        self.progress_scope.setAccessibleName("Preparation scope")
        self.progress_configuration = QLabel()
        self.progress_configuration.setWordWrap(True)
        self.progress_configuration.setAccessibleName("Locked generation configuration")
        self.copy_progress_configuration = copy_text_button(
            "Copy config details", self._full_generation_details, self
        )
        self.copy_progress_configuration.setAccessibleName(
            "Copy locked generation configuration"
        )
        self.progress_bar = QProgressBar()
        self.progress_bar.setAccessibleName("Durably completed generation items")
        self.progress_bar.setTextVisible(True)
        self.progress_counts = QLabel()
        self.progress_counts.setAccessibleName("Offline generation durable counts")
        self.progress_counts.setWordWrap(True)
        self.progress_counts.setStyleSheet("font-weight: 600;")
        self.progress_timing = QLabel()
        self.progress_timing.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_timing.setWordWrap(True)
        self.progress_timing.setAccessibleName("Progress freshness and remaining time")
        self.progress_runtime = QLabel()
        self.progress_runtime.setWordWrap(True)
        self.progress_runtime.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_runtime.setAccessibleName("Offline generation device")
        self.progress_cancel_consequence = QLabel()
        self.progress_cancel_consequence.setAccessibleName(
            "Cancel and resume consequence"
        )
        self.progress_cancel_consequence.setWordWrap(True)
        self.progress_coverage = QLabel()
        self.progress_coverage.setAccessibleName("Final offline audio coverage")
        self.progress_coverage.setWordWrap(True)
        self._build_progress_story_readiness()
        self.play_ready_button = QPushButton("Start reading")
        self.play_ready_button.setAccessibleDescription(
            "Start the selected story while preparation continues in the background"
        )
        self.play_ready_button.clicked.connect(self.readingRequested.emit)
        self.play_ready_button.hide()
        self._layout_progress_panel()

    def _build_progress_story_readiness(self) -> None:
        self.progress_story_readiness_label = QLabel("Ready to read")
        self.progress_story_readiness_label.setToolTip(
            "Includes original game audio and prepared speech. Read without waiting "
            "shows how many lines can be read in a row from the start or current "
            "position before more audio is needed."
        )
        self.progress_story_readiness_label.setStyleSheet("font-weight: 600;")
        self.progress_story_readiness = QTreeWidget()
        self.progress_story_readiness.setColumnCount(3)
        self.progress_story_readiness.setHeaderLabels(
            ["Story", "Ready lines", "Read without waiting"]
        )
        self.progress_story_readiness.setRootIsDecorated(False)
        self.progress_story_readiness.setItemsExpandable(True)
        self.progress_story_readiness.setIndentation(18)
        self._progress_story_selections: tuple[StorySelection, ...] = ()
        self._progress_story_items: dict[str, QTreeWidgetItem] = {}
        self._progress_story_groups: dict[tuple[str, str], QTreeWidgetItem] = {}
        self.progress_story_readiness.setAlternatingRowColors(True)
        self.progress_story_readiness.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column in (1, 2):
            self.progress_story_readiness.header().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.progress_story_readiness.setAccessibleName(
            "Selected story preparation readiness"
        )
        self.progress_story_readiness.setAccessibleDescription(
            "Story totals cover selected chapters. Use Left and Right to collapse "
            "or expand a story and inspect its chapters."
        )
        self.progress_story_readiness.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.progress_story_readiness.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.progress_story_readiness.setWordWrap(True)
        self.progress_story_readiness.setMinimumHeight(180)

    def _layout_progress_panel(self) -> None:
        progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(8, 8, 8, 8)
        progress_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        progress_layout.addWidget(self.progress_phase)
        progress_layout.addWidget(self.resume_status)
        progress_layout.addWidget(self.progress_scope)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_counts)
        progress_layout.addWidget(self.progress_timing)
        progress_layout.addWidget(self.progress_story_readiness_label)
        progress_layout.addWidget(self.progress_story_readiness)
        progress_layout.addWidget(self.progress_coverage)
        self.progress_change_selection = QPushButton("Change stories or voices...")
        self.progress_change_selection.clicked.connect(self._return_to_story_selection)
        self.progress_change_selection.hide()
        progress_layout.addWidget(
            self.progress_change_selection, alignment=Qt.AlignmentFlag.AlignLeft
        )
        self.progress_details_toggle = QPushButton("Show generation details")
        self.progress_details_toggle.setCheckable(True)
        self.progress_details_toggle.setAccessibleDescription(
            "Show device, locked voice and model configuration, and source details"
        )
        progress_layout.addWidget(
            self.progress_details_toggle, alignment=Qt.AlignmentFlag.AlignLeft
        )
        self.progress_details = QWidget()
        details_layout = QVBoxLayout(self.progress_details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        configuration_row = QHBoxLayout()
        configuration_row.addWidget(self.progress_configuration, 1)
        configuration_row.addWidget(self.copy_progress_configuration)
        details_layout.addWidget(self.progress_runtime)
        details_layout.addLayout(configuration_row)
        details_layout.addWidget(self.story_context)
        progress_layout.addWidget(self.progress_details)
        self.progress_details.hide()
        self.progress_details_toggle.toggled.connect(
            self._set_progress_details_expanded
        )
        self.progress_panel.hide()

    def _set_progress_details_expanded(self, expanded: bool) -> None:
        self.progress_details.setVisible(expanded)
        self.progress_details_toggle.setText(
            "Hide generation details" if expanded else "Show generation details"
        )

    def _build_voice_confirmation(
        self, game_narrator_chooser: Callable[..., AppSettings | None] | None
    ) -> None:
        self.voice_confirmation = QGroupBox("Voices for selected stories")
        self.voice_confirmation.setVisible(False)
        self.voice_configuration = QLabel()
        self.voice_configuration.setWordWrap(True)
        self.confirmed_narrator = QLabel()
        self.confirmed_narrator.setTextFormat(Qt.TextFormat.PlainText)
        self.confirmed_narrator.setWordWrap(True)
        self.confirmed_narrator.setAccessibleName("Narrator for prepared audio")
        self.edit_confirmed_narrator = QPushButton("Change narrator...")
        self.edit_confirmed_narrator.clicked.connect(self._choose_game_narrator)
        self.edit_confirmed_narrator.setVisible(game_narrator_chooser is not None)
        self.work_summary = QLabel()
        self.work_summary.setWordWrap(True)
        self.work_summary.setAccessibleName("Selected story preparation estimate")
        self.change_summary = QLabel()
        self.change_summary.setWordWrap(True)
        self.change_summary.setAccessibleName("Changes before generation")
        self.confirmed_narrator.setVisible(game_narrator_chooser is not None)
        self._build_preparation_details()
        self._build_narrator_confirmation_controls(game_narrator_chooser)
        self._build_voice_route_confirmation_controls()
        self.voice_confirmation_status = QLabel()
        self.voice_confirmation_status.setTextFormat(Qt.TextFormat.PlainText)
        self.voice_confirmation_status.setWordWrap(True)
        self._layout_voice_confirmation()

    def _build_preparation_details(self) -> None:
        self.preparation_details_toggle = QCheckBox("Preparation details")
        self.preparation_details = QWidget()
        self.voice_engine_details = QLabel()
        self.voice_engine_details.setTextFormat(Qt.TextFormat.PlainText)
        self.voice_engine_details.setWordWrap(True)
        details = QVBoxLayout(self.preparation_details)
        details.setContentsMargins(8, 4, 8, 4)
        details.addWidget(self.work_summary)
        details.addWidget(self.voice_engine_details)
        details.addWidget(self.voice_configuration)
        self.preparation_details.hide()
        self.preparation_details_toggle.toggled.connect(
            self.preparation_details.setVisible
        )

    def _build_narrator_confirmation_controls(
        self, game_narrator_chooser: Callable[..., AppSettings | None] | None
    ) -> None:
        self.narrator_choice = QComboBox()
        self.narrator_choice.setAccessibleName("Narrator voice for offline generation")
        self.narrator_choice.currentIndexChanged.connect(self._narrator_choice_changed)
        self.play_narrator_reference = QPushButton("Listen to reference")
        self.play_narrator_reference.setToolTip(
            "Play the original game recording, not a generated preview."
        )
        self.play_narrator_reference.clicked.connect(self._play_narrator_reference)
        narrator_choice_row = QHBoxLayout()
        narrator_choice_row.addWidget(QLabel("Narrator"))
        narrator_choice_row.addWidget(self.narrator_choice, 1)
        narrator_choice_row.addWidget(self.play_narrator_reference)
        self.narrator_controls = QWidget()
        self.narrator_controls.setLayout(narrator_choice_row)
        self.narrator_controls.setVisible(game_narrator_chooser is None)

    def _build_voice_route_confirmation_controls(self) -> None:
        self.voice_routes = _VoiceRouteList()
        self.voice_routes.setIconSize(QSize(64, 64))
        self.voice_routes.setAccessibleName("Character voices for selected stories")
        self.voice_routes.setMinimumHeight(90)
        self.voice_routes.setAlternatingRowColors(True)
        self.voice_routes.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._build_voice_route_header()
        self.choose_character_voice = QPushButton("Edit role in Voices...")
        self.choose_character_voice.setAccessibleName("Change selected character voice")
        self.choose_character_voice.setToolTip(
            "Choose another voice, use the narrator, or restore automatic matching."
        )
        self.choose_character_voice.setEnabled(False)
        self.choose_character_voice.clicked.connect(self._choose_character_voice)
        self.voice_routes.itemDoubleClicked.connect(
            lambda _item: self._inspect_character_voice()
        )
        self.voice_route_details = QLabel()
        self.voice_route_details.setTextFormat(Qt.TextFormat.PlainText)
        self.voice_route_details.setWordWrap(True)
        self.voice_route_details.setAccessibleName("Selected voice reference details")
        self.voice_routes.currentItemChanged.connect(self._show_voice_route_details)
        self.voice_route_summary = QLabel()
        self.voice_route_summary.setWordWrap(True)
        self.voice_routes_empty = QLabel()
        self.voice_routes_empty.setWordWrap(True)
        self.voice_routes_empty.setAccessibleName("Character voice list status")
        self.voice_routes_empty.hide()
        self.inspect_character_voice = QPushButton("Listen or change voice...")
        self.inspect_character_voice.setAccessibleDescription(
            "Listen to a role's game references and choose its voice."
        )
        self.inspect_character_voice.setEnabled(False)
        self.inspect_character_voice.clicked.connect(self._inspect_character_voice)
        self.link_identity = QPushButton("Link same person...")
        self.link_identity.clicked.connect(self._link_selected_person)
        self.unlink_identity = QPushButton("Unlink name")
        self.unlink_identity.clicked.connect(self._unlink_selected_person)
        self.identity_suggestion = QLabel()
        self.identity_suggestion.setTextFormat(Qt.TextFormat.PlainText)
        self.identity_suggestion.setWordWrap(True)
        self.identity_suggestion.setAccessibleName("Possible same-person match")
        self.identity_suggestion.hide()
        self.voice_routes.currentItemChanged.connect(
            lambda _item, _previous: self._update_voice_route_actions()
        )
        self._build_more_voice_actions()
        self.back_to_story_selection = QPushButton("Back to stories")
        self.back_to_story_selection.clicked.connect(self._return_to_story_selection)
        self.show_all_voice_routes = QCheckBox("Substitutions only")
        self.show_all_voice_routes.setToolTip(
            "Show substitute voices and suggested reviews only."
        )
        self.show_all_voice_routes.toggled.connect(
            lambda: self._render_voice_routes(self._voice_plan)
        )

    def _build_voice_route_header(self) -> None:
        self.voice_routes_header = QWidget()
        header = QGridLayout(self.voice_routes_header)
        self.voice_routes_header_layout = header
        header.setContentsMargins(8, 0, 8, 0)
        for column, title in enumerate(("Character", "Voice", "Selected by", "Lines")):
            label = QLabel(title)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
            label.setWordWrap(True)
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            if column == 3:
                label.setWordWrap(False)
                label.setSizePolicy(
                    QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred
                )
                label.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            header.addWidget(label, 0, column)
        for column, stretch in enumerate((3, 3, 2, 1)):
            header.setColumnStretch(column, stretch)

    def _build_more_voice_actions(self) -> None:
        self.more_voice_actions_toggle = QCheckBox("More actions")
        self.more_voice_actions = QWidget()
        actions = QVBoxLayout(self.more_voice_actions)
        actions.setContentsMargins(8, 4, 8, 4)
        actions.addWidget(self.identity_suggestion)
        buttons = QVBoxLayout()
        buttons.addWidget(
            self.choose_character_voice, alignment=Qt.AlignmentFlag.AlignLeft
        )
        buttons.addWidget(self.link_identity, alignment=Qt.AlignmentFlag.AlignLeft)
        buttons.addWidget(self.unlink_identity, alignment=Qt.AlignmentFlag.AlignLeft)
        buttons.addStretch()
        actions.addLayout(buttons)
        self.more_voice_actions.hide()
        self.more_voice_actions_toggle.toggled.connect(
            self.more_voice_actions.setVisible
        )

    def _layout_voice_confirmation(self) -> None:
        confirmation_layout = QVBoxLayout(self.voice_confirmation)
        confirmation_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        narrator_row = QHBoxLayout()
        narrator_row.addWidget(self.confirmed_narrator, 1)
        narrator_row.addWidget(
            self.edit_confirmed_narrator, alignment=Qt.AlignmentFlag.AlignTop
        )
        confirmation_layout.addLayout(narrator_row)
        confirmation_layout.addWidget(self.change_summary)
        confirmation_layout.addWidget(self.narrator_controls)
        confirmation_layout.addWidget(self.voice_route_summary)
        confirmation_layout.addWidget(self.show_all_voice_routes)
        confirmation_layout.addWidget(self.voice_routes_header)
        confirmation_layout.addWidget(self.voice_routes, 1)
        confirmation_layout.addWidget(self.voice_routes_empty)
        confirmation_layout.addWidget(self.voice_route_details)
        actions = QHBoxLayout()
        actions.addWidget(self.back_to_story_selection)
        actions.addStretch()
        actions.addWidget(self.inspect_character_voice)
        confirmation_layout.addLayout(actions)
        confirmation_layout.addWidget(self.more_voice_actions_toggle)
        confirmation_layout.addWidget(self.more_voice_actions)
        confirmation_layout.addWidget(self.preparation_details_toggle)
        confirmation_layout.addWidget(self.preparation_details)
        confirmation_layout.addWidget(self.voice_confirmation_status)
        confirmation_layout.addStretch()

    def _build_discovery_panel(self) -> None:
        self.discovery_panel = QGroupBox("Loading game content")
        self.discovery_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.discovery_panel.setAccessibleName("Loading game content")
        self.discovery_message = QLabel(
            "Finding local stories. Controls unlock when loading finishes."
        )
        self.discovery_message.setWordWrap(True)
        self.discovery_message.setStyleSheet("font-weight: 600;")
        self.discovery_progress = QProgressBar()
        self.discovery_progress.setRange(0, 0)
        self.discovery_progress.setTextVisible(False)
        self.discovery_progress.setAccessibleName("Finding local game content")
        self.discovery_import_button = QPushButton("Find installed Reverse: 1999")
        self.discovery_import_button.clicked.connect(self.import_installed_game)
        self.discovery_game_folder_button = QPushButton("Choose game folder...")
        self.discovery_game_folder_button.clicked.connect(self.choose_game_folder)
        discovery_layout = QVBoxLayout(self.discovery_panel)
        discovery_layout.addWidget(self.discovery_message)
        discovery_layout.addWidget(self.discovery_progress)
        discovery_actions = QHBoxLayout()
        discovery_actions.addStretch()
        discovery_actions.addWidget(self.discovery_import_button)
        discovery_actions.addWidget(self.discovery_game_folder_button)
        discovery_layout.addLayout(discovery_actions)
        self.discovery_panel.hide()

    def _build_buttons(self) -> None:
        self.buttons = QDialogButtonBox()
        self.buttons.addButton(
            self.play_ready_button, QDialogButtonBox.ButtonRole.ActionRole
        )
        cancel_button = self.buttons.addButton(
            "Cancel", QDialogButtonBox.ButtonRole.RejectRole
        )
        continue_button = self.buttons.addButton(
            "Continue", QDialogButtonBox.ButtonRole.AcceptRole
        )
        assert cancel_button is not None
        assert continue_button is not None
        self.cancel_button = cancel_button
        self.continue_button = continue_button
        self.continue_button.setDefault(True)
        self.continue_button.setAccessibleDescription(
            "Save this selection and continue or resume offline audio preparation"
        )
        self.continue_button.clicked.connect(self._continue_requested)
        self.cancel_button.clicked.connect(self._cancel_or_reject)

    def _build_layout(
        self,
        narrator_row: QHBoxLayout,
        source_row: QHBoxLayout,
        story_filters: QHBoxLayout,
        selection_actions: QHBoxLayout,
    ) -> None:
        selection_content = QWidget()
        selection_layout = QVBoxLayout(selection_content)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        selection_layout.setSpacing(4)
        selection_layout.addLayout(narrator_row)
        selection_layout.addWidget(self.selection_status)
        selection_layout.addWidget(self.pocket_voice_cloning)
        selection_layout.addWidget(self.pocket_terms)
        selection_layout.addWidget(self.engine_controls)
        selection_layout.addLayout(source_row)
        selection_layout.addWidget(self.import_options)
        selection_layout.addWidget(self.source_status)
        selection_layout.addLayout(story_filters)
        selection_layout.addWidget(self.summary)
        selection_layout.addWidget(self.story_filter_status)
        selection_layout.addWidget(self.stories, 1)
        selection_layout.addLayout(selection_actions)
        story_audio_row = QHBoxLayout()
        story_audio_row.addWidget(self.story_audio_status, 1)
        story_audio_row.addWidget(self.check_story_audio)
        selection_layout.addLayout(story_audio_row)
        selection_layout.addWidget(self.prepare_again)
        self.selection_panel = QScrollArea()
        self.selection_panel.setWidgetResizable(True)
        self.selection_panel.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.selection_panel.setFrameShape(QScrollArea.Shape.NoFrame)
        self.selection_panel.setWidget(selection_content)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setSpacing(4)
        layout.addWidget(self.discovery_panel)
        layout.addWidget(self.voice_panel)
        layout.addWidget(self.voice_confirmation, 1)
        layout.addWidget(self.progress_panel)
        layout.addStretch()
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.content_scroll.setWidget(content)
        shell = QVBoxLayout(self)
        shell.addWidget(self.step)
        shell.addWidget(self.selection_panel, 1)
        shell.addWidget(self.content_scroll, 1)
        shell.addWidget(self.copy_resume_error)
        self.progress_cancel_consequence.hide()
        shell.addWidget(self.progress_cancel_consequence)
        shell.addWidget(self.buttons)
        self._configure_text_accessibility()

    def _configure_text_accessibility(self) -> None:
        make_text_copyable(self)
        self.voice_route_details.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setTabOrder(self.voice_routes, self.voice_route_details)
        self.setTabOrder(self.voice_route_details, self.back_to_story_selection)
        self.setTabOrder(self.back_to_story_selection, self.inspect_character_voice)
        self.setTabOrder(self.inspect_character_voice, self.more_voice_actions_toggle)

    def _finish_setup(self) -> None:
        availability = self.importer.availability()
        self.import_button.setEnabled(availability.available)
        self.import_button.setToolTip(availability.message)
        self.game_folder_button.setEnabled(availability.available)
        self.game_folder_button.setToolTip(availability.message)
        self.discovery_import_button.setEnabled(availability.available)
        self.discovery_import_button.setToolTip(availability.message)
        self.discovery_game_folder_button.setEnabled(availability.available)
        self.discovery_game_folder_button.setToolTip(availability.message)
        if self._background_discovery:
            self._set_discovery_loading(True)
            QTimer.singleShot(0, self, self.refresh)
        else:
            self.refresh()

    def refresh(self) -> None:
        self.discovery_message.setText(
            "Finding local stories. Controls unlock when loading finishes."
        )
        self.coverage_runner.cancel()
        self._checking_story = None
        self._story_audio_checks.clear()
        self._story_playback_speakers.clear()
        self._prepared_voice_manifest = None
        self._prepared_voice_job = None
        self._story_audio_changed()
        if self._background_discovery:
            self._set_discovery_loading(True)
            self.discovery_runner.start(self._discover_content_and_check_updates)
            return
        self._apply_discovery(self._discover_content())

    def _choose_game_narrator(self) -> None:
        if self.has_pending_work():
            return
        assert self.game_narrator_chooser is not None
        previous_voices = self.voice_library.bindings()
        settings = self.game_narrator_chooser(self.settings, self)
        if settings is None:
            return
        self.apply_narrator_settings(
            settings,
            voice_changed=self.voice_library.bindings() != previous_voices,
        )

    def _choose_character_voice(self) -> None:
        item = self.voice_routes.currentItem()
        if item is None or self.has_pending_work() or self._voice_plan_needs_update():
            return
        character = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(character, str) or not character:
            return
        if self._narrator_player is not None:
            self._narrator_player.stop()
        previous_voices = self.voice_library.bindings()
        if self.game_narrator_chooser is not None:
            settings = self.game_narrator_chooser(
                self.settings, self, character=character
            )
        else:
            from vntts.game_narrator_ui import GameNarratorDialog

            dialog = GameNarratorDialog(self.settings, self, importer=self.importer)
            dialog.set_voice_context(self._voice_plan, character=character)
            settings = (
                dialog.result_settings
                if dialog.exec() == QDialog.DialogCode.Accepted
                else None
            )
        if settings is not None:
            self.apply_narrator_settings(
                settings,
                voice_changed=self.voice_library.bindings() != previous_voices,
            )

    def _inspect_character_voice(self) -> None:
        item = self.voice_routes.currentItem()
        if (
            item is None
            or self.has_pending_work()
            or self._voice_plan is None
            or self._voice_plan_needs_update()
        ):
            return
        group_id = item.data(int(Qt.ItemDataRole.UserRole) + 1)
        try:
            self.voice_panel.start(self._voice_plan, group_id=group_id)
        except VoiceAuditionUIError as error:
            self.voice_confirmation_status.setText(
                f"Unable to inspect {item.data(Qt.ItemDataRole.UserRole)}'s voice: {error}\n"
                "Choose another voice through More actions > Edit role in Voices."
            )
            return
        self._awaiting_voice_confirmation = False
        self.step.setText("Step 2 of 4 - Inspect selected voice")
        self.voice_confirmation.hide()
        self.auditioning_voices = True
        self.inspecting_voice_plan = True
        self.cancel_button.setText("Back to voice plan")
        self.cancel_button.setAutoDefault(False)
        self.voice_panel.a_use.setDefault(True)
        self.cancel_button.setToolTip(
            "Return to the voice plan. An unsaved candidate is not applied; "
            "a voice choice already being saved finishes before returning."
        )
        self.continue_button.hide()
        self.selection_panel.hide()
        self.voice_panel.voice_reference.setFocus()

    def _selected_routing_role(self) -> str | None:
        item = self.voice_routes.currentItem()
        role = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return role if isinstance(role, str) and role else None

    def _update_voice_route_actions(self) -> None:
        role = self._selected_routing_role()
        ready = role is not None and not self._voice_plan_needs_update()
        self.inspect_character_voice.setEnabled(ready)
        self.choose_character_voice.setEnabled(ready)
        if role and normalize_character_name(role) == "narrator":
            role = None
        linked = bool(role and self.voice_library.linked_variant_key(role))
        aliases = self._linked_person_aliases(role)
        self.link_identity.setEnabled(
            ready and bool(role) and not linked and not aliases
        )
        self.unlink_identity.setEnabled(ready and (linked or bool(aliases)))
        suggestions = self._suggested_person_links(role) if not linked else ()
        if suggestions:
            matches = ", ".join(
                f"{target} ({len(suggestion.shared_portraits)} matching portraits, "
                f"{len(suggestion.shared_source_banks)} shared voice bank"
                f"{'s' if len(suggestion.shared_source_banks) != 1 else ''})"
                for target, suggestion in suggestions
            )
            self.identity_suggestion.setText(
                f"Possible same person: {matches}. Confirm before linking; "
                "Linking shares one voice across both names."
            )
        elif aliases:
            self.identity_suggestion.setText(f"Linked names: {', '.join(aliases)}")
        self.identity_suggestion.setAccessibleName(
            "Linked character names" if aliases else "Possible same-person match"
        )
        self.identity_suggestion.setVisible(bool(suggestions or aliases))
        self.more_voice_actions_toggle.setText(
            "More actions (possible same person)" if suggestions else "More actions"
        )

    def _linked_person_aliases(self, role: str | None) -> tuple[str, ...]:
        return tuple(
            sorted(
                alias
                for alias, canonical in self.voice_library.person_aliases().items()
                if role is not None
                and normalize_character_name(canonical)
                == normalize_character_name(role)
            )
        )

    def _suggested_person_links(
        self, role: str | None, *, plan: VoicePlan | None = None
    ) -> tuple[tuple[str, PersonLinkSuggestion], ...]:
        plan = plan or self._voice_plan
        if role is None or plan is None or not plan.person_link_suggestions:
            return ()
        if self._job is not None and (
            plan.job_id != self._job.job_id
            or plan.story_index_sha256 != self._job.story_index_sha256
        ):
            return ()
        key = normalize_character_name(role)
        relevant = tuple(
            suggestion
            for suggestion in plan.person_link_suggestions
            if key == normalize_character_name(suggestion.left_role)
            or key == normalize_character_name(suggestion.right_role)
        )
        if not relevant or self.voice_library.linked_variant_key(role):
            return ()
        canonical_key = normalize_character_name(
            self.voice_library.canonical_role(role)
        )
        matches: dict[str, tuple[str, PersonLinkSuggestion]] = {}
        for suggestion in relevant:
            left = normalize_character_name(suggestion.left_role)
            right = normalize_character_name(suggestion.right_role)
            target = (
                suggestion.right_role
                if key == left
                else suggestion.left_role
                if key == right
                else None
            )
            if target is None:
                continue
            target = self.voice_library.canonical_role(target)
            target_key = normalize_character_name(target)
            if target_key == canonical_key:
                continue
            previous = matches.get(target_key)
            if previous is None or len(suggestion.shared_portraits) > len(
                previous[1].shared_portraits
            ):
                matches[target_key] = target, suggestion
        return tuple(sorted(matches.values(), key=lambda match: match[0].casefold()))

    def _link_selected_person(self) -> None:
        role = self._selected_routing_role()
        plan = self._voice_plan
        if (
            role is None
            or normalize_character_name(role) == "narrator"
            or plan is None
            or self.has_pending_work()
            or self._voice_plan_needs_update()
        ):
            return
        suggestions = self._suggested_person_links(role)
        suggested_names = [target for target, _suggestion in suggestions]
        suggested_keys = {normalize_character_name(name) for name in suggested_names}
        other_names = sorted(
            {
                self.voice_library.canonical_role(group.routing_role or group.character)
                for group in plan.groups
                if normalize_character_name(group.routing_role or group.character)
                != normalize_character_name(role)
                and normalize_character_name(group.character) != "narrator"
                and normalize_character_name(
                    self.voice_library.canonical_role(
                        group.routing_role or group.character
                    )
                )
                != normalize_character_name(role)
            },
            key=str.casefold,
        )
        names = suggested_names + [
            name
            for name in other_names
            if normalize_character_name(name) not in suggested_keys
        ]
        if len(suggestions) != 1:
            names.insert(0, "")
        prompt = f"Treat {role} as another name for:"
        if len(suggestions) == 1:
            prompt = (
                f"Game evidence suggests {suggestions[0][0]}. "
                "Confirm the identity, or choose another name:"
            )
        target, accepted = QInputDialog.getItem(
            self,
            "Link character names",
            prompt,
            names,
            0,
            True,
        )
        if not accepted:
            return
        target = target.strip()
        if not target or normalize_character_name(target) == normalize_character_name(
            role
        ):
            self.voice_confirmation_status.setText(
                "Enter a different character name to share its voice."
            )
            return
        affected_line_ids = {
            line_id
            for group in plan.groups
            if normalize_character_name(group.routing_role or group.character)
            == normalize_character_name(role)
            for line_id in group.line_ids
        }
        content = self.current_content()
        stories = (
            [
                selection.title
                for selection in content.selections
                if self._job is not None
                and selection.selection_id in self._job.selected_story_ids
                and affected_line_ids.intersection(selection.line_ids)
            ]
            if content is not None
            else []
        )
        story_preview = ", ".join(stories[:4]) or "selected stories"
        if len(stories) > 4:
            story_preview += f" and {len(stories) - 4} more"
        if (
            QMessageBox.question(
                self,
                "Link character names",
                f"Treat {role} as another name for {target}? This affects "
                f"{len(affected_line_ids)} lines in {story_preview}, plus future stories. "
                "Both names will share one voice choice and reference list. "
                "The earlier separate choice can be restored by unlinking.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.voice_library.link_person(target, role)
        except (OSError, VoiceLibraryError) as error:
            self.voice_confirmation_status.setText(f"Unable to link names: {error}")
            return
        self._voice_auditions_completed()

    def _unlink_selected_person(self) -> None:
        role = self._selected_routing_role()
        if role is None or self.has_pending_work() or self._voice_plan_needs_update():
            return
        try:
            if not self.voice_library.linked_variant_key(role):
                aliases = self._linked_person_aliases(role)
                if not aliases:
                    return
                role = aliases[0]
                if len(aliases) > 1:
                    role, accepted = QInputDialog.getItem(
                        self,
                        "Unlink character name",
                        "Name to separate from this character:",
                        aliases,
                        0,
                        False,
                    )
                    if not accepted:
                        return
            if not self.voice_library.unlink_person(role):
                return
        except (OSError, VoiceLibraryError) as error:
            self.voice_confirmation_status.setText(f"Unable to unlink name: {error}")
            return
        self._voice_auditions_completed()

    def _return_to_story_selection(self) -> None:
        if self.has_pending_work():
            return
        if self._narrator_player is not None:
            self._narrator_player.stop()
        self._awaiting_voice_confirmation = False
        self._voice_plan = None
        self._generation_configuration = None
        self._prepared_voice_manifest = None
        self._prepared_voice_job = None
        self._generation_input = None
        self._changes_rows = ()
        self._refresh_narrator_status()
        self.voice_panel.hide()
        self.voice_confirmation.hide()
        self._set_progress_visible(False)
        self.content_scroll.hide()
        self.selection_panel.show()
        self.step.setText("Step 1 of 4 - Choose stories")
        self.continue_button.show()
        self.continue_button.setText("Continue")
        self.cancel_button.setText("Cancel")
        self._set_import_controls(True)
        self._selection_changed()

    def apply_narrator_settings(
        self, settings: AppSettings, *, voice_changed: bool = False
    ) -> None:
        if self.has_pending_work():
            return
        if not voice_changed and (
            settings.updated(last_main_section=self.settings.last_main_section)
            == self.settings
        ):
            self.settings = settings
            self._refresh_narrator_status()
            return
        return_to_confirmation = (
            self._awaiting_voice_confirmation and self._job is not None
        )
        self.settings = settings
        self.coverage_runner.cancel()
        self._checking_story = None
        self._story_audio_changed()
        with (
            QSignalBlocker(self.engine_choice),
            QSignalBlocker(self.model_choice),
            QSignalBlocker(self.pocket_voice_cloning),
        ):
            self.engine_choice.setCurrentIndex(
                self.engine_choice.findData(settings.offline_speech_backend)
            )
            self.model_choice.setText(settings.offline_tts_model or "")
            self.pocket_voice_cloning.setChecked(settings.pocket_gated_model_accepted)
        self.model_choice.setVisible(
            settings.offline_speech_backend in {"coqui-xtts", "moss-tts"}
        )
        self._voice_plan = None
        self._generation_configuration = None
        self._prepared_voice_manifest = None
        self._prepared_voice_job = None
        self._generation_input = None
        self._generation_result = None
        self._recovery_result = None
        self._pack_result = None
        self._changes_rows = ()
        self._awaiting_voice_confirmation = False
        self.voice_confirmation.hide()
        self._set_progress_visible(False)
        self.voice_panel.hide()
        self.selection_panel.show()
        self._set_import_controls(True)
        self.step.setText("Step 1 of 4 - Choose stories")
        self.continue_button.show()
        self.continue_button.setText("Continue")
        self.cancel_button.setText("Cancel")
        self._refresh_narrator_status()
        self._selection_changed()
        if return_to_confirmation:
            self.preparationRequested.emit()
            self._save_selection()

    def _engine_changed(self) -> None:
        backend = self.engine_choice.currentData()
        self.settings = self.settings.updated(
            offline_speech_backend=backend,
            offline_tts_model=None,
            offline_tts_profile="default" if backend == "pocket-tts" else "stable",
        )
        self.model_choice.clear()
        self.model_choice.setEnabled(backend in {"coqui-xtts", "moss-tts"})
        self.model_choice.setVisible(backend in {"coqui-xtts", "moss-tts"})
        self.pocket_voice_cloning.setVisible(backend == "pocket-tts")
        self.pocket_terms.setVisible(backend == "pocket-tts")
        self._voice_plan = None
        self._refresh_narrator_status()
        self._selection_changed()
        self.offlinePreferencesChanged.emit(self.settings)

    def _model_changed(self, model: str) -> None:
        self.settings = self.settings.updated(offline_tts_model=model.strip() or None)
        self._voice_plan = None
        self._refresh_narrator_status()

    def _generation_engine_available(self) -> bool:
        return (
            self.settings.offline_speech_backend != "coqui-xtts"
            or self.settings.xtts_terms_accepted
        ) and any(
            backend == self.settings.offline_speech_backend and available
            for _label, backend, available in speech_backend_options(
                self.settings.offline_speech_backend
            )
        )

    def _pocket_cloning_toggled(self, enabled: bool) -> None:
        selected_narrator = (
            self.narrator_choice.currentData()
            if self._awaiting_voice_confirmation
            else None
        )
        self.settings = self.settings.updated(pocket_gated_model_accepted=bool(enabled))
        self._refresh_narrator_status()
        if self._awaiting_voice_confirmation and self._voice_plan is not None:
            self._show_voice_confirmation(self._voice_plan)
            if not self._awaiting_voice_confirmation:
                return
            selected_index = self.narrator_choice.findData(selected_narrator)
            if selected_narrator is not None and selected_index >= 0:
                self.narrator_choice.setCurrentIndex(selected_index)
            elif selected_narrator is not None:
                self.voice_confirmation_status.setText(
                    "The previous narrator is unavailable with this cloning setting. "
                    "Choose another narrator before generation."
                )
        else:
            self._voice_plan = None

    def _refresh_narrator_status(self) -> None:
        settings, narrator = self._narrator_configuration_values()
        set_labeled_text(
            self.narrator_status,
            speech_configuration_rows(settings, narrator=narrator),
        )
        self.narrator_status.setToolTip(self._full_narrator_configuration())

    def _narrator_configuration_values(self) -> tuple[AppSettings, str | None]:
        settings = resolve_pregeneration_settings(self.settings)
        narrator = voice_binding_label(self.voice_library.binding("Narrator"))
        if self._awaiting_voice_confirmation and self._voice_plan is not None:
            plan = self._voice_plan
            if isinstance(plan.synthesis_backend, str):
                settings = settings.updated(
                    speech_backend=plan.synthesis_backend,
                    tts_model=plan.synthesis_model,
                )
            narrator = (
                self.narrator_choice.currentText()
                if self.narrator_choice.currentData() is not None
                else "Not selected for this preparation"
            )
        return settings, narrator

    def _narrator_configuration(self, *, compact: bool = False) -> str:
        settings, narrator = self._narrator_configuration_values()
        return speech_configuration_label(settings, narrator=narrator, compact=compact)

    def _update_confirmed_narrator(self) -> None:
        settings, narrator = self._narrator_configuration_values()
        self.confirmed_narrator.setText(
            "Narrator: " + speech_configuration_rows(settings, narrator=narrator)[0][1]
        )
        self.voice_engine_details.setText(
            engine_model_label(
                settings.speech_backend,
                settings.tts_model,
                pocket_cloning=settings.pocket_gated_model_accepted,
            )
        )

    def _full_narrator_configuration(self) -> str:
        return "Settings for this audio preparation\n" + self._narrator_configuration()

    def _full_generation_details(self) -> str:
        content = self.current_content()
        runtime = self.progress_runtime.toolTip() or self.progress_runtime.text()
        settings, narrator = (
            self._generation_configuration or self._narrator_configuration_values()
        )
        return (
            "Settings for this audio preparation\n"
            + speech_configuration_label(settings, narrator=narrator)
            + (
                f"\nSource story index: {content.story_index}\n"
                f"{plain_label_text(self.story_context)}"
                if content is not None
                else ""
            )
            + (f"\nRuntime: {runtime}" if runtime else "")
        )

    def _voice_choices(self, plan: VoicePlan) -> tuple[VoiceChoice, ...]:
        choices: list[VoiceChoice] = []
        backend = (
            plan.synthesis_backend
            if isinstance(plan.synthesis_backend, str)
            else resolve_pregeneration_settings(self.settings).speech_backend
        )
        if backend == "pocket-tts":
            choices.extend(
                VoiceChoice(
                    f"preset:{name}",
                    name.replace("_", " ").title(),
                    "Pocket TTS built-in voice",
                )
                for name in pocket_tts_preset_voices
            )
        if isinstance(plan.voice_manifest, (str, bytes)) and (
            backend != "pocket-tts" or self.settings.pocket_gated_model_accepted
        ):
            registry = CharacterVoiceRegistry.from_file(plan.voice_manifest)
            choices.extend(registry.choices())
        return tuple(choices)

    def _show_voice_confirmation(self, plan: VoicePlan) -> None:
        self.continue_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.step.setText("Step 2 of 4 - Choose and confirm voices")
        local_voice_controls = self.game_narrator_chooser is None
        show_terms = (
            local_voice_controls
            and self.settings.offline_speech_backend == "pocket-tts"
        )
        self.pocket_voice_cloning.setVisible(show_terms)
        self.pocket_terms.setVisible(show_terms)
        self.content_scroll.verticalScrollBar().setValue(0)
        try:
            choices = self._voice_choices(plan)
        except (OSError, ValueError) as error:
            self._awaiting_voice_confirmation = False
            self.voice_confirmation.hide()
            self._set_progress_visible(False)
            self.content_scroll.hide()
            self.selection_panel.show()
            self._set_import_controls(True)
            self.continue_button.setText("Retry voice matching")
            self._set_selection_status(f"Unable to show character voices: {error}")
            return
        self._awaiting_voice_confirmation = True
        self.continue_button.show()
        self.game_narrator_button.setEnabled(True)
        self.pocket_voice_cloning.setEnabled(True)
        self.selection_panel.hide()
        self.voice_panel.hide()
        self._set_progress_visible(False)
        self.voice_confirmation.show()
        self.work_summary.setTextFormat(Qt.TextFormat.RichText)
        self.work_summary.setText(self.summary.text())
        self.voice_configuration.setText(
            "Original game audio stays unchanged. Voice or model changes may require "
            "new recordings. Narrator changes are shared with future preparation and "
            "live speech."
            + (
                " After preparation, VNTTS activates saved audio for Reading if the "
                "current settings still match; playback does not start automatically."
                if self.automatic_activation
                else ""
            )
        )
        self._update_confirmed_narrator()
        self.voice_configuration.setToolTip(
            "These voices apply to generated lines in the selected stories. "
            "Matching saved work can be resumed. Other prepared stories remain unchanged."
        )
        self.narrator_choice.blockSignals(True)
        self.narrator_choice.clear()
        self.narrator_choice.addItem("Choose voice...", None)
        for choice in choices:
            self.narrator_choice.addItem(choice.label, choice.id)
            self.narrator_choice.setItemData(
                self.narrator_choice.count() - 1,
                choice.description,
                Qt.ItemDataRole.ToolTipRole,
            )
        current = pregeneration_narrator_source_id(
            self.settings, voice_library=self.voice_library
        )
        selected = self.narrator_choice.findData(current)
        self.narrator_choice.setCurrentIndex(max(0, selected))
        model = self.narrator_choice.model()
        if isinstance(model, QStandardItemModel):
            model.item(0).setEnabled(False)
        self.narrator_choice.blockSignals(False)
        self._render_voice_routes(plan)
        self.continue_button.setText("Generate with these voices")
        self.continue_button.setAccessibleDescription(
            "Generate the selected stories with the voices shown"
        )
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._narrator_choice_changed()

    def _render_voice_routes(self, plan: VoicePlan | None) -> None:
        previous = self.voice_routes.currentItem()
        selected_character = (
            previous.data(int(Qt.ItemDataRole.UserRole) + 1) if previous else None
        )
        self.voice_routes.clear()
        if plan is None:
            return
        groups = tuple(
            group
            for group in (plan.groups if isinstance(plan.groups, (tuple, list)) else ())
            if normalize_character_name(group.character) != "narrator"
        )
        suggested_roles = {
            normalize_character_name(group.routing_role or group.character)
            for group in groups
            if self._suggested_person_links(
                group.routing_role or group.character, plan=plan
            )
        }
        exceptions = [
            group
            for group in groups
            if (
                group.route != "voice"
                or normalize_character_name(group.routing_role or group.character)
                in suggested_roles
                or normalize_character_name(group.character)
                != normalize_character_name(
                    group.source_character or group.source_speaker or ""
                )
            )
        ]
        needs_review = sum(group.route == "needs-audition" for group in groups)
        self.voice_route_summary.setText(
            f"Optional voice review: {needs_review} assignment{'s' if needs_review != 1 else ''}. "
            "Continuing uses the narrator."
            if needs_review
            else ""
        )
        self.voice_route_summary.setVisible(bool(needs_review))
        summary_font = self.voice_route_summary.font()
        summary_font.setBold(bool(needs_review))
        self.voice_route_summary.setFont(summary_font)
        self.show_all_voice_routes.setVisible(bool(groups))
        visible = exceptions if self.show_all_voice_routes.isChecked() else groups
        show_portraits = bool(groups) and all(
            group.portrait_image and group.portrait_image_sha256 for group in groups
        )
        self.voice_routes_header_layout.setContentsMargins(
            8 + (self.voice_routes.iconSize().width() + 6 if show_portraits else 0),
            0,
            8,
            0,
        )
        self.voice_routes.setVisible(bool(visible))
        self.voice_routes_header.setVisible(bool(visible))
        self.voice_routes_empty.setText(
            "No substitutions or suggestions. Turn off Substitutions only to see all characters."
            if groups
            else "No character voices need to be chosen for these stories."
        )
        self.voice_routes_empty.setVisible(not visible)
        self.inspect_character_voice.setVisible(bool(visible))
        self.more_voice_actions_toggle.setVisible(bool(visible))
        self.more_voice_actions.setVisible(
            bool(visible) and self.more_voice_actions_toggle.isChecked()
        )
        for group in sorted(
            visible,
            key=lambda value: (
                value.route != "needs-audition",
                -len(value.line_ids),
                value.character.casefold(),
                value.group_id,
            ),
        ):
            item = self._voice_route_item(
                group,
                show_portraits=show_portraits,
                suggested_link=normalize_character_name(
                    group.routing_role or group.character
                )
                in suggested_roles,
            )
            self.voice_routes.addItem(item)
            self._set_voice_route_card(item)
            if item.data(int(Qt.ItemDataRole.UserRole) + 1) == selected_character:
                self.voice_routes.setCurrentItem(item)
        if self.voice_routes.currentItem() is None and self.voice_routes.count():
            self.voice_routes.setCurrentRow(0)
        self.voice_routes.resize_rows()
        self._show_voice_route_details(self.voice_routes.currentItem())
        self._update_voice_route_actions()

    def _voice_route_item(
        self, group: VoiceGroup, *, show_portraits: bool, suggested_link: bool
    ) -> QListWidgetItem:
        routing_role = group.routing_role or group.character
        display_role = (
            f"{routing_role} (same person as {group.character})"
            if normalize_character_name(routing_role)
            != normalize_character_name(group.character)
            else group.character
        )
        lines = len(group.line_ids)
        narrator_missing = self.narrator_choice.currentData() is None
        if group.route in {"needs-audition", "narrator"}:
            source = self.narrator_choice.currentText()
            route = (
                "Narrator required"
                if narrator_missing
                else source
                if group.character == "Narrator"
                else f"Narrator voice ({source})"
            )
        else:
            route = group.source_character or group.source_speaker or "No voice"
        candidate = next(
            (
                value
                for value in (*group.candidate_inventory, group.narrator_candidate)
                if value is not None and value.source_id == group.source_id
            ),
            None,
        )
        if route.startswith("Player candidate "):
            route = "Game voice"
            if candidate is not None and candidate.source_excerpts:
                route += f" ({candidate.source_excerpts[0][:55]})"
        status = self._voice_selection_label(group)
        attention = group.route == "needs-audition"
        prefix = (
            "REVIEW VOICE: "
            if attention
            else "POSSIBLE SAME PERSON: "
            if suggested_link
            else ""
        )
        explanation = (
            "Choose a narrator voice before generation"
            if group.route in {"narrator", "needs-audition"} and narrator_missing
            else _voice_resolution_label(group.resolution)
        )
        reference = self._voice_reference_details(group, candidate, explanation)
        reference_details = (
            f"{display_role}: {reference}" if reference else display_role
        )
        item = QListWidgetItem(
            f"{prefix}{display_role} -> {route} | {status} | {lines} "
            f"line{'s' if lines != 1 else ''}\n{reference_details}"
        )
        if prefix:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            action = (
                "Select this role and click Listen or change voice to choose a "
                "character voice. Continuing without a choice uses Narrator."
                if attention
                else "Optional: open More actions and choose Link same person."
            )
            item.setToolTip(action)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, item.text())
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, action)
        item.setData(Qt.ItemDataRole.UserRole, routing_role)
        item.setData(int(Qt.ItemDataRole.UserRole) + 1, group.group_id)
        item.setData(
            int(Qt.ItemDataRole.UserRole) + 2,
            (
                display_role,
                route,
                status,
                str(lines),
                explanation,
            ),
        )
        item.setData(int(Qt.ItemDataRole.UserRole) + 3, reference_details)
        if show_portraits and group.portrait_image and group.portrait_image_sha256:
            try:
                if sha256_file(group.portrait_image) == group.portrait_image_sha256:
                    pixmap = QPixmap(group.portrait_image)
                    if not pixmap.isNull():
                        item.setIcon(QIcon(pixmap))
            except OSError:
                pass
        return item

    def _voice_reference_details(
        self, group: VoiceGroup, candidate: VoiceCandidate | None, explanation: str
    ) -> str:
        if group.route in {"narrator", "needs-audition"}:
            if self.narrator_choice.currentData() is None:
                return ""
            if self.narrator_choice.currentData() != pregeneration_narrator_source_id(
                self.settings, voice_library=self.voice_library
            ):
                return ""
            candidate = group.narrator_candidate
            references = (
                len(candidate.reference_sha256s) if candidate is not None else 0
            )
        else:
            references = len(group.reference_sha256s)
        duration = (
            f", {candidate.reference_duration_seconds:.1f} s total"
            if candidate is not None
            and candidate.reference_duration_seconds is not None
            else ""
        )
        return (
            f"{references} reference recording{'s' if references != 1 else ''}{duration}. {explanation}".rstrip()
            if references
            else explanation
        )

    def _voice_selection_label(self, group: VoiceGroup) -> str:
        if group.route == "needs-audition":
            return "automatic (review optional)"
        if group.resolution == "saved-player-decision":
            return "chosen by you"
        if not group.resolution.startswith("saved-"):
            return "automatic"
        binding = self.voice_library.binding(group.character)
        if binding is None or not (
            voice_binding_source_id(binding) == group.source_id
            or (
                binding.source_sha256s
                and binding.source_sha256s == group.reference_sha256s
            )
        ):
            return "saved choice"
        method = binding.provenance.get("method")
        return (
            "automatic"
            if method == "automatic"
            else "chosen by you"
            if method == "manual"
            else "saved choice"
        )

    def _show_voice_route_details(
        self, item: QListWidgetItem | None, _previous: QListWidgetItem | None = None
    ) -> None:
        self.voice_route_details.setText(
            item.data(int(Qt.ItemDataRole.UserRole) + 3) if item is not None else ""
        )
        self.voice_route_details.setVisible(item is not None)

    def _set_voice_route_card(self, item: QListWidgetItem) -> None:
        role, voice, status, coverage, _explanation = item.data(
            int(Qt.ItemDataRole.UserRole) + 2
        )
        card = QWidget()
        card.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QGridLayout(card)
        layout.setContentsMargins(8, 6, 8, 6)
        for column, value in enumerate((role, voice, status, coverage)):
            label = QLabel(value)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            if column == 3:
                label.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            if column == 0:
                font = label.font()
                font.setBold(True)
                label.setFont(font)
            layout.addWidget(label, 0, column)
        for column, stretch in enumerate((3, 3, 2, 1)):
            layout.setColumnStretch(column, stretch)
        item.setSizeHint(card.sizeHint())
        summary = item.text()
        item.setData(Qt.ItemDataRole.AccessibleTextRole, summary)
        item.setToolTip(summary)
        item.setText("")
        self.voice_routes.setItemWidget(item, card)

    def _narrator_choice_changed(self, _index: int | None = None) -> None:
        source_id = self.narrator_choice.currentData()
        self.narrator_choice.setToolTip(self.narrator_choice.currentText())
        self._refresh_narrator_status()
        self._update_confirmed_narrator()
        if self._narrator_player is not None:
            self._narrator_player.stop()
        self.play_narrator_reference.setEnabled(
            isinstance(source_id, str) and source_id.startswith("character:")
        )
        if self._voice_plan is not None:
            self._render_voice_routes(self._voice_plan)
        needs_narrator = bool(
            self._voice_plan
            and isinstance(self._voice_plan.groups, (tuple, list))
            and any(
                group.route in {"narrator", "needs-audition"}
                for group in self._voice_plan.groups
            )
        )
        ready = source_id is not None or not needs_narrator
        changed = self._voice_plan_needs_update()
        if changed:
            set_labeled_text(
                self.change_summary,
                (
                    (
                        "Voice changes",
                        "Apply these changes to review character voices or start generation. Existing recordings stay unchanged.",
                    ),
                ),
            )
        else:
            set_labeled_text(self.change_summary, self._changes_rows)
        self.change_summary.setVisible(bool(self.change_summary.text()))
        self.continue_button.setText(
            "Apply voice changes" if changed else "Generate with these voices"
        )
        self.continue_button.setEnabled(ready)
        self._update_voice_route_actions()
        self.voice_confirmation_status.setText(
            "Choose a narrator before generation." if not ready else ""
        )

    def _voice_plan_needs_update(self) -> bool:
        source_id = self.narrator_choice.currentData()
        return (
            source_id is not None
            and source_id
            != pregeneration_narrator_source_id(
                self.settings, voice_library=self.voice_library
            )
        ) or (
            self._voice_plan is not None
            and self._voice_plan.synthesis_backend == "pocket-tts"
            and self._voice_plan.pocket_voice_cloning
            != self.settings.pocket_gated_model_accepted
        )

    def _play_narrator_reference(self) -> None:
        source_id = self.narrator_choice.currentData()
        if not (
            self._voice_plan is not None
            and self._voice_plan.voice_manifest
            and isinstance(source_id, str)
            and source_id.startswith("character:")
        ):
            self.voice_confirmation_status.setText(
                "Choose an imported narrator voice before listening."
            )
            return
        try:
            reference = load_original_reference(
                self._voice_plan.voice_manifest, source_id
            )
            if self._narrator_player is None:
                self._narrator_player = QtPcmPlayer(self)
                self._narrator_player.errorOccurred.connect(
                    lambda _code, message: self.voice_confirmation_status.setText(
                        f"Unable to play the original game voice: {message}"
                    )
                )
            self._narrator_player.stop()
            clip = self._narrator_player.play_bytes(
                reference.payload, source=str(reference.path)
            )
            if clip is None:
                return
            self.voice_confirmation_status.setText(
                f"Playing {reference.character}'s {reference.duration_seconds:.1f} s "
                "original reference."
            )
        except Exception as error:
            self.voice_confirmation_status.setText(
                f"Unable to play the original game voice: {error}"
            )

    def _confirm_voice_plan(self) -> None:
        if self._narrator_player is not None:
            self._narrator_player.stop()
        source_id = self.narrator_choice.currentData()
        current = pregeneration_narrator_source_id(
            self.settings, voice_library=self.voice_library
        )
        assert self._voice_plan is not None
        planned_cloning = self._voice_plan.pocket_voice_cloning
        controls_changed = (
            self._voice_plan.synthesis_backend == "pocket-tts"
            and isinstance(planned_cloning, bool)
            and (planned_cloning != self.settings.pocket_gated_model_accepted)
        )
        narrator_changed = source_id is not None and source_id != current
        if controls_changed or narrator_changed:
            if narrator_changed:
                rollback = self.voice_library.binding_rollback()
                registry = (
                    CharacterVoiceRegistry.from_file(self._voice_plan.voice_manifest)
                    if self._voice_plan.voice_manifest
                    else CharacterVoiceRegistry()
                )
                try:
                    remember_voice_binding(
                        self.voice_library,
                        registry,
                        "Narrator",
                        source_id,
                        method="manual",
                        evidence={"selected_in": "story-preparation"},
                        algorithm="story-preparation-v1",
                        rollback=rollback,
                    )
                except Exception as error:
                    self.voice_confirmation_status.setText(
                        f"Unable to save the narrator choice: {error}"
                    )
                    return
                self._provisional_binding_rollback = rollback
                self._refresh_narrator_status()
            self._awaiting_voice_confirmation = False
            self.pocket_voice_cloning.setEnabled(False)
            self.voice_confirmation.hide()
            self.planning_voices = True
            self.replanning_voice_decisions = False
            self._show_waiting_phase(
                "Applying narrator voice",
                "Applying your voice choices before generation...",
                "Cancel stops voice matching. Nothing has been generated yet.",
            )
            self.voice_runner.start(
                self._create_voice_plan,
                self._job,
                False,
            )
            return
        self._generation_configuration = self._narrator_configuration_values()
        self._awaiting_voice_confirmation = False
        self.pocket_voice_cloning.setEnabled(False)
        self.voice_confirmation.hide()
        if self._generation_input is None:
            self._generate_after_input = True
            self._start_generation_input(self._voice_plan)
            return
        self._start_generation()

    def _discover_content(self) -> ContentDiscovery:
        try:
            discovery = self.discovery()
        except (OSError, PregenerationSetupError) as error:
            discovery = ContentDiscovery((), (str(error),))
        if not isinstance(discovery, ContentDiscovery):
            raise TypeError("discovery must return ContentDiscovery")
        return discovery

    def _discover_content_and_check_updates(self) -> tuple[ContentDiscovery, bool]:
        discovery = self._discover_content()
        changed = (
            self.importer.availability().available
            and self.importer.installed_story_changed()
        )
        return discovery, changed

    def _refresh_stale_voice_job(
        self, job: PregenerationJob
    ) -> tuple[
        ContentDiscovery, GameContent | None, PregenerationJob | None, str | None
    ]:
        discovery = self._discover_content()
        source = Path(job.catalog_story_index).expanduser().resolve()
        content = next(
            (
                value
                for value in discovery.content
                if Path(value.story_index).expanduser().resolve() == source
            ),
            None,
        )
        if content is None:
            return (
                discovery,
                None,
                None,
                (
                    "The game story source changed and could not be found again. "
                    "Your voice choice was saved; select the story source and continue."
                ),
            )
        available = {selection.selection_id for selection in content.selections}
        missing = set(job.selected_story_ids) - available
        if missing:
            return (
                discovery,
                content,
                None,
                (
                    "The updated game content no longer contains every selected story. "
                    "Your voice choice was saved; review the story selection and continue."
                ),
            )
        try:
            if sha256_file(source) != content.story_index_sha256:
                raise StoryContentChanged("Game story content changed again")
            fresh_job = self.job_store.create_or_resume(content, job.selected_story_ids)
        except (OSError, PregenerationSetupError, StoryContentChanged) as error:
            return (
                discovery,
                content,
                None,
                (
                    f"Unable to refresh the selected stories: {error}. "
                    "Your voice choice was saved; refresh Stories and retry."
                ),
            )
        return discovery, content, fresh_job, None

    def _discovery_finished(self, discovery: object, error: Exception | None) -> None:
        if self._recovering_stale_job:
            self._recovering_stale_job = False
            self._stale_voice_job_finished(discovery, error)
            return
        changed = False
        if (
            isinstance(discovery, tuple)
            and len(discovery) == 2
            and isinstance(discovery[0], ContentDiscovery)
            and isinstance(discovery[1], bool)
        ):
            discovery, changed = discovery
        if error is None and not isinstance(discovery, ContentDiscovery):
            error = TypeError("Content discovery returned an invalid result")
        if error is not None:
            discovery = ContentDiscovery((), (str(error),))
        assert isinstance(discovery, ContentDiscovery)
        self._apply_discovery(discovery)
        self._set_discovery_loading(False)
        if self._background_discovery and changed:
            self._start_import(None, automatic=True)

    def _stale_voice_job_finished(
        self, result: object, error: Exception | None
    ) -> None:
        if self._close_after_voice_cancel:
            self.planning_voices = False
            self.reject()
            return
        if error is not None or not (
            isinstance(result, tuple)
            and len(result) == 4
            and isinstance(result[0], ContentDiscovery)
        ):
            reason = error or PregenerationVoiceError("Invalid story refresh result")
            self.planning_voices = False
            self._provisional_binding_rollback = None
            self._show_voice_plan_error(self._voice_plan_completion_error(None, reason))
            return
        discovery, content, job, reason = result
        if isinstance(content, GameContent) and isinstance(job, PregenerationJob):
            self._story_selection_drafts[content.story_index_sha256] = set(
                job.selected_story_ids
            )
        self._apply_discovery(discovery)
        if isinstance(content, GameContent):
            index = next(
                (
                    index
                    for index, value in enumerate(self._content)
                    if Path(value.story_index).expanduser().resolve()
                    == Path(content.story_index).expanduser().resolve()
                ),
                -1,
            )
            if index >= 0:
                self.source.setCurrentIndex(index)
        if not isinstance(job, PregenerationJob):
            self.planning_voices = False
            self._provisional_binding_rollback = None
            self._show_voice_plan_error(
                self._voice_plan_completion_error(
                    None,
                    PregenerationVoiceError(reason or "Unable to refresh game stories"),
                )
            )
            return
        self._job = job
        self._voice_plan = None
        self._generation_configuration = None
        self._prepared_voice_manifest = None
        self._prepared_voice_job = None
        self.step.setText("Step 2 of 4 - Choose and confirm voices")
        self._show_waiting_phase(
            "Matching updated game stories",
            "The game content changed while choosing voices. Reusing your saved "
            "voice choice with the updated stories...",
            "Cancel stops voice matching. Your saved voice choice remains available.",
        )
        self.voice_runner.start(
            self._create_voice_plan, job, self._pending_voice_rematch
        )

    def _set_discovery_loading(self, loading: bool) -> None:
        if loading:
            self.phaseChanged.emit("Finding installed stories")
        self.game_narrator_button.setEnabled(not loading)
        self.pocket_voice_cloning.setEnabled(not loading)
        self.discovery_panel.setVisible(loading)
        self.selection_panel.setVisible(not loading)
        self.content_scroll.setVisible(loading)
        self.selection_panel.setEnabled(not loading)
        self.continue_button.show()
        self.continue_button.setEnabled(
            not loading
            and bool(self.selected_story_ids())
            and self._generation_engine_available()
        )

    def _apply_discovery(self, discovery: ContentDiscovery) -> None:
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
            self.import_options_toggle.setChecked(False)
            self._update_import_options()
            self.source_status.setText(
                f"Found {len(self._content)} local game source(s)."
            )
            self.source_status.hide()
            self._source_changed(self.source.currentIndex())
        else:
            detail = f" {discovery.errors[0]}" if discovery.errors else ""
            self.source_status.setText(
                "Choose Find installed Reverse: 1999, or select its game folder. "
                "You can prepare stories while the game is closed." + detail
            )
            self.source_status.show()
            self._update_import_options()
            self._populate_stories(None)
        self.content_scroll.hide()
        self.selection_panel.show()

    def _update_import_options(self, _checked: bool | None = None) -> None:
        has_content = bool(self._content)
        expanded = not has_content or self.import_options_toggle.isChecked()
        self.import_options.setVisible(expanded)
        self.import_options_toggle.setVisible(has_content)
        self.import_options_toggle.setText(
            "Hide import options"
            if expanded and has_content
            else "Add or import content..."
        )

    def browse(self) -> None:
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
            self.source_status.show()
            return
        self._select_content(content, "Selected extracted game content is ready.")

    def _select_content(self, content: GameContent, status: str) -> None:
        self._prepared_voice_manifest = None
        self._prepared_voice_job = None
        previous = self.current_content()
        removed_selections: set[str] = set()
        for checksum, (draft_content, selected) in tuple(
            self._unsaved_story_selections.items()
        ):
            if draft_content.story_index == content.story_index:
                del self._unsaved_story_selections[checksum]
                self._story_selection_drafts[content.story_index_sha256] = set(
                    selected
                ) & {selection.selection_id for selection in content.selections}
        if (
            previous is not None
            and previous.story_index == content.story_index
            and previous.story_index_sha256 != content.story_index_sha256
        ):
            available = {selection.selection_id for selection in content.selections}
            removed_selections = set(self.selected_story_ids()) - available
            self._story_selection_drafts[content.story_index_sha256] = (
                set(self.selected_story_ids()) & available
            )
        self._content = tuple(
            value
            for value in self._content
            if value.story_index != content.story_index
            and value.story_index_sha256 != content.story_index_sha256
        ) + (content,)
        with QSignalBlocker(self.source):
            self.source.clear()
            for value in self._content:
                self.source.addItem(_content_label(value), value.story_index_sha256)
            self.source.setCurrentIndex(len(self._content) - 1)
        self._source_changed(self.source.currentIndex())
        if removed_selections:
            status += (
                f" {len(removed_selections)} previously selected stories are no "
                "longer available; review the selection before continuing."
            )
        self.source_status.setText(status)
        self.source_status.show()
        self.import_options_toggle.setChecked(False)
        self._update_import_options()

    def import_installed_game(self) -> None:
        self._stop_discovery_for_manual_source()
        self._start_import(None)

    def choose_game_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Choose the Reverse: 1999 installation folder",
        )
        if path:
            self._stop_discovery_for_manual_source()
            self._start_import(path)

    def _stop_discovery_for_manual_source(self) -> None:
        if self.discovery_runner.cancel():
            self._set_discovery_loading(False)

    def _start_import(
        self, installation_root: str | None, *, automatic: bool = False
    ) -> None:
        if self.importing:
            return
        availability = self.importer.availability()
        if not availability.available:
            self.source_status.setText(availability.message)
            self.source_status.show()
            return
        self.importing = True
        self._automatic_import = automatic
        if automatic:
            self.discovery_message.setText(
                "Game files changed. Updating the story list; previous recordings "
                "remain saved. You can cancel this update."
            )
            self._set_discovery_loading(True)
            self.discovery_import_button.setEnabled(False)
            self.discovery_game_folder_button.setEnabled(False)
        self.continue_button.hide()
        self.phaseChanged.emit("Importing installed game")
        self.import_cancel_event.clear()
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel import")
        self.cancel_button.setEnabled(True)
        self.source_status.setText(
            "Updating stories from the installed game..."
            if automatic
            else "Finding the installed game and importing story content..."
        )
        self.source_status.show()
        protected = tuple(
            path
            for path in (
                self._prepared_voice_manifest,
                self._voice_plan.voice_manifest if self._voice_plan else None,
                self.settings.voice_manifest,
            )
            if path
        )
        self.import_runner.start(
            self._import_and_prune,
            self.import_cancel_event,
            installation_root,
            protected,
        )

    def _import_and_prune(
        self,
        cancel_event: Event,
        installation_root: str | None,
        protected: tuple[str | Path, ...],
    ) -> GameContent:
        content = self.importer.import_installed(cancel_event, installation_root)
        if isinstance(self.importer.output_root, Path):
            prune_obsolete_voice_candidate_caches(
                self.importer.output_root / "reverse1999" / "voice-candidates",
                self.job_store.root,
                protected_paths=protected,
            )
        return content

    def current_content(self) -> GameContent | None:
        index = self.source.currentIndex()
        return self._content[index] if 0 <= index < len(self._content) else None

    def selected_story_ids(self) -> tuple[str, ...]:
        return tuple(
            self.stories.item(row).data(0, Qt.ItemDataRole.UserRole)
            for row in range(self.stories.count())
            if self.stories.item(row).checkState(0) == Qt.CheckState.Checked
        )

    def job(self) -> PregenerationJob | None:
        return self._job

    def voice_plan(self) -> VoicePlan | None:
        return self._voice_plan

    def generation_input(self) -> PregenerationInput | None:
        return self._generation_input

    def runtime_playback_settings(self) -> AppSettings | None:
        generation_input = self._generation_input
        if generation_input is None:
            return None
        manifest = runtime_progress_manifest_path(generation_input)
        if not manifest.is_file():
            return None
        return self.settings.updated(
            game_pack=None,
            story_index=str(generation_input.story_index),
            voice_manifest=str(generation_input.voice_manifest),
            live_sequence_plan=(
                str(self._prepared_sequence_plan)
                if self._prepared_sequence_plan is not None
                else None
            ),
            live_sequence_mode=(
                "audio-auto"
                if self._prepared_sequence_plan is not None
                and self.settings.auto_advance_enabled
                else "off"
            ),
            live_speaker_corpus=None,
            generated_audio_manifest=str(manifest),
            audio_source_policy="prefer-game-audio",
            speech_rate_percent=100,
            force_live_narrator=False,
        )

    def prioritize_line(self, line_id: str, text_sha256: str) -> bool:
        return self.recovery.prioritize_line(line_id, text_sha256)

    def _reading_line_observed(self, line_id: str, _text_sha256: str) -> None:
        if line_id == self._reading_line_id:
            return
        self._reading_line_id = line_id
        self._refresh_story_statuses()

    def generation_result(self) -> OfflineGenerationResult | None:
        return self._generation_result

    def recovery_result(self) -> OfflineRecoveryResult | None:
        return self._recovery_result

    def pack_result(self) -> OfflinePackResult | None:
        return self._pack_result

    def _set_progress_visible(self, visible: bool) -> None:
        self.progress_panel.setVisible(visible)
        self.progress_cancel_consequence.setVisible(
            visible and bool(self.progress_cancel_consequence.text())
        )
        if not visible:
            self.progress_change_selection.hide()
            self.play_ready_button.hide()
            self.cancel_button.setToolTip("")
            self.cancel_button.setAccessibleDescription("")

    def _set_progress_runtime(self, detail: str) -> None:
        self.progress_runtime.setText(compact_runtime_label(detail))
        self.progress_runtime.setToolTip(detail)
        self.progress_runtime.setVisible(bool(detail))

    def _show_phase(self, phase: str, detail: str, cancel_consequence: str) -> None:
        self._clear_resume_error()
        self.pocket_voice_cloning.hide()
        self.pocket_terms.hide()
        self.selection_panel.hide()
        self.content_scroll.show()
        self.content_scroll.verticalScrollBar().setValue(0)
        self._set_progress_visible(True)
        self.continue_button.setVisible(not self.has_pending_work())
        self.progress_phase.setText(phase)
        self.progress_change_selection.hide()
        self.progress_bar.show()
        settings, narrator = (
            self._generation_configuration or self._narrator_configuration_values()
        )
        set_labeled_text(
            self.progress_configuration,
            speech_configuration_rows(settings, narrator=narrator),
        )
        self.progress_configuration.setToolTip(
            speech_configuration_label(settings, narrator=narrator)
        )
        self._set_phase_detail(detail)
        self.progress_cancel_consequence.setText(cancel_consequence)
        self.progress_cancel_consequence.setVisible(bool(cancel_consequence))
        self.cancel_button.setToolTip(cancel_consequence)
        self.cancel_button.setAccessibleDescription(cancel_consequence)
        content = self.current_content()
        selected = (
            self._job.selected_story_ids
            if self._job is not None
            else self.selected_story_ids()
        )
        selections = [
            selection
            for selection in (content.selections if content else ())
            if selection.selection_id in selected
        ]
        story_count = len(
            {
                (selection.kind, selection.parent_story_id or selection.selection_id)
                for selection in selections
            }
        )
        scope = (
            f"{content.display_name} · {story_count} selected {'story' if story_count == 1 else 'stories'}"
            if content
            else ""
        )
        chapter_count = sum(
            selection.parent_story_id is not None for selection in selections
        )
        if chapter_count:
            scope += f" · {chapter_count} selected {'chapter' if chapter_count == 1 else 'chapters'}"
        self.progress_scope.setText(scope)
        self._refresh_story_statuses()
        self._emit_task_progress()

    def _set_selection_status(self, detail: str, *, visible: bool = True) -> None:
        self.selection_status.setText(detail)
        self.selection_status.setVisible(visible and bool(detail))

    def _set_phase_detail(self, detail: str) -> None:
        self.resume_status.setText(detail)
        self.resume_status.setVisible(bool(detail))

    def _emit_task_progress(self) -> None:
        self.phaseChanged.emit(
            ". ".join(
                value
                for value in (
                    self.progress_phase.text(),
                    plain_label_text(self.progress_counts),
                    self.resume_status.text() if not self.has_pending_work() else "",
                )
                if value
            )
        )

    def _preparation_paused(self, phase: str, error: Exception | str | None) -> None:
        self.progress_phase.setText(phase)
        if not self.has_pending_work():
            self.progress_bar.hide()
            self.cancel_button.setText("Close")
            self.cancel_button.setToolTip("")
            self.cancel_button.setAccessibleDescription("")
        self._refresh_story_statuses()
        self._selection_changed()
        if self._generation_configuration is not None and not self.has_pending_work():
            self.selection_panel.hide()
            self.continue_button.setText("Continue preparation")
            self.progress_change_selection.show()
        QTimer.singleShot(0, self, self._emit_task_progress)

    def _set_resume_error(self, prefix: str, error: Exception | str) -> None:
        self._resume_error_details = f"{prefix}: {error}"
        self._set_phase_detail(self._resume_error_details)
        self._set_selection_status(self._resume_error_details)
        self.copy_resume_error.show()
        from vntts.support import record_pregeneration_failure

        state_path = (
            runtime_progress_manifest_path(self._generation_input).with_name(
                "generation-state.json"
            )
            if isinstance(self._generation_input, PregenerationInput)
            else None
        )
        record_pregeneration_failure(
            prefix,
            error,
            job=self._job,
            generation_input=self._generation_input,
            voice_plan=self._voice_plan,
            state_path=state_path,
        )

    def _clear_resume_error(self) -> None:
        self._resume_error_details = ""
        self.copy_resume_error.hide()
        self.selection_status.hide()

    def _show_waiting_phase(
        self, phase: str, detail: str, cancel_consequence: str
    ) -> None:
        self._show_phase(phase, detail, cancel_consequence)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.progress_bar.setAccessibleName("Preparation progress")
        self.progress_counts.setAccessibleName("Preparation progress counts")
        self.progress_counts.clear()
        self.progress_counts.hide()
        self.progress_timing.clear()
        self.progress_timing.hide()
        self._set_progress_runtime("")
        self.progress_coverage.clear()
        self._render_progress_story_readiness()
        self._emit_task_progress()

    def _set_progress_counts(
        self,
        completed: int,
        total: int,
        generated: int = 0,
        failed: int = 0,
        other: int = 0,
    ) -> None:
        text = f"{completed} of {total} new speech lines processed"
        if failed or other:
            outcomes = [f"Prepared: {generated}"]
            if failed:
                outcomes.append(f"Failed: {failed}")
            if other:
                outcomes.append(f"Live fallback or omitted: {other}")
            text += "\n" + " · ".join(outcomes)
        self.progress_counts.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_counts.setText(text)
        self.progress_counts.show()

    def _set_progress_timing(self, value: str) -> None:
        self.progress_timing.setText(value)
        self.progress_timing.setVisible(bool(value))

    def _start_generation_progress(self) -> None:
        assert self._generation_input is not None
        if self._generation_configuration is None:
            self._generation_configuration = self._narrator_configuration_values()
        self._stop_generation_progress()
        self._progress_baseline = None
        self._progress_snapshot = None
        self._progress_changed_at = monotonic()
        self._progress_error = None
        self._set_progress_runtime("Generation device: waiting for the worker.")
        self._show_phase(
            "Generating offline audio",
            "",
            "Finished lines are saved. Reopen this window to continue only unfinished lines.",
        )
        cancel_detail = "Stop generation and close this window after the worker stops. Finished lines are saved; reopen to continue."
        self.cancel_button.setToolTip(cancel_detail)
        self.cancel_button.setAccessibleDescription(cancel_detail)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.progress_counts.clear()
        self.progress_counts.hide()
        self.progress_coverage.clear()
        self.play_ready_button.show()
        self.play_ready_button.setEnabled(False)
        self._poll_generation_progress()
        self.progress_timer.start()

    def _poll_generation_progress(self) -> None:
        if self._generation_input is None or not (self.generating or self.recovering):
            return
        self._refresh_progress_timing()
        if self.progress_runner.active:
            return
        inspect = getattr(self.generator, "inspect_progress", None)
        if not callable(inspect):
            return
        self.progress_runner.start(inspect, self._generation_input)

    def _progress_finished(self, progress: object, error: Exception | None) -> None:
        if not (self.generating or self.recovering) or self._close_after_voice_cancel:
            return
        if error is not None or not isinstance(progress, OfflineGenerationProgress):
            self._progress_error = str(error or "Invalid progress response")
            self._set_progress_runtime("Generation device: report unavailable.")
        else:
            self._progress_error = None
            runtime = (
                progress.runtime_status
                or "Generation device: waiting for the worker to confirm CPU/GPU use."
            )
            self._set_progress_runtime(runtime)
            now = monotonic()
            if progress != self._progress_snapshot:
                self._progress_changed_at = now
            self._progress_snapshot = progress
            if progress.available and self._progress_baseline is None:
                self._progress_baseline = (now, progress.completed)
            if progress.available:
                self._render_generation_progress(progress)
        self._refresh_progress_timing()

    def _refresh_progress_timing(self) -> None:
        assert self._generation_input is not None
        self.progress_timing.setToolTip(
            self._progress_error
            or "Time since a line or stage changed, not a process-health check. "
            "Model loading and long lines may take time. The estimate excludes final checks."
        )
        if self._progress_error:
            self._set_progress_timing(
                "Progress unavailable; generation may still be running. Retrying..."
            )
            return
        age = max(0, int(monotonic() - self._progress_changed_at))
        progress = self._progress_snapshot
        if progress is None or not progress.available:
            self._set_progress_timing(
                f"Waiting for progress ({age}s). Model loading may take time."
            )
            return
        estimate = ""
        if not self.recovering and self._progress_baseline is not None:
            started, baseline = self._progress_baseline
            completed = progress.completed - baseline
            remaining = max(0, self._generation_input.ready_items - progress.completed)
            if remaining == 0:
                estimate = "Generation processed; final checks may still be needed."
            elif completed >= 2:
                # ponytail: per-line average; weight by text length if ETA proves misleading.
                seconds = (monotonic() - started) * remaining / completed
                estimate = (
                    f"About {max(1, round(seconds / 60))} min of generation left."
                )
        self.progress_timing.setToolTip(
            f"Last progress change {age}s ago. " + self.progress_timing.toolTip()
        )
        self._set_progress_timing(
            f"Last progress change {age}s ago. {estimate}".rstrip()
            if age >= 30
            else estimate
        )

    def _stop_generation_progress(self) -> None:
        self.progress_timer.stop()
        self.progress_runner.cancel()
        self.progress_timing.clear()
        self.progress_timing.hide()
        self._set_progress_runtime("")

    def _render_generation_progress(self, progress: OfflineGenerationProgress) -> None:
        assert self._generation_input is not None
        total = self._generation_input.ready_items
        self.progress_bar.setAccessibleName("Durably completed generation items")
        self.progress_counts.setAccessibleName("Offline generation durable counts")
        completed = min(progress.completed, total)
        if self.generating:
            durable_phase = (
                {
                    "generating": "Generating offline audio",
                    "validating": "Checking generated audio",
                    "publishing": "Saving generated audio",
                }.get(progress.active_phase)
                if progress.active_phase is not None
                else None
            )
            if durable_phase is not None:
                self.progress_phase.setText(durable_phase)
        if self.recovering:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("")
        else:
            self.progress_bar.setRange(0, max(1, total))
            self.progress_bar.setValue(completed)
            self.progress_bar.setFormat("%p%")
        self._set_progress_counts(
            completed,
            total,
            progress.generated,
            progress.failed,
            progress.other_terminal,
        )
        ready = set(progress.ready_line_ids)
        selected = set(self._job.selected_story_ids) if self._job is not None else set()
        content = self.current_content()
        ready_count = sum(
            line_id in ready
            for selection in (content.selections if content is not None else ())
            if not selected or selection.selection_id in selected
            for line_id in selection.line_ids
        )
        self.play_ready_button.setEnabled(ready_count > 0)
        self._refresh_story_statuses()
        self._emit_task_progress()

    def _render_generation_result(self, result: OfflineGenerationResult) -> None:
        if self._generation_input is None:
            return
        self._render_generation_progress(
            OfflineGenerationProgress(
                result.generated,
                result.failed,
                result.other_terminal,
                ready_line_ids=(
                    self._progress_snapshot.ready_line_ids
                    if self._progress_snapshot is not None
                    else ()
                ),
            )
        )

    def _show_final_handoff(
        self,
        *,
        original: int,
        approved: int,
        live_fallbacks: int,
        story_lines: int,
        omissions: int,
    ) -> None:
        assert self._job is not None
        self.progress_timer.stop()
        self.step.setText("Step 4 of 4 - Activate and read")
        self.selection_panel.hide()
        self.voice_panel.hide()
        if self._generation_result is not None:
            self._render_generation_result(self._generation_result)
        self.progress_counts.clear()
        self.progress_counts.hide()
        self._set_progress_runtime("")
        self.progress_timing.clear()
        self.progress_timing.hide()
        self.play_ready_button.hide()
        self._show_phase(
            "Ready with live speech for remaining lines"
            if live_fallbacks
            else "Offline audio is ready",
            (
                "Your story audio is saved and validated. VNTTS is checking that "
                "Reading settings are unchanged before activating it. Playback will "
                "not start automatically."
                if self.automatic_activation
                else "Your story audio is saved, but not active yet. Click Use "
                "prepared audio. This also replaces live voice overrides for these "
                "story roles. Playback will not start automatically."
            ),
            "Close leaves the current audio setup unchanged; the saved pack can be "
            "activated by reopening this preparation later.",
        )
        self.progress_bar.hide()
        self._render_completed_story_readiness(live=live_fallbacks)
        coverage_rows = [
            ("Original game audio", f"{original} in this selection"),
            ("Prepared", f"{approved} in the saved pack"),
            (
                "Story lines",
                str(story_lines or self._job.estimate.selected_lines),
            ),
        ]
        if live_fallbacks:
            coverage_rows.insert(2, ("Live fallback", str(live_fallbacks)))
        if omissions:
            coverage_rows.append(("Omissions", f"{omissions} explicit"))
        set_labeled_text(self.progress_coverage, coverage_rows)
        self.continue_button.setText("Use prepared audio")
        self.continue_button.setAccessibleDescription(
            "Close this preparation and activate the saved offline audio pack"
        )
        self.continue_button.setEnabled(True)
        self.continue_button.setDefault(True)
        self.continue_button.setFocus()
        self.cancel_button.setText("Close")
        self.cancel_button.setEnabled(True)
        if self.automatic_activation:
            self.progress_phase.setText("Audio saved; connecting it to Reading")
            self.continue_button.hide()
            self.packReady.emit()

    def defer_activation(self, reason: str) -> None:
        self.progress_phase.setText("Audio saved; activation needs attention")
        self._set_phase_detail(
            reason + " Click Use prepared audio to activate the saved pack and replace "
            "live voice overrides for these story roles. Playback will not start automatically."
        )
        self.continue_button.show()
        self.continue_button.setEnabled(True)
        self._emit_task_progress()

    def _source_changed(self, _index: int) -> None:
        self.coverage_runner.cancel()
        self._checking_story = None
        self.step.setText("Step 1 of 4 - Choose stories")
        self._populate_stories(self.current_content())

    def _story_audio_changed(self, *_items: object) -> None:
        self.story_audio_status.setToolTip("")
        item = self.stories.current_story_item()
        self.check_story_audio.setEnabled(
            item is not None and not self.coverage_runner.active
        )
        self.story_audio_status.setVisible(item is not None)
        if item is not None:
            self.story_audio_status.setText(
                f"Story details: {item.toolTip(0)} "
                f"({item.data(0, Qt.ItemDataRole.UserRole + 5)} lines)\n"
                f"{item.text(1)}: {item.data(0, Qt.ItemDataRole.UserRole + 4)}\n"
                "Saved audio is checked automatically. You can also recheck this story."
            )
        if item is not None:
            checked = self._story_audio_checks.get(
                self._story_audio_key(item.data(0, Qt.ItemDataRole.UserRole))
            )
            if checked is not None:
                self._render_story_audio_check(*checked)

    def _story_audio_key(self, selection_id: str) -> StoryAudioKey:
        content = self.current_content()
        return (
            content.story_index_sha256 if content is not None else None,
            selection_id,
            self.settings.game_pack,
        )

    def _inspect_story_audio(
        self, content: GameContent, selection_id: str, active_manifest: Path | None
    ) -> tuple[StoryAudioCoverage | None, StoryAudioCoverage | None, tuple[str, ...]]:
        active = None
        if active_manifest:
            active = inspect_story_audio(
                content, selection_id, self.job_store, manifest=active_manifest
            )
        saved = (
            active
            if active is not None and not active.missing
            else inspect_story_audio(content, selection_id, self.job_store)
        )
        selection = next(
            value for value in content.selections if value.selection_id == selection_id
        )
        speakers = selection.playback_speakers
        return saved, active, speakers

    def _reading_override_reason(
        self, selection_id: str, coverage: StoryAudioCoverage
    ) -> str | None:
        settings = self.settings
        if settings.audio_source_policy == "live-tts-only":
            return "Reading uses live speech only. Choose a recording playback policy in Settings."
        if coverage.original and settings.audio_source_policy != "prefer-game-audio":
            return "This story relies on original game voices. Choose Original game audio in Settings."
        if coverage.generated and settings.speech_rate_percent != 100:
            return "Reading skips prepared recordings at this speed. Set speech speed to 100% in Settings."
        if settings.force_live_narrator and any(
            is_narrator(speaker)
            for speaker in self._story_playback_speakers.get(
                self._story_audio_key(selection_id), ()
            )
        ):
            return (
                "Narrator is forced to live speech. Disable that override in Voices "
                "to use prepared recordings."
            )
        return ""

    def _check_story_audio(self) -> None:
        item = self.stories.current_story_item()
        content = self.current_content()
        if item is None or content is None:
            return
        selection_id = item.data(0, Qt.ItemDataRole.UserRole)
        self._checking_story = self._story_audio_key(selection_id)
        self.check_story_audio.setEnabled(False)
        self.story_audio_status.setText(
            "Checking saved pack and recordings in the background..."
        )
        self.selection_panel.ensureWidgetVisible(self.story_audio_status)
        self.coverage_runner.start(
            self._inspect_story_audio,
            content,
            selection_id,
            self.settings.game_pack,
        )

    def _queue_story_checks(self) -> None:
        if (
            self.coverage_runner.active
            or self.has_pending_work()
            or self._awaiting_voice_confirmation
            or self._pack_result is not None
            or self.selection_panel.isHidden()
        ):
            return
        content = self.current_content()
        if content is None:
            return
        for selection_id in self.selected_story_ids():
            key = self._story_audio_key(selection_id)
            if key in self._story_audio_checks or not (
                self.settings.game_pack
                or self._story_job_statuses.get(selection_id) == "ready"
            ):
                continue
            self._checking_story = key
            self.check_story_audio.setEnabled(False)
            self.coverage_runner.start(
                self._inspect_story_audio,
                content,
                selection_id,
                self.settings.game_pack,
            )
            return

    def _story_audio_finished(self, result: object, error: Exception | None) -> None:
        self.check_story_audio.setEnabled(
            self.stories.current_story_item() is not None
            and not self.has_pending_work()
        )
        if self._checking_story is None:
            return
        key = self._checking_story
        if error is None and not _is_story_audio_result(result):
            error = TypeError("Story audio check returned an invalid result")
        if error is None:
            assert _is_story_audio_result(result)
            saved, active, speakers = result
        else:
            saved, active, speakers = None, None, ()
        self._story_playback_speakers[key] = speakers
        self._story_audio_checks[key] = (saved, active, error)
        self._checking_story = None
        self._refresh_story_statuses()
        self._selection_changed()
        item = self.stories.current_story_item()
        if (
            item is not None
            and self._story_audio_key(item.data(0, Qt.ItemDataRole.UserRole)) == key
        ):
            self._render_story_audio_check(saved, active, error)

    def _render_story_audio_check(
        self,
        coverage: StoryAudioCoverage | None,
        active: StoryAudioCoverage | None,
        error: Exception | str | None,
    ) -> None:
        self.story_audio_status.show()
        if error is not None:
            self.story_audio_status.setText(f"Audio needs attention: {error}")
            return
        if active is not None and not active.missing:
            coverage = active
        assert coverage is not None
        active_story = coverage is active
        item = self.stories.current_story_item()
        playback_warning = (
            self._reading_override_reason(
                item.data(0, Qt.ItemDataRole.UserRole), coverage
            )
            if active_story and item is not None
            else ""
        )
        state = (
            "Saved audio checked; Reading needs attention. " + playback_warning
            if playback_warning
            else "Preparation needed"
            if coverage.missing
            else "Ready with live speech for remaining lines"
            if coverage.live
            else "No live speech needed for indexed dialogue"
        )
        counts = "; ".join(
            f"{label}: {count}"
            for label, count in (
                ("Original game audio (indexed)", coverage.original),
                ("verified recordings", coverage.generated),
                ("live speech", coverage.live),
                ("omitted sounds", coverage.omitted),
                ("non-spoken", coverage.non_spoken),
                ("not prepared", coverage.missing),
            )
            if count
        )
        self.story_audio_status.setText(
            f"{coverage.title}: {state}.\n{counts or 'No indexed dialogue lines.'}\n"
            + (
                "Checked the active Reading pack."
                if active_story
                else "Checked saved audio; choose Use prepared audio to activate it."
                if coverage.manifest
                else "No saved preparation pack found for this story."
            )
            + (
                f" Reading currently has {active.generated} recordings, {active.original} original lines, "
                f"{active.live} live-speech lines and {active.missing} missing lines."
                if active is not None and not active_story
                else ""
            )
        )
        self.story_audio_status.setToolTip(str(coverage.manifest or ""))

    def _story_readiness_counts(
        self, line_ids: tuple[str, ...]
    ) -> tuple[int, int, int]:
        ready = set(getattr(self._progress_snapshot, "ready_line_ids", ()))
        ready_count = sum(line_id in ready for line_id in line_ids)
        ready_prefix = 0
        start = (
            line_ids.index(self._reading_line_id)
            if self._reading_line_id in line_ids
            else 0
        )
        for line_id in line_ids[start:]:
            if line_id not in ready:
                break
            ready_prefix += 1
        return ready_count, ready_prefix, start

    def _preparation_story_status(self, selection: StorySelection) -> tuple[str, str]:
        ready_count, ready_prefix, start = self._story_readiness_counts(
            selection.line_ids
        )
        remaining = selection.line_count - start
        if ready_prefix == selection.line_count:
            return "ready", "all lines are playable while other selections prepare"
        if start and ready_prefix == remaining:
            return "preparing", "all remaining lines are playable from the current line"
        if ready_count:
            return (
                "preparing",
                f"{ready_count}/{selection.line_count} lines ready; "
                f"{ready_prefix} consecutive from "
                f"{'current line' if start else 'selection start'}",
            )
        if self._progress_snapshot is not None:
            return (
                "preparing",
                f"0/{selection.line_count} lines ready; waiting for the first "
                f"consecutive line from {'current line' if start else 'selection start'}",
            )
        return "preparing", self.progress_phase.text()

    def _refresh_story_statuses(self) -> None:
        content = self.current_content()
        if content is None:
            return
        selections = {value.selection_id: value for value in content.selections}
        with QSignalBlocker(self.stories):
            for row in range(self.stories.count()):
                item = self.stories.item(row)
                selection_id = item.data(0, Qt.ItemDataRole.UserRole)
                selection = selections[selection_id]
                saved_status = self._story_job_statuses.get(selection_id)
                checked = self._story_audio_checks.get(
                    self._story_audio_key(selection_id)
                )
                status, detail, coverage = self._saved_story_status(
                    selection_id, selection, saved_status, checked
                )
                if (
                    self._job is not None
                    and self._job.catalog_story_index_sha256
                    == content.catalog_story_index_sha256
                    and selection_id in self._job.selected_story_ids
                    and self.has_pending_work()
                ):
                    status, detail = self._preparation_story_status(selection)
                label = {
                    "not_started": "Not prepared",
                    "preparing": "Preparing",
                    "in_progress": "Partially prepared",
                    "ready": "Ready",
                    "attention": "Needs attention",
                }[status]
                if (
                    status == "ready"
                    and checked is not None
                    and coverage is not None
                    and coverage.live
                ):
                    label = "Ready with live speech"
                item.setText(0, selection.title)
                if selection.parent_story_title is not None:
                    item.setText(
                        0,
                        selection.title.removeprefix(
                            f"{selection.parent_story_title} - "
                        ),
                    )
                item.setText(1, label)
                item.setToolTip(0, selection.title)
                item.setToolTip(1, detail)
                item.setData(0, Qt.ItemDataRole.UserRole + 2, status)
                item.setData(0, Qt.ItemDataRole.UserRole + 4, detail)
                item.setData(0, Qt.ItemDataRole.UserRole + 5, selection.line_count)
        self._filter_stories()
        if self.stories.current_story_item() is not None:
            self._story_audio_changed()
        self._render_progress_story_readiness()

    def _render_progress_story_readiness(self) -> None:
        content = self.current_content()
        if content is None:
            self._set_story_readiness_rows([])
            return
        selected = (
            set(self._job.selected_story_ids)
            if self._job is not None
            and self._job.catalog_story_index_sha256
            == content.catalog_story_index_sha256
            else set(self.selected_story_ids())
        )
        rows = []
        for selection in content.selections:
            if selection.selection_id not in selected:
                continue
            if self._job is not None and self._progress_snapshot is not None:
                _status, detail = self._preparation_story_status(selection)
                ready, consecutive, start = self._story_readiness_counts(
                    selection.line_ids
                )
                continuity = _readiness_continuity(
                    ready, selection.line_count, consecutive, start
                )
                rows.append(
                    (
                        selection,
                        f"{ready}/{selection.line_count}",
                        continuity,
                        detail,
                    )
                )
            else:
                item = next(
                    (
                        self.stories.item(row)
                        for row in range(self.stories.count())
                        if self.stories.item(row).data(0, Qt.ItemDataRole.UserRole)
                        == selection.selection_id
                    ),
                    None,
                )
                if item is not None:
                    rows.append(
                        (
                            selection,
                            "-",
                            "",
                            "Readiness has not been reported yet. "
                            + str(item.data(0, Qt.ItemDataRole.UserRole + 4)),
                        )
                    )
        self._set_story_readiness_rows(rows)

    def _set_story_readiness_rows(
        self,
        rows: list[tuple[StorySelection, str, str, str]],
        *,
        completed: bool = False,
    ) -> None:
        selections = tuple(row[0] for row in rows)
        if selections != self._progress_story_selections:
            self._build_readiness_story_rows(selections)
        groups: dict[tuple[str, str], list[StorySelection]] = {}
        for selection, ready, continuity, detail in rows:
            item = self._progress_story_items[selection.selection_id]
            title = selection.title
            if (
                selection.parent_story_id is not None
                and selection.parent_story_title is not None
            ):
                title = title.removeprefix(f"{selection.parent_story_title} - ")
                groups.setdefault(
                    (selection.kind, selection.parent_story_id), []
                ).append(selection)
            self._set_readiness_item(item, title, ready, continuity, detail)
        for key, chapters in groups.items():
            self._update_readiness_story_group(
                self._progress_story_groups[key], chapters, completed=completed
            )
        visible = bool(rows)
        self.progress_story_readiness_label.setVisible(visible)
        self.progress_story_readiness.setVisible(visible)

    def _build_readiness_story_rows(
        self, selections: tuple[StorySelection, ...]
    ) -> None:
        self.progress_story_readiness.clear()
        self._progress_story_items.clear()
        self._progress_story_groups.clear()
        for selection in selections:
            parent = None
            if (
                selection.parent_story_id is not None
                and selection.parent_story_title is not None
            ):
                key = (selection.kind, selection.parent_story_id)
                parent = self._progress_story_groups.get(key)
                if parent is None:
                    parent = QTreeWidgetItem(self.progress_story_readiness)
                    parent.setText(0, selection.parent_story_title)
                    font = parent.font(0)
                    font.setBold(True)
                    parent.setFont(0, font)
                    self._progress_story_groups[key] = parent
            item = QTreeWidgetItem(
                parent if parent is not None else self.progress_story_readiness
            )
            self._progress_story_items[selection.selection_id] = item
        self._progress_story_selections = selections
        self.progress_story_readiness.setRootIsDecorated(
            bool(self._progress_story_groups)
        )

    def _update_readiness_story_group(
        self, item: QTreeWidgetItem, chapters: list[StorySelection], *, completed: bool
    ) -> None:
        line_ids = tuple(
            dict.fromkeys(
                line_id for chapter in chapters for line_id in chapter.line_ids
            )
        )
        detail = "Readiness covers only the selected chapters. Expand to inspect each chapter."
        if completed or self._progress_snapshot is not None:
            ready, consecutive, start = self._story_readiness_counts(line_ids)
            if completed:
                ready = len(line_ids)
            count = f"{ready}/{len(line_ids)}"
            continuity = _readiness_continuity(
                ready, len(line_ids), consecutive, start, grouped=True
            )
        else:
            count, continuity = "-", ""
            detail += " Readiness has not been reported yet."
        self._set_readiness_item(item, item.text(0), count, continuity, detail)

    @staticmethod
    def _set_readiness_item(
        item: QTreeWidgetItem, title: str, ready: str, continuity: str, detail: str
    ) -> None:
        for column, value in enumerate((title, ready, continuity)):
            item.setText(column, value)
            item.setToolTip(column, title if column == 0 else detail)
            item.setData(column, Qt.ItemDataRole.AccessibleDescriptionRole, detail)

    def _render_completed_story_readiness(self, *, live: int) -> None:
        content = self.current_content()
        if content is None or self._job is None:
            return
        selected = set(self._job.selected_story_ids)
        suffix = "Ready with live speech" if live else "Ready in saved audio"
        self._set_story_readiness_rows(
            [
                (
                    selection,
                    f"{selection.line_count}/{selection.line_count}",
                    "All lines",
                    suffix,
                )
                for selection in content.selections
                if selection.selection_id in selected
            ],
            completed=True,
        )

    def _saved_story_status(
        self,
        selection_id: str,
        selection: StorySelection,
        saved_status: str | None,
        checked: StoryAudioCheck | None,
    ) -> tuple[str, str, StoryAudioCoverage | None]:
        status = "in_progress" if saved_status else "not_started"
        detail = (
            "checking saved audio"
            if saved_status == "ready"
            else "saved progress; continue to finish"
            if saved_status
            else f"{selection.generation_lines} lines need speech"
        )
        if checked is None:
            return status, detail, None
        coverage, active, error = checked
        if error is not None:
            return "attention", str(error), coverage
        if active is not None and not active.missing:
            coverage = active
        assert coverage is not None
        status = "in_progress" if coverage.missing else "ready"
        if coverage.missing and not (
            coverage.generated or coverage.original or saved_status
        ):
            status = "not_started"
        detail = ", ".join(
            f"{count} {label}"
            for label, count in (
                ("recordings", coverage.generated),
                ("original", coverage.original),
                ("live speech", coverage.live),
                ("missing", coverage.missing),
            )
            if count
        )
        if coverage is active:
            detail += "; active in Reading"
            warning = self._reading_override_reason(selection_id, coverage)
            if warning:
                return "attention", detail + "; " + warning, coverage
        elif coverage.manifest:
            detail += "; not active in Reading"
        return status, detail, coverage

    def _can_start_reading(self) -> bool:
        if self.change_voices.isChecked():
            return False
        selected = self.selected_story_ids()
        return bool(selected) and all(
            checked is not None
            and checked[2] is None
            and checked[1] is not None
            and not checked[1].missing
            and not self._reading_override_reason(selection_id, checked[1])
            for selection_id in selected
            for checked in (
                self._story_audio_checks.get(self._story_audio_key(selection_id)),
            )
        )

    def _saved_manifest_for_selection(self) -> Path | None:
        if self.change_voices.isChecked():
            return None
        manifests = set()
        for selection_id in self.selected_story_ids():
            checked = self._story_audio_checks.get(self._story_audio_key(selection_id))
            if (
                checked is None
                or checked[2] is not None
                or checked[0] is None
                or checked[0].missing
                or checked[0].manifest is None
            ):
                return None
            manifests.add(checked[0].manifest)
        return next(iter(manifests)) if len(manifests) == 1 else None

    def _load_saved_selection(
        self, content: GameContent, selected: tuple[str, ...], manifest: Path
    ) -> OfflinePackResult:
        result = load_saved_pack(manifest)
        for selection_id in selected:
            coverage = inspect_story_audio(
                content,
                selection_id,
                self.job_store,
                imported_pack=result.imported,
            )
            if coverage.missing:
                raise OfflinePackError(
                    "Saved audio no longer covers this selection. Prepare the missing stories again."
                )
        return result

    def _saved_pack_finished(self, result: object, error: Exception | None) -> None:
        self.activating_saved = False
        if self._close_after_voice_cancel:
            self.reject()
            return
        self._set_import_controls(True)
        if error is not None or not isinstance(result, OfflinePackResult):
            for selection_id in self.selected_story_ids():
                self._story_audio_checks[self._story_audio_key(selection_id)] = (
                    None,
                    None,
                    error or OfflinePackError("Invalid saved pack"),
                )
            self.selection_panel.show()
            self._set_resume_error(
                "Unable to use saved audio", error or "Invalid saved pack"
            )
            self._refresh_story_statuses()
            self._selection_changed()
            return
        self._pack_result = result
        self.accept()

    def _populate_stories(self, content: GameContent | None) -> None:
        self._story_audio_changed()
        self.stories.blockSignals(True)
        self.stories.clear()
        self._story_groups.clear()
        story_parents: dict[tuple[str, str], QTreeWidgetItem] = {}
        ordered_selections = (
            sorted(
                content.selections,
                key=lambda value: (_story_type_order(value.kind), value.order),
            )
            if content is not None
            else ()
        )
        if content is not None:
            resumed = self.job_store.latest_for_content(content)
            selection_error = None
            if content.story_index_sha256 not in self._story_selection_drafts:
                try:
                    saved = self.job_store.selection_for_content(content)
                except (OSError, ValueError, PregenerationSetupError) as error:
                    selection_error = error
                    saved = ()
                if saved is not None:
                    self._story_selection_drafts[content.story_index_sha256] = set(
                        saved
                    )
            selected_ids = self._story_selection_drafts.get(
                content.story_index_sha256,
                set(resumed.selected_story_ids) if resumed else set(),
            )
            self._story_selection_drafts[content.story_index_sha256] = set(selected_ids)
            story_statuses = self.job_store.story_statuses(content)
            self._story_job_statuses = story_statuses
            for selection in ordered_selections:
                group = self._story_groups.get(selection.kind)
                if group is None:
                    group = QTreeWidgetItem(self.stories)
                    group.setData(0, Qt.ItemDataRole.UserRole + 3, selection.kind)
                    group.setFlags(group.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    group.setCheckState(0, Qt.CheckState.Unchecked)
                    group_font = group.font(0)
                    group_font.setBold(True)
                    group.setFont(0, group_font)
                    group.setToolTip(
                        0,
                        "Expand or collapse this type. Its checkbox selects every story "
                        "in the type, including stories hidden by a filter.",
                    )
                    group.setExpanded(
                        self._story_group_expansion.get(
                            (content.story_index_sha256, selection.kind), True
                        )
                    )
                    self._story_groups[selection.kind] = group
                parent = group
                if (
                    selection.parent_story_id is not None
                    and selection.parent_story_title is not None
                ):
                    parent_key = (selection.kind, selection.parent_story_id)
                    story_parent = story_parents.get(parent_key)
                    if story_parent is None:
                        story_parent = QTreeWidgetItem(group)
                        story_parent.setText(0, selection.parent_story_title)
                        parent_font = story_parent.font(0)
                        parent_font.setBold(True)
                        story_parent.setFont(0, parent_font)
                        story_parent.setData(
                            0, Qt.ItemDataRole.UserRole + 3, selection.kind
                        )
                        story_parent.setData(
                            0,
                            Qt.ItemDataRole.UserRole + 6,
                            selection.parent_story_id,
                        )
                        story_parent.setFlags(
                            story_parent.flags() | Qt.ItemFlag.ItemIsUserCheckable
                        )
                        story_parent.setCheckState(0, Qt.CheckState.Unchecked)
                        story_parent.setExpanded(
                            self._story_group_expansion.get(
                                (
                                    content.story_index_sha256,
                                    f"story:{selection.kind}:{selection.parent_story_id}",
                                ),
                                False,
                            )
                        )
                        story_parent.setToolTip(
                            0,
                            "Expand or collapse this story. Its checkbox selects all "
                            "stages, including stages hidden by a filter.",
                        )
                        story_parents[parent_key] = story_parent
                    parent = story_parent
                item = QTreeWidgetItem()
                item.setText(
                    0,
                    selection.title.removeprefix(f"{selection.parent_story_title} - ")
                    if selection.parent_story_title is not None
                    else selection.title,
                )
                item.setData(0, Qt.ItemDataRole.UserRole, selection.selection_id)
                item.setData(
                    0,
                    Qt.ItemDataRole.UserRole + 1,
                    " ".join(
                        (
                            selection.title,
                            *selection.character_names,
                            *selection.playback_speakers,
                            *selection.episode_titles,
                        )
                    ).casefold(),
                )
                item.setData(0, Qt.ItemDataRole.UserRole + 3, selection.kind)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if selection.selection_id in selected_ids
                    else Qt.CheckState.Unchecked,
                )
                self.stories.add_story(parent, item)
            ready = sum(status == "ready" for status in story_statuses.values())
            message = (
                "Saved offline audio found. Story selection is remembered when you continue or close."
                if ready
                else "Previous selection restored. Continue resumes the same preparation."
                if resumed
                else "Your story selection is remembered when you continue or close."
            )
            if selection_error is not None:
                message = f"Unable to restore story selection: {selection_error}. Select stories again."
            self._set_selection_status(message, visible=selection_error is not None)
            self._set_phase_detail(message)
        else:
            self.selection_status.clear()
            self.resume_status.clear()
        self.stories.blockSignals(False)
        self._refresh_story_statuses()
        self._story_audio_changed()
        self._selection_changed()

    def _story_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        if not isinstance(item.data(0, Qt.ItemDataRole.UserRole), str):
            state = (
                Qt.CheckState.Unchecked
                if item.checkState(0) == Qt.CheckState.Unchecked
                else Qt.CheckState.Checked
            )
            with QSignalBlocker(self.stories):
                for child in self.stories.story_descendants(item):
                    child.setCheckState(0, state)
        self._selection_changed()

    def _story_group_expansion_changed(self, item: QTreeWidgetItem) -> None:
        content = self.current_content()
        if content is None:
            return
        kind = item.data(0, Qt.ItemDataRole.UserRole + 3)
        parent_id = item.data(0, Qt.ItemDataRole.UserRole + 6)
        if item.parent() is not None and not isinstance(parent_id, str):
            return
        expansion_key = (
            f"story:{kind}:{parent_id}" if isinstance(parent_id, str) else kind
        )
        self._story_group_expansion[(content.story_index_sha256, expansion_key)] = (
            item.isExpanded()
        )
        current = self.stories.current_story_item()
        if (
            not item.isExpanded()
            and current is not None
            and current in self.stories.story_descendants(item)
        ):
            self.stories.setCurrentItem(item)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        with QSignalBlocker(self.stories):
            for row in range(self.stories.count()):
                item = self.stories.item(row)
                if not item.isHidden():
                    item.setCheckState(0, state)
        self._selection_changed()

    def _filter_stories(self, _value: object | None = None) -> None:
        query = self.story_search.text().strip().casefold()
        status = self.story_filter.currentData()
        filtered = bool(query) or status is not None
        content = self.current_content()
        shown = selected = hidden_selected = 0
        current_was_hidden = False

        def update_group(
            item: QTreeWidgetItem,
            leaves: list[QTreeWidgetItem],
            expansion_key: str,
            unit: str,
        ) -> None:
            matches = sum(not leaf.isHidden() for leaf in leaves)
            checked_count = sum(
                leaf.checkState(0) == Qt.CheckState.Checked for leaf in leaves
            )
            ready = sum(
                leaf.data(0, Qt.ItemDataRole.UserRole + 2) == "ready" for leaf in leaves
            )
            item.setHidden(matches == 0)
            expanded = (
                self._story_group_expansion.get(
                    (content.story_index_sha256, expansion_key), item.parent() is None
                )
                if content is not None
                else True
            )
            item.setExpanded(True if filtered and matches else expanded)
            item.setCheckState(
                0,
                Qt.CheckState.Checked
                if checked_count == len(leaves)
                else Qt.CheckState.PartiallyChecked
                if checked_count
                else Qt.CheckState.Unchecked,
            )
            item.setText(1, f"{ready}/{len(leaves)} ready")
            item.setToolTip(
                1,
                f"{ready} of {len(leaves)} {unit} ready; {checked_count} selected"
                + (f"; {matches} match filters" if filtered else ""),
            )

        with QSignalBlocker(self.stories):
            for row in range(self.stories.count()):
                item = self.stories.item(row)
                visible = query in item.data(0, Qt.ItemDataRole.UserRole + 1) and (
                    status is None
                    or status == item.data(0, Qt.ItemDataRole.UserRole + 2)
                )
                item.setHidden(not visible)
                checked = item.checkState(0) == Qt.CheckState.Checked
                shown += visible
                selected += checked
                hidden_selected += checked and not visible
            for kind, group in self._story_groups.items():
                for row in range(group.childCount()):
                    child = group.child(row)
                    assert child is not None
                    parent_id = child.data(0, Qt.ItemDataRole.UserRole + 6)
                    if isinstance(parent_id, str):
                        update_group(
                            child,
                            self.stories.story_descendants(child),
                            f"story:{kind}:{parent_id}",
                            "stages",
                        )
                update_group(
                    group,
                    self.stories.story_descendants(group),
                    kind,
                    "selections",
                )
                group.setText(0, _story_type_label(kind))
            current = self.stories.current_story_item()
            ancestor = current
            while ancestor is not None and not ancestor.isHidden():
                ancestor = ancestor.parent()
            if current is not None and ancestor is not None:
                self.stories.setCurrentIndex(QModelIndex())
                current_was_hidden = True
        if current_was_hidden:
            self._story_audio_changed()
        self.story_filter_status.setText(
            (f"{shown}/{self.stories.count()} shown; " if filtered else "")
            + f"{selected} selected"
            + (f"; {hidden_selected} hidden by filters." if hidden_selected else ".")
            + (
                " Clear filters to see other stories."
                if not shown and self.stories.count()
                else ""
            )
        )
        self.select_all_button.setText("Select matches" if filtered else "Select all")
        self.select_none_button.setText("Clear matches" if filtered else "Select none")

    def _selection_changed(self, _item: object | None = None) -> None:
        self.continue_button.show()
        content = self.current_content()
        selected = self.selected_story_ids()
        if content is not None:
            self._remember_story_selection(content, selected)
        self._filter_stories()
        self._queue_story_checks()
        can_read = self._can_start_reading()
        saved_manifest = self._saved_manifest_for_selection()
        checking = (
            self._checking_story is not None and self._checking_story[1] in selected
        )
        self.prepare_again.setVisible(
            can_read
            or saved_manifest is not None
            or any(self._story_job_statuses.get(value) == "ready" for value in selected)
        )
        self.prepare_again.setEnabled(
            bool(selected)
            and not self.has_pending_work()
            and self._generation_engine_available()
        )
        if self._pack_result is None and not self._awaiting_voice_confirmation:
            self.continue_button.setText(
                self._selection_action_label(
                    selected, checking, can_read, saved_manifest
                )
            )
        if self._show_selection_blocker(
            content, selected, checking, can_read, saved_manifest
        ):
            return
        assert content is not None
        self._show_selection_summary(content, selected, can_read, saved_manifest)
        self.continue_button.setEnabled(True)

    def _remember_story_selection(
        self, content: GameContent, selected: tuple[str, ...]
    ) -> None:
        if set(selected) != self._story_selection_drafts.get(
            content.story_index_sha256
        ):
            self._unsaved_story_selections[content.story_index_sha256] = (
                content,
                selected,
            )
        self._story_selection_drafts[content.story_index_sha256] = set(selected)

    def _selection_action_label(
        self,
        selected: tuple[str, ...],
        checking: bool,
        can_read: bool,
        saved_manifest: Path | None,
    ) -> str:
        if checking:
            return "Checking saved audio..."
        if can_read:
            return "Start reading"
        if saved_manifest is not None:
            return "Use prepared audio"
        if any(
            self.stories.item(row).checkState(0) == Qt.CheckState.Checked
            and self.stories.item(row).data(0, Qt.ItemDataRole.UserRole + 2)
            != "not_started"
            for row in range(self.stories.count())
        ):
            return "Continue preparation"
        if not selected:
            return "Choose a story"
        return (
            "Prepare selected story"
            if len(selected) == 1
            else f"Prepare {len(selected)} stories"
        )

    def _show_selection_blocker(
        self,
        content: GameContent | None,
        selected: tuple[str, ...],
        checking: bool,
        can_read: bool,
        saved_manifest: Path | None,
    ) -> bool:
        if checking:
            self.summary.setText(
                "Checking saved recordings for the selected stories. No audio will be generated."
            )
            self.continue_button.setEnabled(False)
            return True
        if (
            not can_read
            and saved_manifest is None
            and not self._generation_engine_available()
        ):
            set_labeled_text(
                self.summary,
                (
                    (
                        "Preparation",
                        "Choose an available generation engine before preparing stories."
                        if self.settings.offline_speech_backend != "coqui-xtts"
                        or self.settings.xtts_terms_accepted
                        else "Accept the XTTS model terms in Settings before preparing stories.",
                    ),
                ),
            )
            self.continue_button.setEnabled(False)
            return True
        if content is None:
            set_labeled_text(
                self.story_context,
                (("Stories", "Select the stories you want to read."),),
            )
            self.story_context.setToolTip("")
            set_labeled_text(
                self.summary,
                (("Preparation", "Choose or import game content to continue."),),
            )
            self.continue_button.setEnabled(False)
            return True
        if not selected:
            set_labeled_text(self.story_context, (("Stories", "No stories selected."),))
            self.story_context.setToolTip("")
            set_labeled_text(
                self.summary,
                (("Preparation", "Select at least one story, stage, or chapter."),),
            )
            self.continue_button.setEnabled(False)
            return True
        return False

    def _show_selection_summary(
        self,
        content: GameContent,
        selected: tuple[str, ...],
        can_read: bool,
        saved_manifest: Path | None,
    ) -> None:
        estimate = estimate_preparation(content, selected)
        titles = [
            item.title for item in content.selections if item.selection_id in selected
        ]
        set_labeled_text(
            self.story_context,
            (
                ("Source", content.display_name),
                (
                    "Stories",
                    ", ".join(titles[:3])
                    + (f" and {len(titles) - 3} more" if len(titles) > 3 else ""),
                ),
            ),
        )
        self.story_context.setToolTip("\n".join(titles))
        disk_megabytes = max(1, round(estimate.estimated_disk_bytes / 1_000_000))
        summary_rows = [
            (
                "Selected",
                f"{len(selected)} stor{'y' if len(selected) == 1 else 'ies'}; "
                f"{estimate.selected_lines} dialogue lines",
            ),
            (
                "Audio",
                f"{estimate.original_audio_lines} original game lines; up to "
                f"{estimate.generation_lines} need speech across about "
                f"{estimate.speaker_count} voices",
            ),
            (
                "Storage",
                f"About {disk_megabytes} MB for the full selection; saved recordings can be reused",
            ),
        ]
        if self.change_voices.isChecked():
            summary_rows.append(
                (
                    "Voice re-match",
                    "Continuing once clears saved character choices for these "
                    "selected stories and runs automatic matching again. The "
                    "Narrator and characters used only by other stories stay unchanged. "
                    "A character shared with another story gets the same new default "
                    "there for future preparation; existing recordings stay unchanged.",
                )
            )
        if can_read:
            live_count = sum(
                cast(
                    StoryAudioCoverage,
                    self._story_audio_checks[self._story_audio_key(value)][1],
                ).live
                for value in selected
            )
            summary_rows = [
                (
                    "Ready",
                    "Selected stories are ready in the active Reading pack. "
                    + (
                        f"{live_count} lines use live speech; reading setup checks that voice. "
                        if live_count
                        else ""
                    )
                    + "Start reading to open the game reading setup.",
                )
            ]
        elif saved_manifest is not None:
            summary_rows = [
                (
                    "Ready",
                    "Use the saved recordings for these stories without generating them again. This restores the recorded voice routes for these roles. Reading setup will check any required live voice.",
                )
            ]
        for selection_id in selected:
            checked = self._story_audio_checks.get(self._story_audio_key(selection_id))
            if checked is not None and checked[2] is not None:
                summary_rows.append(
                    (
                        "Saved audio needs attention",
                        f"{checked[2]}. Prepare this story again to repair its audio.",
                    )
                )
                break
        if not self.prepare_again.isHidden():
            summary_rows.append(
                (
                    "Prepare again",
                    "Existing recordings keep their recorded voice and model. Current "
                    "defaults apply only to checked stories; matching recordings and "
                    "other stories are kept.",
                )
            )
        set_labeled_text(self.summary, summary_rows)
        self.prepare_again.setToolTip("Prepare again: " + ", ".join(titles))

    def select_voice_affected_stories(
        self, results: Iterable[StoryVoiceImpact]
    ) -> None:
        """Offer the normal scoped preparation flow; saving a default never generates."""
        if self.has_pending_work():
            return
        selected = {value.selection_id for value in results if value.changed_line_ids}
        with QSignalBlocker(self.stories):
            for row in range(self.stories.count()):
                item = self.stories.item(row)
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if item.data(0, Qt.ItemDataRole.UserRole) in selected
                    else Qt.CheckState.Unchecked,
                )
        self._selection_changed()
        self.prepare_again.setVisible(bool(selected))
        set_labeled_text(
            self.summary,
            (
                ("Affected stories", str(len(selected))),
                (
                    "Changed voice lines",
                    str(sum(len(value.changed_line_ids) for value in results)),
                ),
                (
                    "Next step",
                    "Choose Prepare selected stories again to review and apply these "
                    "defaults. Original recordings and existing audio stay playable "
                    "until preparation succeeds.",
                ),
            ),
        )

    def _prepare_again_requested(self) -> None:
        if self.has_pending_work() or not self._save_story_selection_drafts():
            return
        self.preparationRequested.emit()
        self._save_selection()

    def _continue_requested(self) -> None:
        if not self._save_story_selection_drafts():
            return
        if self.has_pending_work() or (
            self._checking_story is not None
            and self._checking_story[1] in self.selected_story_ids()
        ):
            return
        if self._pack_result is None and self._can_start_reading():
            self.readingRequested.emit()
            return
        self.preparationRequested.emit()
        manifest = self._saved_manifest_for_selection()
        if (
            manifest is not None
            and self._pack_result is None
            and not self._awaiting_voice_confirmation
        ):
            self.activating_saved = True
            self._close_after_voice_cancel = False
            self._set_import_controls(False)
            self._show_waiting_phase(
                "Checking saved audio",
                "Validating the saved pack before activation...",
                "Cancellation leaves the saved audio and current reading setup unchanged.",
            )
            self.cancel_button.setEnabled(True)
            self.saved_pack_runner.start(
                self._load_saved_selection,
                self.current_content(),
                self.selected_story_ids(),
                manifest,
            )
            return
        self._save_selection()

    def _save_story_selection_drafts(self) -> bool:
        for checksum, (content, selected) in tuple(
            self._unsaved_story_selections.items()
        ):
            try:
                self.job_store.save_selection(content, selected)
            except (OSError, ValueError, PregenerationSetupError) as error:
                self._set_selection_status(
                    f"Unable to save story selection: {error}. Check write access and retry."
                )
                return False
            del self._unsaved_story_selections[checksum]
        return True

    def _save_selection(self) -> None:
        if self._pack_result is not None:
            self.accept()
            return
        if self._awaiting_voice_confirmation:
            self._confirm_voice_plan()
            return
        if not self._generation_engine_available():
            self._selection_changed()
            return
        self._stale_job_retry_attempted = False
        self.coverage_runner.cancel()
        self._checking_story = None
        content = self.current_content()
        if content is None:
            return
        try:
            self._job = self.job_store.create_or_resume(
                content,
                self.selected_story_ids(),
            )
        except (OSError, PregenerationSetupError) as error:
            self._set_resume_error("Unable to save preparation", error)
            return
        self._generation_configuration = None
        self._story_job_statuses = self.job_store.story_statuses(content)
        for selection_id in self._job.selected_story_ids:
            self._story_audio_checks.pop(self._story_audio_key(selection_id), None)
        self.planning_voices = True
        self._pending_voice_rematch = self.change_voices.isChecked()
        self._semantic_preparation_enabled = (
            content.provider_id == self.importer.provider_id
            and content.game == self.importer.display_name
        )
        self.step.setText("Step 2 of 4 - Choose and confirm voices")
        self.replanning_voice_decisions = False
        self._close_after_voice_cancel = False
        self.voice_cancel_event.clear()
        self._set_import_controls(False)
        self.selection_panel.setVisible(False)
        self.cancel_button.setText("Cancel voice matching")
        self.cancel_button.setEnabled(True)
        self._show_waiting_phase(
            "Classifying selected game speech"
            if self._semantic_preparation_enabled
            else "Matching character voices",
            "Measuring and classifying selected game speech before matching voices. "
            "A pinned offline model may be installed once; this uses CPU."
            if self._semantic_preparation_enabled
            else "Preparing and matching character voices...",
            "Cancel stops voice matching and closes this window. Reopen it to "
            "reuse the saved story selection.",
        )
        self.voice_runner.start(
            self._create_voice_plan,
            self._job,
            self._pending_voice_rematch,
            self._semantic_preparation_enabled,
        )

    def _create_voice_plan(
        self,
        job: PregenerationJob,
        ignore_decisions: bool = False,
        semantic_enabled: bool = False,
    ) -> VoicePlan | _SemanticVoicePlan:
        started, cpu_started = monotonic(), process_time()
        settings = resolve_pregeneration_settings(self.settings)
        job, selected_content = self._prepare_selected_voice_job(job)
        if semantic_enabled or source_audio_semantics_need_migration(
            selected_content.story_index
        ):
            try:
                semantic_content = self.importer.prepare_source_audio_semantics(
                    job,
                    self.voice_cancel_event,
                    progress=self.decoderProgress.emit,
                    provision_model=True,
                    scan_progress=self.sourceAudioProgress.emit,
                )
            except GameContentImportCancelled as error:
                raise PregenerationVoiceCancelled(
                    "Source-audio semantic preparation was cancelled"
                ) from error
            if semantic_content is not None:
                selected_content = semantic_content
                job = self.job_store.create_or_resume(
                    semantic_content, job.selected_story_ids
                )
        self.decoderProgress.emit("Matching character voices...")
        self.voice_plan_store.validate_saved_voice_access(job, settings)
        if self._prepared_voice_job != job.job_id:
            self._prepared_voice_manifest = None
            self._prepared_voice_job = job.job_id
        manifest = self._prepared_voice_manifest
        if manifest is not None and not player_voice_catalog_is_current(manifest):
            manifest = None
            self._prepared_voice_manifest = None
        if manifest is None:
            try:
                manifest = self.importer.prepare_voice_candidates(
                    job,
                    self.voice_cancel_event,
                    progress=self.decoderProgress.emit,
                    scan_progress=self.sourceAudioProgress.emit,
                )
            except GameContentImportCancelled as error:
                raise PregenerationVoiceCancelled(
                    "Voice candidate preparation was cancelled"
                ) from error
            manifest = (
                manifest
                or self.settings.voice_manifest
                or find_default_voice_manifest()
            )
            self._prepared_voice_manifest = manifest
        record_background_operation(
            "pregeneration-voice-plan-candidate-preparation",
            (monotonic() - started) * 1000,
            "complete",
            cpu_ms=(process_time() - cpu_started) * 1000,
        )
        started, cpu_started = monotonic(), process_time()
        plan = self.voice_plan_store.create(
            job,
            settings,
            manifest_path=manifest,
            cancellation=self.voice_cancel_event,
            ignore_decisions=ignore_decisions,
        )
        record_background_operation(
            "pregeneration-voice-plan-store",
            (monotonic() - started) * 1000,
            "complete",
            cpu_ms=(process_time() - cpu_started) * 1000,
        )
        return _SemanticVoicePlan(plan, job, selected_content)

    def _prepare_selected_voice_job(
        self, job: PregenerationJob
    ) -> tuple[PregenerationJob, GameContent]:
        if self.voice_cancel_event.is_set():
            raise PregenerationVoiceCancelled("Voice matching was cancelled")
        content = self.job_store.selected_content(job)
        if self.voice_cancel_event.is_set():
            raise PregenerationVoiceCancelled("Voice matching was cancelled")
        if (
            content.story_index_sha256 != job.story_index_sha256
            or str(content.story_index) != job.story_index
        ):
            job = self.job_store.create_or_resume(content, job.selected_story_ids)
        return job, content

    def _decoder_progress(self, message: str) -> None:
        if self.planning_voices and not self._close_after_voice_cancel:
            self._set_phase_detail(message)
            if message == "Matching character voices...":
                self.progress_phase.setText("Matching character voices")
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("")
            self.progress_counts.clear()

    def _source_audio_progress(self, value: object) -> None:
        if (
            not isinstance(value, SourceAudioProgress)
            or not self.planning_voices
            or self._close_after_voice_cancel
        ):
            return
        phases = {
            "duration": ("Measuring selected game speech", "source-audio lines"),
            "semantics": ("Classifying selected game speech", "source-audio lines"),
            "references": ("Preparing character voices", "voice references"),
            "portraits": ("Finding character portraits", "game archives"),
        }
        phase, unit = phases[value.stage]
        self.progress_phase.setText(phase)
        self.progress_bar.setAccessibleName("Story scan progress")
        self.progress_counts.setAccessibleName("Story scan progress counts")
        self.progress_bar.setRange(0, max(1, value.total))
        self.progress_bar.setValue(value.processed)
        counts = f"{value.processed} of {value.total} {unit} checked"
        self.progress_bar.setFormat(counts)
        self.progress_counts.setText(counts)
        self._set_phase_detail(
            "Saving scanned story data..."
            if value.state == "publishing"
            else phase + "..."
        )
        self._emit_task_progress()

    def _voice_plan_finished(self, plan: object, error: Exception | None) -> None:
        self.planning_voices = False
        fast_choice = self._fast_voice_choice_pending
        self._fast_voice_choice_pending = False
        if self._recover_stale_voice_job(error):
            return
        if isinstance(plan, _SemanticVoicePlan):
            self._job = plan.job
            self._story_selection_drafts[plan.job.catalog_story_index_sha256] = set(
                plan.job.selected_story_ids
            )
            plan = plan.plan
        error = self._voice_plan_completion_error(plan, error)
        if self._show_voice_plan_error(error):
            return
        assert _is_voice_plan_result(plan)
        self._stale_job_retry_attempted = False
        self._voice_plan = plan
        self.replanning_voice_decisions = False
        if fast_choice:
            self._generation_input = None
            self._changes_rows = (
                (
                    "Voice choices",
                    "Saved. Existing audio will be checked when generation starts.",
                ),
            )
        if (
            plan.synthesis_backend != "pocket-tts"
            and any(group.route == "narrator" for group in plan.groups)
            and self.voice_library.binding("Narrator") is None
        ):
            self._changes_rows = ()
            self._show_voice_confirmation(plan)
            self.voice_confirmation_status.setText(
                "Choose a narrator in Voices before generation."
                if self.game_narrator_chooser is not None
                else "Choose a narrator before generation."
            )
            return
        if fast_choice:
            self._show_voice_confirmation(plan)
            return
        self._start_generation_input(plan)

    def _recover_stale_voice_job(self, error: Exception | None) -> bool:
        if (
            error is None
            or self._job is None
            or self._close_after_voice_cancel
            or self._stale_job_retry_attempted
            or not _story_index_was_replaced(error)
        ):
            return False
        self._stale_job_retry_attempted = True
        self._recovering_stale_job = True
        self.planning_voices = True
        self._set_import_controls(False)
        self._show_waiting_phase(
            "Updating changed game stories",
            "The installed game content changed while choosing voices. "
            "Refreshing the same story selection...",
            "Cancel stops voice matching. Saved voice choices remain available.",
        )
        self.discovery_runner.start(self._refresh_stale_voice_job, self._job)
        return True

    def _voice_plan_completion_error(
        self, plan: object, error: Exception | None
    ) -> Exception | None:
        if error is None and not _is_voice_plan_result(plan):
            error = TypeError("Voice matching returned an invalid voice plan")
        if error is not None or self._close_after_voice_cancel:
            restore_error = self._restore_provisional_bindings()
            if restore_error is not None:
                self._close_after_voice_cancel = False
                error = PregenerationVoiceError(
                    "Voice matching stopped, but the previous voice choices could "
                    f"not be restored: {restore_error}"
                )
        else:
            self._provisional_binding_rollback = None
            if self._pending_voice_rematch:
                with QSignalBlocker(self.change_voices):
                    self.change_voices.setChecked(False)
        self._pending_voice_rematch = False
        return error

    def _show_voice_plan_error(self, error: Exception | None) -> bool:
        if self._close_after_voice_cancel:
            self.reject()
            return True
        if error is None:
            return False
        self._awaiting_voice_confirmation = False
        self._refresh_narrator_status()
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_progress_visible(False)
        self.content_scroll.hide()
        self.selection_panel.setVisible(True)
        self.step.setText("Step 1 of 4 - Choose stories")
        self._set_import_controls(True)
        self._preparation_paused("Voice matching paused", error)
        self.progress_cancel_consequence.setText(
            "Change the selection or choose Continue to retry."
        )
        if isinstance(error, PregenerationVoiceCancelled):
            self._set_phase_detail("Voice matching cancelled.")
            return True
        self._set_resume_error("Unable to match character voices", error)
        if isinstance(error, DecoderSetupRequired) and confirm_decoder_setup(
            self, error
        ):
            self.importer.allow_decoder_homebrew = True
            self._save_selection()
        return True

    def _restore_provisional_bindings(self) -> Exception | None:
        rollback = self._provisional_binding_rollback
        self._provisional_binding_rollback = None
        if rollback is None:
            return None
        try:
            if not self.voice_library.rollback_bindings(rollback):
                return VoiceLibraryError(
                    "Voice bindings changed concurrently; rollback was skipped"
                )
            self._refresh_narrator_status()
        except Exception as error:
            return error
        return None

    def _voice_auditions_completed(self) -> None:
        fast_choice = (
            self.voice_panel.saved_inspection_choice
            if self.inspecting_voice_plan
            else None
        )
        self.auditioning_voices = False
        self.inspecting_voice_plan = False
        self.continue_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.replanning_voice_decisions = True
        self.planning_voices = True
        self.voice_confirmation.hide()
        self.cancel_button.setText("Cancel voice matching")
        self.cancel_button.setEnabled(True)
        if (
            fast_choice is not None
            and self._job is not None
            and self._voice_plan is not None
        ):
            self._fast_voice_choice_pending = True
            self._show_waiting_phase(
                "Applying selected voice",
                "Applying the selected character voice...",
                "The voice choice is saved. Reopen this story to continue if cancelled.",
            )
            self.voice_runner.start(
                self.voice_plan_store.apply_saved_choice,
                self._job,
                self._voice_plan,
                resolve_pregeneration_settings(self.settings),
                fast_choice,
            )
            return
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

    def _voice_auditions_cancelled(self) -> None:
        self.auditioning_voices = False
        if self.inspecting_voice_plan:
            self.inspecting_voice_plan = False
            self.cancel_button.setText("Cancel")
            self.cancel_button.setToolTip("")
            self.cancel_button.setEnabled(True)
            assert self._voice_plan is not None
            self._show_voice_confirmation(self._voice_plan)
            self.voice_confirmation_status.setText(
                "No new voice was saved. If none of the references fit, choose another "
                "voice or narrator fallback before continuing."
            )
            return
        if self._close_after_voice_cancel:
            self.reject()
            return
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        self.selection_panel.setVisible(True)
        self._set_selection_status("Voice selection cancelled.")
        self._set_phase_detail("Voice selection cancelled.")
        self._set_progress_visible(False)

    def _start_generation_input(self, plan: VoicePlan) -> None:
        self.preparing_inputs = True
        self._generation_input = None
        self._prepared_sequence_plan = None
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel preparation")
        self._show_waiting_phase(
            "Checking saved audio",
            "Checking which recordings can be reused. No speech is being generated.",
            "Cancel stops preparation and closes this window. Reopen it and choose "
            "Continue to resume from the saved selection and voice choices.",
        )
        self.input_runner.start(
            self._prepare_input_with_changes,
            self._job,
            plan,
        )

    def _prepare_input_with_changes(
        self, job: PregenerationJob, plan: VoicePlan
    ) -> tuple[
        PregenerationInput, OfflinePreparationChanges, GenerationResourceEstimate | None
    ]:
        started, cpu_started = monotonic(), process_time()
        prepared = self.input_store.materialize(
            job, plan, cancellation=self.voice_cancel_event
        )
        record_background_operation(
            "pregeneration-acceptance-materialize",
            (monotonic() - started) * 1000,
            "complete",
            cpu_ms=(process_time() - cpu_started) * 1000,
        )
        started, cpu_started = monotonic(), process_time()
        changes = self.publisher.inspect_changes(job, prepared, self.voice_cancel_event)
        record_background_operation(
            "pregeneration-acceptance-inspect-changes",
            (monotonic() - started) * 1000,
            "complete",
            cpu_ms=(process_time() - cpu_started) * 1000,
        )
        started, cpu_started = monotonic(), process_time()
        resources = (
            estimate_generation_resources(prepared)
            if isinstance(prepared, PregenerationInput)
            else None
        )
        record_background_operation(
            "pregeneration-acceptance-resource-estimate",
            (monotonic() - started) * 1000,
            "complete",
            cpu_ms=(process_time() - cpu_started) * 1000,
        )
        return prepared, changes, resources

    def _generation_input_finished(
        self, prepared: object, error: Exception | None
    ) -> None:
        generate_after_input = self._generate_after_input
        self._generate_after_input = False
        self.preparing_inputs = False
        if self._finish_preparation_ui():
            return
        if error is None and not _is_generation_input_result(prepared):
            error = TypeError("Offline preparation returned an invalid input")
        if self._recover_stale_voice_job(error):
            return
        if error is not None:
            self.selection_panel.setVisible(True)
            self._preparation_paused("Preparation paused", error)
            self.progress_cancel_consequence.setText(
                "Choose Continue to retry from the saved selection."
            )
            if isinstance(error, PregenerationQueueCancelled):
                if self._close_after_voice_cancel:
                    self.reject()
                else:
                    self._set_phase_detail("Offline preparation cancelled.")
                return
            self._set_resume_error("Unable to prepare generation", error)
            return
        assert _is_generation_input_result(prepared)
        self._generation_input, changes, resources = prepared
        self._prepared_original_audio_lines = changes.original
        self._start_prepared_sequence(self._generation_input)
        assert self._job is not None
        self._changes_rows = (
            (
                "Reuse",
                f"{changes.reused} saved recordings; {changes.original} original game lines",
            ),
            ("Process", f"{changes.new} new lines; {changes.failed} failed lines"),
            (
                "Existing live fallback",
                f"{changes.live_fallbacks} lines; {changes.omissions} omissions",
            ),
            (
                "On activation",
                f"{changes.replacement_candidates} recordings may be replaced; "
                f"{changes.preserved} in other stories stay."
                + (
                    " This switches to a different story pack."
                    if changes.switches_pack
                    else ""
                ),
            ),
        )
        if resources is not None:
            set_labeled_text(
                self.summary,
                (
                    ("Selected", f"{self._job.estimate.selected_lines} dialogue lines"),
                    (
                        "Remaining work",
                        f"{resources.remaining_items} lines; saved results are reused",
                    ),
                    (
                        "Additional WAV storage",
                        f"roughly {max(1, (resources.remaining_disk_bytes + 999_999) // 1_000_000)} MB; final publication also needs space for a copy of the pack"
                        if resources.remaining_items
                        else "No new speech to generate; final publication may still need temporary space",
                    ),
                    (
                        "Time",
                        "Estimated after generation starts; first-time model setup and final checks take additional time.",
                    ),
                ),
            )
        self.change_summary.setToolTip(
            "New work may hit the synthesis cache. Final fallback counts depend on generation. "
            "Replacement counts identify existing recordings not proven reusable with these choices."
        )
        self._set_import_controls(False)
        assert self._voice_plan is not None
        if generate_after_input:
            self._start_generation()
            return
        self._show_voice_confirmation(self._voice_plan)

    def _start_generation(self) -> None:
        self.step.setText("Step 3 of 4 - Generate and check audio")
        self.voice_panel.shutdown()
        self.generating = True
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel generation")
        self.cancel_button.setEnabled(True)
        self._start_generation_progress()
        self.generation_runner.start(
            self.recovery.generate_and_recover,
            self._generation_input,
            self._voice_plan,
            self.voice_cancel_event,
        )

    def _start_prepared_sequence(self, generation_input: PregenerationInput) -> None:
        """Best-effort plan preparation never delays audio generation."""
        if self._job is None or self._job.provider_id != self.importer.provider_id:
            return
        self.sequence_runner.start(
            _prepare_runtime_sequence,
            generation_input,
            self.voice_cancel_event,
        )

    def _prepared_sequence_finished(
        self, result: object, error: Exception | None
    ) -> None:
        if self._close_after_voice_cancel and not self.has_pending_work():
            self._finish_preparation_ui()
            return
        if error is not None or not isinstance(result, Path):
            return
        generation_input = self._generation_input
        if generation_input is None or result.parent != generation_input.directory:
            return
        self._prepared_sequence_plan = result

    def _generation_finished(self, result: object, error: Exception | None) -> None:
        self._stop_generation_progress()
        self.generating = False
        if self._finish_preparation_ui():
            return
        if error is None and not isinstance(
            result, (OfflineRecoveryResult, OfflineGenerationResult)
        ):
            error = TypeError("Offline generation returned an invalid result")
        if error is not None:
            self.progress_timer.stop()
            self.selection_panel.setVisible(True)
            self._preparation_paused("Generation paused", error)
            self._set_progress_timing(
                "Stopped. Counts show the last available progress report."
            )
            self.progress_cancel_consequence.setText(
                "Finished lines remain saved. Choose Continue to generate only "
                "unfinished lines, or Close to resume later."
            )
            if isinstance(error, OfflineGenerationCancelled):
                self._set_phase_detail(
                    "Generation cancelled. Continue later to resume saved lines."
                )
                return
            self._set_resume_error("Unable to generate offline audio", error)
            return
        if isinstance(result, OfflineRecoveryResult):
            self._recovery_finished(result, None)
            return
        assert isinstance(result, OfflineGenerationResult)
        self._generation_result = result
        self._render_generation_result(result)
        if result.failed < 1:
            self.progress_timer.stop()
            self._start_publication(result)
            return
        self.recovering = True
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel automatic recovery")
        self.cancel_button.setEnabled(True)
        self._show_waiting_phase(
            "Recovering failed lines",
            "",
            "Cancel stops recovery and closes this window. Finished lines remain "
            "saved; reopen and choose Continue to retry unfinished lines.",
        )
        self.progress_timer.start()
        self.recovery_runner.start(
            self.recovery.recover,
            self._generation_input,
            self._voice_plan,
            result,
            self.voice_cancel_event,
        )

    def _recovery_finished(self, result: object, error: Exception | None) -> None:
        self._stop_generation_progress()
        self.recovering = False
        self.progress_timer.stop()
        if self._finish_preparation_ui():
            return
        if error is None and not isinstance(result, OfflineRecoveryResult):
            error = TypeError("Automatic recovery returned an invalid result")
        if error is not None:
            self.selection_panel.setVisible(True)
            self._preparation_paused("Automatic recovery paused", error)
            self.progress_cancel_consequence.setText(
                "Finished lines remain saved. Choose Continue to retry unfinished "
                "lines, or Close to resume later."
            )
            if isinstance(error, OfflineGenerationCancelled):
                self._set_phase_detail(
                    "Automatic recovery cancelled. Continue later to resume saved lines."
                )
                return
            self._set_resume_error("Unable to recover offline audio", error)
            return
        assert isinstance(result, OfflineRecoveryResult)
        assert self._generation_input is not None
        self._recovery_result = result
        self._generation_result = result.generation
        self._render_generation_result(result.generation)
        if result.remaining_failed:
            ready = max(0, self._generation_input.ready_items - result.remaining_failed)
            self.progress_bar.setValue(ready)
            self.progress_bar.setFormat(
                f"{ready} of {self._generation_input.ready_items} ready"
            )
            self.selection_panel.setVisible(True)
            self._preparation_paused(
                "Automatic recovery paused",
                "Some lines still need safe automatic repair. Continue preparation to retry.",
            )
            self.progress_cancel_consequence.setText(
                "Prepared lines remain saved. Choose Continue to retry only the "
                "unfinished lines, or Close to resume later."
            )
            self._set_phase_detail(
                f"Offline audio is not complete: {result.remaining_failed} line"
                f"{'s' if result.remaining_failed != 1 else ''} still need a safe "
                "automatic repair."
            )
            return
        self._start_publication(result.generation)

    def _start_publication(self, generation_result: OfflineGenerationResult) -> None:
        self.publishing_pack = True
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel final save")
        self.cancel_button.setEnabled(True)
        self._show_waiting_phase(
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
            self._prepared_sequence_plan,
        )

    def _publication_finished(self, result: object, error: Exception | None) -> None:
        self.publishing_pack = False
        if self._finish_preparation_ui():
            return
        if error is None and not isinstance(result, OfflinePackResult):
            error = TypeError("Offline pack save returned an invalid result")
        if error is not None:
            self.selection_panel.setVisible(True)
            self._preparation_paused("Final save paused", error)
            self.progress_cancel_consequence.setText(
                "Prepared lines remain saved. Choose Continue preparation to retry "
                "the final save, or Close to resume later."
            )
            if isinstance(error, OfflineGenerationCancelled):
                self._set_phase_detail(
                    "Final save cancelled. Continue later to reuse prepared lines."
                )
                return
            self._set_resume_error("Unable to create offline game pack", error)
            return
        assert isinstance(result, OfflinePackResult)
        self._pack_result = result
        status_error = None
        try:
            assert self._job is not None
            self._job = self.job_store.mark_prepared(self._job)
            self._populate_stories(self.current_content())
        except (OSError, PregenerationSetupError) as error:
            status_error = error
        self._show_final_handoff(
            original=self._prepared_original_audio_lines,
            approved=result.approved,
            live_fallbacks=result.live_fallbacks,
            story_lines=result.story_lines,
            omissions=result.omissions,
        )
        if status_error is not None:
            self._set_phase_detail(
                "Offline audio was saved, but its story status could not be updated: "
                f"{status_error}"
            )

    def _import_finished(self, content: object, error: Exception | None) -> None:
        self.importing = False
        was_automatic = self._automatic_import
        if was_automatic:
            self._automatic_import = False
            self._set_discovery_loading(False)
            available = self.importer.availability().available
            self.discovery_import_button.setEnabled(available)
            self.discovery_game_folder_button.setEnabled(available)
        if self._close_after_voice_cancel:
            self.source_status.setText("Game import cancelled.")
            self.source_status.show()
            self.reject()
            return
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if error is None and not isinstance(content, GameContent):
            error = TypeError("Game import returned invalid content")
        if error is not None:
            self.source_status.setText(
                "Game import cancelled."
                if isinstance(error, GameContentImportCancelled)
                else f"Unable to import the installed game: {error}"
            )
            self.source_status.show()
            return
        assert isinstance(content, GameContent)
        self._select_content(
            content,
            "Updated stories from the installed game."
            if was_automatic
            else "Installed game content imported successfully.",
        )

    def _set_import_controls(self, enabled: bool) -> None:
        self._refresh_story_statuses()
        if enabled and not self.selection_panel.isHidden():
            show_terms = (
                self.game_narrator_chooser is None
                and self.settings.offline_speech_backend == "pocket-tts"
            )
            self.pocket_voice_cloning.setVisible(show_terms)
            self.pocket_terms.setVisible(show_terms)
        self.engine_choice.setEnabled(enabled)
        self.model_choice.setEnabled(
            enabled
            and self.settings.offline_speech_backend in {"coqui-xtts", "moss-tts"}
        )
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
        self.check_story_audio.setEnabled(
            enabled
            and self.stories.current_story_item() is not None
            and not self.coverage_runner.active
        )
        self.story_search.setEnabled(enabled)
        self.story_filter.setEnabled(enabled)
        self.expand_stories_button.setEnabled(enabled)
        self.collapse_stories_button.setEnabled(enabled)
        self.select_all_button.setEnabled(enabled)
        self.select_none_button.setEnabled(enabled)
        self.change_voices.setEnabled(enabled)
        self.prepare_again.setEnabled(
            enabled
            and bool(self.selected_story_ids())
            and self._generation_engine_available()
        )
        self.game_narrator_button.setEnabled(enabled)
        self.pocket_voice_cloning.setEnabled(enabled)
        self.continue_button.setEnabled(
            enabled
            and bool(self.selected_story_ids())
            and (self._can_start_reading() or self._generation_engine_available())
        )
        if (
            enabled
            and self._pack_result is None
            and not self._awaiting_voice_confirmation
        ):
            self._selection_changed()

    def _pending_preparation_stage(
        self, *, include_sequence: bool = False
    ) -> str | None:
        for active, label in (
            (self.recovering, "automatic recovery"),
            (self.publishing_pack, "final save"),
            (self.generating, "generation"),
            (self.preparing_inputs, "offline preparation"),
            (
                include_sequence and self.sequence_runner.active,
                "playback sequence preparation",
            ),
            (self.auditioning_voices, "voice selection"),
            (self.planning_voices, "voice matching"),
        ):
            if active:
                return label
        return None

    def _finish_preparation_ui(self) -> bool:
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if self._close_after_voice_cancel:
            self.reject()
            return True
        return False

    def _cancel_or_reject(self) -> None:
        self._stop_generation_progress()
        if self.activating_saved:
            self.saved_pack_runner.cancel()
            self.activating_saved = False
            self.reject()
            return
        if self.auditioning_voices and self.inspecting_voice_plan:
            self.cancel_button.setEnabled(False)
            self.voice_panel.cancel()
            return
        stage = self._pending_preparation_stage(include_sequence=True)
        if stage is not None:
            self._close_after_voice_cancel = True
            self.voice_cancel_event.set()
            if self.auditioning_voices:
                self.voice_panel.cancel()
            self.cancel_button.setEnabled(False)
            self.play_ready_button.setEnabled(False)
            self.progress_phase.setText(f"Cancelling {stage}...")
            self._set_phase_detail("")
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("")
            self._emit_task_progress()
            return
        if not self.importing:
            self.reject()
            return
        self.import_cancel_event.set()
        self._close_after_voice_cancel = True
        self.cancel_button.setEnabled(False)
        self.source_status.setText("Cancelling game import...")
        self.source_status.show()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_generation_progress()
        if self.has_pending_work() or self.sequence_runner.active:
            self._cancel_or_reject()
            event.ignore()
            return
        if not self._confirm_selection_close():
            event.ignore()
            return
        self.import_runner.cancel()
        self.coverage_runner.cancel()
        self.saved_pack_runner.cancel()
        self.discovery_runner.cancel()
        self.voice_runner.cancel()
        self.input_runner.cancel()
        self.sequence_runner.cancel()
        self.generation_runner.cancel()
        self.recovery_runner.cancel()
        self.publication_runner.cancel()
        self.progress_timer.stop()
        if self._narrator_player is not None:
            self._narrator_player.stop()
        self.voice_panel.shutdown()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        if self._narrator_player is not None:
            self._narrator_player.stop()
        if self.has_pending_work():
            self._cancel_or_reject()
            return
        self._close_after_voice_cancel = False
        if not self._confirm_selection_close():
            return
        self._stop_generation_progress()
        self.discovery_runner.cancel()
        self.coverage_runner.cancel()
        self.saved_pack_runner.cancel()
        self.progress_timer.stop()
        if not self.voice_panel.active:
            self.voice_panel.shutdown()
        super().done(result)

    def _confirm_selection_close(self) -> bool:
        if not self._save_story_selection_drafts():
            choice = QMessageBox.question(
                self,
                "Story selection was not saved",
                self.selection_status.text()
                + "\nDiscard these checkbox changes and close? Existing recordings and preparation progress are unchanged.",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Discard:
                return False
            self._unsaved_story_selections.clear()
        return True

    def has_pending_work(self) -> bool:
        return (
            self.importing
            or self.activating_saved
            or self._pending_preparation_stage() is not None
        )


def _readiness_continuity(
    ready: int, total: int, consecutive: int, start: int, *, grouped: bool = False
) -> str:
    if ready == total:
        return "All selected lines" if grouped else "All lines"
    location = "current line" if start else "selected start" if grouped else "start"
    return f"{consecutive} from {location}" if consecutive else f"Waiting at {location}"


def _content_label(content: GameContent) -> str:
    count = len(
        {
            (selection.kind, selection.parent_story_id or selection.selection_id)
            for selection in content.selections
        }
    )
    return f"{content.display_name} - {count} {'story' if count == 1 else 'stories'}"


def _story_type_order(kind: str) -> int:
    return {
        "main_story": 0,
        "chapter": 0,
        "anecdote": 1,
        "character_story": 2,
    }.get(kind.replace("-", "_"), 3)


def _story_type_label(kind: str) -> str:
    return {
        "main_story": "Main story",
        "chapter": "Chapters",
        "anecdote": "Anecdotes",
        "character_story": "Character stories",
    }.get(kind.replace("-", "_"), "Other stories")


def _voice_resolution_label(resolution: str) -> str:
    return {
        "ambiguous-voice-evidence": "Several plausible game voices; choose one to replace Narrator fallback",
        "automatic-narrator-fallback": "No usable character voice; using narrator",
        "linked-reference-needs-preview": "Only a reference under another name is available; preview it before use",
    }.get(resolution, "")


def _story_index_was_replaced(error: Exception) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, StoryContentChanged):
            return True
        current = current.__cause__
    return False


__all__ = ["OfflineAudioPreparationDialog"]


def _is_story_audio_result(
    value: object,
) -> TypeGuard[
    tuple[StoryAudioCoverage | None, StoryAudioCoverage | None, tuple[str, ...]]
]:
    return (
        isinstance(value, tuple)
        and len(value) == 3
        and any(isinstance(item, StoryAudioCoverage) for item in value[:2])
        and all(
            item is None or isinstance(item, StoryAudioCoverage) for item in value[:2]
        )
        and isinstance(value[2], tuple)
        and all(isinstance(speaker, str) for speaker in value[2])
    )


def _is_generation_input_result(
    value: object,
) -> TypeGuard[
    tuple[
        PregenerationInput,
        OfflinePreparationChanges,
        GenerationResourceEstimate | None,
    ]
]:
    return (
        isinstance(value, tuple)
        and len(value) == 3
        and isinstance(value[0], PregenerationInput)
        and isinstance(value[1], OfflinePreparationChanges)
        and (value[2] is None or isinstance(value[2], GenerationResourceEstimate))
    )


def _is_voice_plan_result(value: object) -> TypeGuard[VoicePlan]:
    return isinstance(value, VoicePlan)
