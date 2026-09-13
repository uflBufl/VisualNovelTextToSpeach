"""Guided player UI for selecting content to prepare for offline speech."""

from threading import Event
from time import monotonic

from PySide6.QtCore import QSignalBlocker, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.file_integrity import sha256_file

from vntts.application_directories import get_local_data_directory
from vntts.async_ui import LatestTaskRunner
from vntts.game_audio_decoder import DecoderSetupRequired, confirm_decoder_setup
from vntts.game_content_importer import (
    GameContentImportCancelled,
    Reverse1999GameImporter,
)
from vntts.pregeneration_acceptance import OfflineAcceptanceWorker
from vntts.pregeneration_audition_ui import VoiceAuditionPanel, VoiceAuditionUIError
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationProgress,
    OfflineGenerationWorker,
)
from vntts.pregeneration_pack import (
    OfflinePackError,
    OfflinePackPublisher,
    OfflinePackResult,
    OfflinePreparationChanges,
    inspect_story_audio,
    load_saved_pack,
)
from vntts.pregeneration_queue import (
    PregenerationInput,
    PregenerationInputStore,
    PregenerationQueueCancelled,
)
from vntts.pregeneration_recovery import OfflineRecoveryWorker
from vntts.pregeneration_setup import (
    ContentDiscovery,
    PregenerationJobStore,
    PregenerationSetupError,
    discover_game_content,
    estimate_generation_resources,
    estimate_preparation,
    inspect_story_index,
)
from vntts.pregeneration_voices import (
    PregenerationVoiceCancelled,
    VoiceDecisionStore,
    VoicePlanStore,
    pregeneration_narrator_source_id,
    resolve_pregeneration_settings,
)
from vntts.qt_audio import QtPcmPlayer
from vntts.release_backends import speech_backend_options
from vntts.speech_presentation import (
    speech_configuration_label,
    speech_configuration_rows,
)
from vntts.ui_text import (
    copy_text_button,
    make_text_copyable,
    plain_label_text,
    set_labeled_text,
)
from vntts.voices import (
    CharacterVoiceRegistry,
    VoiceChoice,
    find_default_voice_manifest,
    find_voice_assignment,
    is_narrator,
    is_unattributed_speaker,
    normalize_character_name,
    pocket_tts_preset_voices,
)


class OfflineAudioPreparationDialog(QDialog):
    decoderProgress = Signal(str)
    phaseChanged = Signal(str)
    activityChanged = Signal(bool)
    preparationRequested = Signal()
    packReady = Signal()
    readingRequested = Signal()

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
        game_narrator_chooser=None,
        automatic_activation=False,
        thread_pool=None,
        parent=None,
    ):
        super().__init__(parent)
        self.settings = settings
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
        self.game_narrator_chooser = game_narrator_chooser
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
        self.progress_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.progress_runner.finished.connect(self._progress_finished)
        for runner in (
            self.discovery_runner,
            self.import_runner,
            self.voice_runner,
            self.input_runner,
            self.generation_runner,
            self.recovery_runner,
            self.acceptance_runner,
            self.publication_runner,
            self.saved_pack_runner,
        ):
            runner.activeChanged.connect(self.activityChanged.emit)
        self._progress_baseline = None
        self._progress_snapshot = None
        self._progress_changed_at = monotonic()
        self._progress_error = None
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
        self.activating_saved = False
        self._close_after_voice_cancel = False
        self._content = ()
        self._story_selection_drafts = {}
        self._unsaved_story_selections = {}
        self._story_audio_checks = {}
        self._story_playback_speakers = {}
        self._checking_story = None
        self._story_job_statuses = {}
        self._job = None
        self._voice_plan = None
        self._prepared_voice_manifest = None
        self._prepared_voice_job = None
        self._generation_input = None
        self._generation_result = None
        self._recovery_result = None
        self._acceptance_result = None
        self._pack_result = None
        self._awaiting_voice_confirmation = False
        self._changes_rows = ()
        self._resume_error_details = ""
        self._narrator_player = preview_player
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

        self.engine_choice = QComboBox()
        self.engine_choice.setAccessibleName("Offline generation engine")
        for label, backend, available in speech_backend_options(
            settings.speech_backend
        ):
            if backend == "coqui-xtts":
                if settings.speech_backend != backend:
                    continue
                label += " (not supported for story preparation)"
                available = False
            self.engine_choice.addItem(label, backend)
            self.engine_choice.model().item(self.engine_choice.count() - 1).setEnabled(
                available
            )
        self.engine_choice.setCurrentIndex(
            max(0, self.engine_choice.findData(settings.speech_backend))
        )
        self.model_choice = QLineEdit(settings.tts_model or "")
        self.model_choice.setPlaceholderText("Default model shown above")
        self.model_choice.setAccessibleName("Offline generation model")
        self.model_choice.setEnabled(
            settings.speech_backend in {"coqui-xtts", "moss-tts"}
        )
        self.model_choice.setVisible(self.model_choice.isEnabled())
        self.engine_choice.currentIndexChanged.connect(self._engine_changed)
        self.model_choice.textChanged.connect(self._model_changed)
        self.engine_controls = QWidget()
        engine_row = QHBoxLayout(self.engine_controls)
        engine_row.setContentsMargins(0, 0, 0, 0)
        engine_row.addWidget(QLabel("Generate with"))
        engine_row.addWidget(self.engine_choice, 1)
        engine_row.addWidget(self.model_choice, 1)
        self.engine_controls.setVisible(game_narrator_chooser is None)

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
            "Choose the narrator and speech engine in the shared Voices editor"
        )
        self.game_narrator_button.setVisible(game_narrator_chooser is not None)
        self.game_narrator_button.clicked.connect(self._choose_game_narrator)
        narrator_row.addWidget(self.game_narrator_button)
        self._refresh_narrator_status()

        self.pocket_voice_cloning = QCheckBox(
            "I accepted the Pocket terms; enable game voice cloning"
        )
        self.pocket_voice_cloning.setChecked(settings.pocket_gated_model_accepted)
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
            and resolve_pregeneration_settings(settings).speech_backend == "pocket-tts"
        )
        self.pocket_voice_cloning.setVisible(uses_pocket)
        self.pocket_terms.setVisible(uses_pocket)

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

        self.source_status = QLabel()
        self.source_status.setWordWrap(True)
        self.source_status.setAccessibleName("Game content status")
        self.stories = QListWidget()
        self.stories.setWordWrap(True)
        self.stories.setAccessibleName("Stories and chapters")
        self.stories.setAccessibleDescription(
            "Check every story or chapter to prepare for offline speech"
        )
        self.stories.setMinimumHeight(100)
        self.stories.itemChanged.connect(self._selection_changed)
        self.stories.currentRowChanged.connect(self._story_audio_changed)
        self.check_story_audio = QPushButton("Check story audio")
        self.check_story_audio.setEnabled(False)
        self.check_story_audio.clicked.connect(self._check_story_audio)
        self.story_audio_status = QLabel("Highlight a story to check its saved audio.")
        self.story_audio_status.setWordWrap(True)
        self.story_audio_status.setAccessibleName("Highlighted story audio coverage")

        self.story_search = QLineEdit()
        self.story_search.setPlaceholderText("Search stories...")
        self.story_search.setAccessibleName("Search stories by title")
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
        self.story_filter_status = QLabel()
        self.story_filter_status.setAccessibleName("Shown and selected story counts")

        selection_actions = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self.select_none_button = QPushButton("Select none")
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))
        self.change_voices = QCheckBox("Change saved voice choices")
        self.change_voices.toggled.connect(self._selection_changed)
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
        self.prepare_again = QPushButton("Prepare selected stories again")
        self.prepare_again.setAccessibleDescription(
            "Apply current voice defaults only to checked stories, reusing matching recordings and saved voice choices"
        )
        self.prepare_again.clicked.connect(self._prepare_again_requested)
        self.prepare_again.hide()
        self.selection_status = QLabel()
        self.selection_status.setWordWrap(True)
        self.selection_status.hide()
        self.resume_status = QLabel()
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

        self.progress_panel = QGroupBox("Preparation progress")
        self.progress_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.progress_phase = QLabel()
        self.progress_phase.setAccessibleName("Offline preparation phase")
        self.progress_phase.setStyleSheet("font-weight: 600;")
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
        self.progress_timing = QLabel()
        self.progress_timing.setWordWrap(True)
        self.progress_timing.setAccessibleName("Progress freshness and remaining time")
        self.progress_runtime = QLabel()
        self.progress_runtime.setWordWrap(True)
        self.progress_runtime.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_runtime.setAccessibleName("Offline generation device")
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
        progress_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        progress_layout.addWidget(self.progress_phase)
        progress_configuration_row = QHBoxLayout()
        progress_configuration_row.addWidget(self.progress_configuration, 1)
        progress_configuration_row.addWidget(self.copy_progress_configuration)
        progress_layout.addLayout(progress_configuration_row)
        progress_layout.addWidget(self.story_context)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_counts)
        progress_layout.addWidget(self.progress_timing)
        progress_layout.addWidget(self.progress_runtime)
        progress_layout.addWidget(self.resume_status)
        progress_layout.addWidget(self.progress_guarantee)
        progress_layout.addWidget(self.progress_failures)
        progress_layout.addWidget(self.progress_cancel_consequence)
        progress_layout.addWidget(self.progress_coverage)
        self.progress_panel.hide()

        self.voice_confirmation = QGroupBox("Preparation summary")
        self.voice_confirmation.setVisible(False)
        self.voice_configuration = QLabel()
        self.voice_configuration.setWordWrap(True)
        self.confirmed_narrator = QLabel()
        self.confirmed_narrator.setWordWrap(True)
        self.confirmed_narrator.setAccessibleName("Narrator for prepared audio")
        self.edit_confirmed_narrator = QPushButton("Edit narrator in Voices...")
        self.edit_confirmed_narrator.clicked.connect(self._choose_game_narrator)
        self.edit_confirmed_narrator.setVisible(game_narrator_chooser is not None)
        self.work_summary = QLabel()
        self.work_summary.setWordWrap(True)
        self.work_summary.setAccessibleName("Selected story preparation estimate")
        self.change_summary = QLabel()
        self.change_summary.setWordWrap(True)
        self.change_summary.setAccessibleName("Changes before generation")
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
        self.voice_routes = QListWidget()
        self.voice_routes.setIconSize(QSize(64, 64))
        self.voice_routes.setAccessibleName("Planned character voice routes")
        self.voice_routes.setMinimumHeight(90)
        self.choose_character_voice = QPushButton("Choose another character's voice...")
        self.choose_character_voice.setAccessibleName("Change selected character voice")
        self.choose_character_voice.setEnabled(False)
        self.choose_character_voice.clicked.connect(self._choose_character_voice)
        self.voice_routes.currentItemChanged.connect(
            lambda item, _previous: self.choose_character_voice.setEnabled(
                item is not None
            )
        )
        self.voice_routes.itemDoubleClicked.connect(
            lambda _item: self._choose_character_voice()
        )
        self.voice_route_summary = QLabel()
        self.voice_route_summary.setWordWrap(True)
        self.show_all_voice_routes = QCheckBox("Show all character assignments")
        self.show_all_voice_routes.toggled.connect(
            lambda: self._render_voice_routes(self._voice_plan)
        )
        self.voice_confirmation_status = QLabel()
        self.voice_confirmation_status.setWordWrap(True)
        confirmation_layout = QVBoxLayout(self.voice_confirmation)
        confirmation_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        confirmation_layout.addWidget(self.work_summary)
        confirmation_layout.addWidget(self.confirmed_narrator)
        confirmation_layout.addWidget(self.edit_confirmed_narrator)
        confirmation_layout.addWidget(self.voice_configuration)
        confirmation_layout.addWidget(self.change_summary)
        confirmation_layout.addWidget(self.narrator_controls)
        confirmation_layout.addWidget(self.voice_route_summary)
        confirmation_layout.addWidget(self.show_all_voice_routes)
        confirmation_layout.addWidget(self.voice_routes)
        confirmation_layout.addWidget(self.choose_character_voice)
        confirmation_layout.addWidget(self.voice_confirmation_status)

        self.discovery_panel = QGroupBox("Loading game content")
        self.discovery_panel.setAccessibleName("Loading game content")
        discovery_message = QLabel(
            "Finding local stories. Controls unlock when loading finishes."
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
        self.continue_button.clicked.connect(self._continue_requested)
        self.cancel_button.clicked.connect(self._cancel_or_reject)

        self.selection_panel = QWidget()
        selection_layout = QVBoxLayout(self.selection_panel)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        selection_layout.setSpacing(4)
        selection_layout.addLayout(narrator_row)
        selection_layout.addWidget(self.pocket_voice_cloning)
        selection_layout.addWidget(self.pocket_terms)
        selection_layout.addWidget(self.engine_controls)
        selection_layout.addLayout(source_row)
        selection_layout.addWidget(self.import_options)
        selection_layout.addWidget(self.source_status)
        selection_layout.addLayout(story_filters)
        selection_layout.addWidget(self.summary)
        story_status_row = QHBoxLayout()
        story_status_row.addWidget(self.story_filter_status, 1)
        story_status_row.addWidget(self.check_story_audio)
        selection_layout.addLayout(story_status_row)
        selection_layout.addWidget(self.stories, 1)
        selection_layout.addLayout(selection_actions)
        selection_layout.addWidget(self.story_audio_status)
        selection_layout.addWidget(self.prepare_again)
        selection_layout.addWidget(self.selection_status)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setSpacing(4)
        layout.addWidget(self.discovery_panel)
        layout.addWidget(self.voice_panel)
        layout.addWidget(self.voice_confirmation, 1)
        layout.addWidget(self.progress_panel)
        layout.addWidget(self.copy_resume_error)
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
        shell.addWidget(self.buttons)
        make_text_copyable(self)
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
        self.coverage_runner.cancel()
        self._checking_story = None
        self._story_audio_checks.clear()
        self._story_playback_speakers.clear()
        self._story_audio_changed()
        if self._background_discovery:
            self._set_discovery_loading(True)
            self.discovery_runner.start(self._discover_content)
            return
        self._apply_discovery(self._discover_content())

    def _choose_game_narrator(self):
        settings = self.game_narrator_chooser(self.settings, self)
        if settings is None:
            return
        self.apply_narrator_settings(settings)

    def _choose_character_voice(self):
        item = self.voice_routes.currentItem()
        if item is None or self.has_pending_work():
            return
        character = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(character, str) or not character:
            return
        if self._narrator_player is not None:
            self._narrator_player.stop()
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
            self.apply_narrator_settings(settings)

    def apply_narrator_settings(self, settings):
        if self.has_pending_work():
            return
        if (
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
                self.engine_choice.findData(settings.speech_backend)
            )
            self.model_choice.setText(settings.tts_model or "")
            self.pocket_voice_cloning.setChecked(settings.pocket_gated_model_accepted)
        self.model_choice.setVisible(
            settings.speech_backend in {"coqui-xtts", "moss-tts"}
        )
        self._voice_plan = None
        self._prepared_voice_manifest = None
        self._prepared_voice_job = None
        self._generation_input = None
        self._generation_result = None
        self._recovery_result = None
        self._acceptance_result = None
        self._pack_result = None
        self._changes_rows = ()
        self._awaiting_voice_confirmation = False
        self.voice_confirmation.hide()
        self.progress_panel.hide()
        self.voice_panel.hide()
        self.selection_panel.show()
        self._set_import_controls(True)
        self.step.setText("Step 1 of 4 - Choose stories")
        self.continue_button.setText("Continue")
        self.cancel_button.setText("Cancel")
        self._refresh_narrator_status()
        self._selection_changed()
        if return_to_confirmation:
            self.preparationRequested.emit()
            self._save_selection()

    def _engine_changed(self):
        backend = self.engine_choice.currentData()
        self.settings = self.settings.updated(
            speech_backend=backend,
            tts_model=None,
            tts_profile="default" if backend == "pocket-tts" else "stable",
        )
        self.model_choice.clear()
        self.model_choice.setEnabled(backend in {"coqui-xtts", "moss-tts"})
        self.model_choice.setVisible(backend in {"coqui-xtts", "moss-tts"})
        self.pocket_voice_cloning.setVisible(backend == "pocket-tts")
        self.pocket_terms.setVisible(backend == "pocket-tts")
        self._voice_plan = None
        self._refresh_narrator_status()
        self._selection_changed()

    def _model_changed(self, model):
        self.settings = self.settings.updated(tts_model=model.strip() or None)
        self._voice_plan = None
        self._refresh_narrator_status()

    def _generation_engine_available(self):
        return self.settings.speech_backend != "coqui-xtts" and any(
            backend == self.settings.speech_backend and available
            for _label, backend, available in speech_backend_options(
                self.settings.speech_backend
            )
        )

    def _pocket_cloning_toggled(self, enabled):
        self.settings = self.settings.updated(pocket_gated_model_accepted=bool(enabled))
        self._refresh_narrator_status()
        if self._awaiting_voice_confirmation and self._voice_plan is not None:
            self._show_voice_confirmation(self._voice_plan)
            if not self._awaiting_voice_confirmation:
                return
            self.continue_button.setText("Update voice routes")
            self.voice_confirmation_status.setText(
                "Voice cloning changed. Update the routes before generation."
            )
        else:
            self._voice_plan = None

    def _refresh_narrator_status(self):
        settings, narrator = self._narrator_configuration_values()
        set_labeled_text(
            self.narrator_status,
            speech_configuration_rows(settings, narrator=narrator),
        )
        self.narrator_status.setToolTip(self._full_narrator_configuration())

    def _narrator_configuration_values(self):
        settings = resolve_pregeneration_settings(self.settings)
        narrator = None
        if self._awaiting_voice_confirmation and self._voice_plan is not None:
            plan = self._voice_plan
            if isinstance(plan.synthesis_backend, str):
                settings = settings.updated(
                    speech_backend=plan.synthesis_backend,
                    tts_model=plan.synthesis_model,
                )
            if self.narrator_choice.currentData() is not None:
                narrator = self.narrator_choice.currentText()
        return settings, narrator

    def _narrator_configuration(self, *, compact=False):
        settings, narrator = self._narrator_configuration_values()
        return speech_configuration_label(settings, narrator=narrator, compact=compact)

    def _full_narrator_configuration(self):
        return (
            "Defaults for future preparation and live speech\n"
            + self._narrator_configuration()
        )

    def _full_generation_details(self):
        content = self.current_content()
        return self._full_narrator_configuration() + (
            f"\nSource story index: {content.story_index}\n"
            f"{plain_label_text(self.story_context)}"
            if content is not None
            else ""
        )

    def _voice_choices(self, plan):
        choices = []
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

    def _show_voice_confirmation(self, plan):
        local_voice_controls = self.game_narrator_chooser is None
        show_terms = (
            local_voice_controls and self.settings.speech_backend == "pocket-tts"
        )
        self.pocket_voice_cloning.setVisible(show_terms)
        self.pocket_terms.setVisible(show_terms)
        self.content_scroll.verticalScrollBar().setValue(0)
        try:
            choices = self._voice_choices(plan)
        except (OSError, ValueError) as error:
            self._awaiting_voice_confirmation = False
            self.voice_confirmation.hide()
            self.selection_panel.show()
            self._set_import_controls(True)
            self.continue_button.setText("Retry voice matching")
            self.selection_status.setText(f"Unable to show character voices: {error}")
            return
        self._awaiting_voice_confirmation = True
        self.game_narrator_button.setEnabled(True)
        self.pocket_voice_cloning.setEnabled(True)
        self.selection_panel.hide()
        self.voice_panel.hide()
        self.progress_panel.hide()
        self.voice_confirmation.show()
        self.work_summary.setTextFormat(Qt.TextFormat.RichText)
        self.work_summary.setText(self.summary.text())
        self.voice_configuration.setText(
            "Original game audio stays. Changing voices or model may require new recordings."
            + (
                " When finished, the saved audio becomes active for reading automatically. "
                "This replaces live voice overrides for these story roles; playback does not start."
                if self.automatic_activation
                else ""
            )
        )
        self.confirmed_narrator.setText(self._narrator_configuration())
        self.voice_configuration.setToolTip(
            "These voices apply to generated lines in the selected stories. "
            "Matching saved work can be resumed. Other prepared stories remain unchanged."
        )
        self.narrator_choice.blockSignals(True)
        self.narrator_choice.clear()
        self.narrator_choice.addItem("Choose a narrator voice...", None)
        for choice in choices:
            self.narrator_choice.addItem(choice.label, choice.id)
            self.narrator_choice.setItemData(
                self.narrator_choice.count() - 1,
                choice.description,
                Qt.ItemDataRole.ToolTipRole,
            )
        current = pregeneration_narrator_source_id(self.settings)
        selected = self.narrator_choice.findData(current)
        self.narrator_choice.setCurrentIndex(max(0, selected))
        self.narrator_choice.blockSignals(False)
        self._render_voice_routes(plan)
        self.continue_button.setText("Generate with these voices")
        self.continue_button.setAccessibleDescription(
            "Start offline generation with the exact visible voice routes"
        )
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._narrator_choice_changed()

    def _render_voice_routes(self, plan):
        previous = self.voice_routes.currentItem()
        selected_character = (
            previous.data(Qt.ItemDataRole.UserRole) if previous else None
        )
        self.voice_routes.clear()
        if plan is None:
            return
        groups = plan.groups if isinstance(plan.groups, (tuple, list)) else ()
        exceptions = [
            group
            for group in groups
            if normalize_character_name(group.character) != "narrator"
            and (
                group.route != "voice"
                or normalize_character_name(group.character)
                != normalize_character_name(
                    group.source_character or group.source_speaker or ""
                )
            )
        ]
        self.voice_route_summary.setText(
            f"{len(exceptions)} of {len(groups)} voice roles use a substitute or need attention."
            if exceptions
            else "No character voice substitutions. Narrator is shown above."
        )
        visible = groups if self.show_all_voice_routes.isChecked() else exceptions
        self.voice_routes.setVisible(bool(visible))
        for group in sorted(
            visible,
            key=lambda value: (value.character.casefold(), value.group_id),
        ):
            lines = len(group.line_ids)
            if group.route == "narrator":
                source = self.narrator_choice.currentText()
                route = (
                    source
                    if group.character == "Narrator"
                    else f"Narrator voice ({source})"
                )
            else:
                route = group.source_character or group.source_speaker or "No voice"
            item = QListWidgetItem(
                f"{group.character} -> {route} - {lines} "
                f"line{'s' if lines != 1 else ''}"
            )
            item.setData(Qt.ItemDataRole.UserRole, group.character)
            if group.portrait_image and group.portrait_image_sha256:
                try:
                    if sha256_file(group.portrait_image) == group.portrait_image_sha256:
                        pixmap = QPixmap(group.portrait_image)
                        if not pixmap.isNull():
                            item.setIcon(QIcon(pixmap))
                except OSError:
                    pass
            self.voice_routes.addItem(item)
            if group.character == selected_character:
                self.voice_routes.setCurrentItem(item)
        if self.voice_routes.currentItem() is None and self.voice_routes.count():
            self.voice_routes.setCurrentRow(0)

    def _narrator_choice_changed(self, _index=None):
        source_id = self.narrator_choice.currentData()
        self._refresh_narrator_status()
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
            and any(group.route == "narrator" for group in self._voice_plan.groups)
        )
        ready = source_id is not None or not needs_narrator
        changed = (
            source_id is not None
            and source_id != pregeneration_narrator_source_id(self.settings)
        ) or (
            self._voice_plan is not None
            and self._voice_plan.synthesis_backend == "pocket-tts"
            and self._voice_plan.pocket_voice_cloning
            != self.settings.pocket_gated_model_accepted
        )
        if changed:
            set_labeled_text(
                self.change_summary,
                (("Voice choices", "Changed. Update routes to recalculate changes."),),
            )
        else:
            set_labeled_text(self.change_summary, self._changes_rows)
        self.continue_button.setText(
            "Update voice routes" if changed else "Generate with these voices"
        )
        self.continue_button.setEnabled(ready)
        self.voice_confirmation_status.setText(
            "Choose a narrator before generation."
            if not ready
            else "Generation will use exactly the routes shown above."
        )

    def _play_narrator_reference(self):
        source_id = self.narrator_choice.currentData()
        if not (
            self._voice_plan is not None
            and self._voice_plan.voice_manifest
            and isinstance(source_id, str)
            and source_id.startswith("character:")
        ):
            return
        try:
            registry = CharacterVoiceRegistry.from_file(self._voice_plan.voice_manifest)
            voice = registry.resolve_source(source_id)
            reference = voice.references[0]
            if self._narrator_player is None:
                self._narrator_player = QtPcmPlayer(self)
                self._narrator_player.errorOccurred.connect(
                    lambda _code, message: self.voice_confirmation_status.setText(
                        f"Unable to play the original game voice: {message}"
                    )
                )
            self._narrator_player.stop()
            self._narrator_player.setSource(QUrl.fromLocalFile(str(reference)))
            self._narrator_player.play()
        except Exception as error:
            self.voice_confirmation_status.setText(
                f"Unable to play the original game voice: {error}"
            )

    def _confirm_voice_plan(self):
        if self._narrator_player is not None:
            self._narrator_player.stop()
        source_id = self.narrator_choice.currentData()
        current = pregeneration_narrator_source_id(self.settings)
        self._awaiting_voice_confirmation = False
        self.pocket_voice_cloning.setEnabled(False)
        self.voice_confirmation.hide()
        planned_cloning = getattr(self._voice_plan, "pocket_voice_cloning", None)
        controls_changed = (
            self._voice_plan.synthesis_backend == "pocket-tts"
            and isinstance(planned_cloning, bool)
            and (planned_cloning != self.settings.pocket_gated_model_accepted)
        )
        narrator_changed = source_id is not None and source_id != current
        if controls_changed or narrator_changed:
            if narrator_changed:
                assignments = {
                    character: value
                    for character, value in self.settings.voice_assignments.items()
                    if normalize_character_name(character) != "narrator"
                }
                assignments["Narrator"] = source_id
                self.settings = self.settings.updated(voice_assignments=assignments)
                self._refresh_narrator_status()
            self.planning_voices = True
            self.replanning_voice_decisions = False
            self._show_waiting_phase(
                "Applying narrator voice",
                "Updating the visible voice routes before generation...",
                "Cancel stops voice matching. Nothing has been generated yet.",
            )
            self.voice_runner.start(
                self._create_voice_plan,
                self._job,
                False,
            )
            return
        self._start_generation()

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
        if loading:
            self.phaseChanged.emit("Finding installed stories")
        self.game_narrator_button.setEnabled(not loading)
        self.pocket_voice_cloning.setEnabled(not loading)
        self.discovery_panel.setVisible(loading)
        self.selection_panel.setVisible(not loading)
        self.content_scroll.setVisible(loading)
        self.selection_panel.setEnabled(not loading)
        self.continue_button.setEnabled(
            not loading
            and bool(self.selected_story_ids())
            and self._generation_engine_available()
        )

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

    def _update_import_options(self, _checked=None):
        has_content = bool(self._content)
        expanded = not has_content or self.import_options_toggle.isChecked()
        self.import_options.setVisible(expanded)
        self.import_options_toggle.setVisible(has_content)
        self.import_options_toggle.setText(
            "Hide import options"
            if expanded and has_content
            else "Add or import content..."
        )

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
            self.source_status.show()
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
        self.source_status.show()
        self.import_options_toggle.setChecked(False)
        self._update_import_options()

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
            self.source_status.show()
            return
        self.importing = True
        self.phaseChanged.emit("Importing installed game")
        self.import_cancel_event.clear()
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel import")
        self.cancel_button.setEnabled(True)
        self.source_status.setText(
            "Finding the installed game and importing story content..."
        )
        self.source_status.show()
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
        self._clear_resume_error()
        self.pocket_voice_cloning.hide()
        self.pocket_terms.hide()
        self.selection_panel.hide()
        self.content_scroll.show()
        self.content_scroll.verticalScrollBar().setValue(0)
        self.progress_panel.show()
        self.progress_phase.setText(phase)
        settings, narrator = self._narrator_configuration_values()
        set_labeled_text(
            self.progress_configuration,
            speech_configuration_rows(settings, narrator=narrator),
        )
        self.progress_configuration.setToolTip(self._full_generation_details())
        self.story_context.show()
        self.resume_status.setText(detail)
        self.progress_cancel_consequence.setText(cancel_consequence)
        self._refresh_story_statuses()
        self._emit_task_progress()

    def _emit_task_progress(self):
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

    def _preparation_paused(self, phase, error):
        self.progress_phase.setText(phase)
        if (
            self._job is not None
            and error is not None
            and not isinstance(
                error,
                (
                    OfflineGenerationCancelled,
                    PregenerationVoiceCancelled,
                    PregenerationQueueCancelled,
                ),
            )
        ):
            for selection_id in self._job.selected_story_ids:
                self._story_audio_checks[self._story_audio_key(selection_id)] = (
                    None,
                    None,
                    error,
                )
        self._refresh_story_statuses()
        self._selection_changed()
        QTimer.singleShot(0, self._emit_task_progress)

    def _set_resume_error(self, prefix, error):
        self._resume_error_details = f"{prefix}: {error}"
        self.resume_status.setText(self._resume_error_details)
        self.selection_status.setText(self._resume_error_details)
        self.selection_status.show()
        self.copy_resume_error.show()

    def _clear_resume_error(self):
        self._resume_error_details = ""
        self.copy_resume_error.hide()
        self.selection_status.hide()

    def _show_waiting_phase(self, phase, detail, cancel_consequence):
        self._show_phase(phase, detail, cancel_consequence)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.progress_counts.clear()
        self.progress_timing.clear()
        self.progress_runtime.clear()
        self.progress_guarantee.setText(
            "Your selected stories and completed voice choices are saved for restart."
        )
        self.progress_failures.clear()
        self.progress_coverage.clear()
        self._emit_task_progress()

    def _set_progress_counts(self, completed, total, generated=0, failed=0, other=0):
        set_labeled_text(
            self.progress_counts,
            (
                ("Progress", f"{completed} of {total} lines processed and saved"),
                ("Prepared", str(generated)),
                ("Failed", str(failed)),
                ("Without prepared audio", str(other)),
            ),
        )

    def _set_progress_timing(self, value):
        set_labeled_text(self.progress_timing, (("Time", value),))

    def _start_generation_progress(self):
        self._stop_generation_progress()
        self._progress_baseline = None
        self._progress_snapshot = None
        self._progress_changed_at = monotonic()
        self._progress_error = None
        self.progress_runtime.setText("Generation device: waiting for the worker.")
        total = self._generation_input.ready_items
        self._show_phase(
            "Generating offline audio",
            f"Generating {total} offline lines.",
            "Cancel stops generation and closes this window. Finished lines stay "
            "saved; reopen and select Continue to generate only unfinished lines.",
        )
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat(f"0 of {total} processed")
        self._set_progress_counts(0, total)
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
        self._refresh_progress_timing()
        if self.progress_runner.active:
            return
        inspect = getattr(self.generator, "inspect_progress", None)
        if not callable(inspect):
            return
        self.progress_runner.start(inspect, self._generation_input)

    def _progress_finished(self, progress, error):
        if not (self.generating or self.recovering) or self._close_after_voice_cancel:
            return
        if error is not None or not isinstance(progress, OfflineGenerationProgress):
            self._progress_error = str(error or "Invalid progress response")
            self.progress_runtime.setText("Generation device: report unavailable.")
        else:
            self._progress_error = None
            self.progress_runtime.setText(
                progress.runtime_status
                or "Generation device: waiting for the worker to confirm CPU/GPU use."
            )
            now = monotonic()
            if progress != self._progress_snapshot:
                self._progress_changed_at = now
            self._progress_snapshot = progress
            if progress.available and self._progress_baseline is None:
                self._progress_baseline = (now, progress.completed)
            if progress.available:
                self._render_generation_progress(progress)
        self._refresh_progress_timing()

    def _refresh_progress_timing(self):
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
        estimate = "Estimating remaining time..."
        if self.recovering:
            estimate = "Recovery time varies by failure."
        elif self._progress_baseline is not None:
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
        self._set_progress_timing(f"Last progress change {age}s ago. {estimate}")

    def _stop_generation_progress(self):
        self.progress_timer.stop()
        self.progress_runner.cancel()
        self.progress_timing.clear()
        self.progress_runtime.clear()

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
        self.progress_bar.setFormat(f"{completed} of {total} processed")
        self._set_progress_counts(
            completed,
            total,
            progress.generated,
            progress.failed,
            progress.other_terminal,
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
        self._emit_task_progress()

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
        self.step.setText("Step 4 of 4 - Activate and read")
        self.selection_panel.hide()
        self.voice_panel.hide()
        self._render_generation_result(self._generation_result)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.progress_bar.setFormat("Audio saved")
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
            "Ready with live speech for remaining lines"
            if live
            else "Offline audio is ready",
            "Your story audio is saved, but not active yet. Click Use prepared audio, "
            "This also replaces live voice overrides for these story roles. "
            "Then open this story in the game and click Start reading. "
            "Reading uses saved recordings automatically; only uncovered lines need TTS.",
            "Close leaves the current audio setup unchanged; the saved pack can be "
            "activated by reopening this preparation later.",
        )
        coverage_rows = [
            ("Original game audio", f"{original} in this selection"),
            ("Prepared", f"{prepared} in the saved pack"),
            ("Live fallback", str(live)),
            (
                "Story lines",
                str(story_lines or self._job.estimate.selected_lines),
            ),
        ]
        if omissions:
            coverage_rows.append(("Omissions", f"{omissions} explicit"))
        set_labeled_text(self.progress_coverage, coverage_rows)
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
        if self.automatic_activation:
            self.progress_phase.setText("Audio saved; connecting it to Reading")
            self.resume_status.setText(
                "Activating the validated recordings. Playback will not start automatically."
            )
            self.continue_button.setEnabled(False)
            self.packReady.emit()

    def defer_activation(self, reason):
        self.progress_phase.setText("Audio saved; activation needs attention")
        self.resume_status.setText(
            reason
            + " Review the saved voice setup, then click Use prepared audio when ready. Closing keeps the current reading setup unchanged."
        )
        self.continue_button.setEnabled(True)

    def _source_changed(self, _index):
        self.coverage_runner.cancel()
        self._checking_story = None
        self.step.setText("Step 1 of 4 - Choose stories")
        self._populate_stories(self.current_content())

    def _story_audio_changed(self, _row=None):
        self.story_audio_status.setToolTip("")
        item = self.stories.currentItem()
        self.check_story_audio.setEnabled(
            item is not None and not self.coverage_runner.active
        )
        self.story_audio_status.setVisible(item is not None)
        self.story_audio_status.setText(
            "Saved audio for selected stories is checked automatically. You can also recheck this story."
        )
        if item is not None:
            checked = self._story_audio_checks.get(
                self._story_audio_key(item.data(Qt.ItemDataRole.UserRole))
            )
            if checked is not None:
                self._render_story_audio_check(*checked)

    def _story_audio_key(self, selection_id):
        content = self.current_content()
        return (
            content.story_index_sha256 if content is not None else None,
            selection_id,
            self.settings.game_pack,
        )

    def _inspect_story_audio(self, content, selection_id, active_manifest):
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

    def _reading_override_reason(self, selection_id, coverage):
        settings = self.settings
        if settings.audio_source_policy == "live-tts-only":
            return "Reading uses live speech only. Choose a recording playback policy in Settings."
        if coverage.original and settings.audio_source_policy != "prefer-game-audio":
            return "This story relies on original game voices. Choose Original game audio in Settings."
        if coverage.generated and settings.speech_rate_percent != 100:
            return "Reading skips prepared recordings at this speed. Set speech speed to 100% in Settings."
        overrides = [
            speaker
            for speaker in self._story_playback_speakers.get(
                self._story_audio_key(selection_id), ()
            )
            if not is_unattributed_speaker(speaker)
            and find_voice_assignment(settings.voice_assignments, speaker) is not None
            and (not is_narrator(speaker) or settings.force_live_narrator)
        ]
        if overrides:
            return (
                "Live voice overrides replace recordings for "
                + ", ".join(sorted(overrides))
                + ". Clear those overrides in Voices or prepare these stories again."
            )
        return ""

    def _check_story_audio(self):
        item = self.stories.currentItem()
        content = self.current_content()
        if item is None or content is None:
            return
        selection_id = item.data(Qt.ItemDataRole.UserRole)
        self._checking_story = self._story_audio_key(selection_id)
        self.check_story_audio.setEnabled(False)
        self.story_audio_status.setText(
            "Checking saved pack and recordings in the background..."
        )
        self.content_scroll.ensureWidgetVisible(self.story_audio_status)
        self.coverage_runner.start(
            self._inspect_story_audio,
            content,
            selection_id,
            self.settings.game_pack,
        )

    def _queue_story_checks(self):
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

    def _story_audio_finished(self, result, error):
        self.check_story_audio.setEnabled(
            self.stories.currentItem() is not None and not self.has_pending_work()
        )
        if self._checking_story is None:
            return
        key = self._checking_story
        saved, active, speakers = result if error is None else (None, None, ())
        self._story_playback_speakers[key] = speakers
        self._story_audio_checks[key] = (saved, active, error)
        self._checking_story = None
        self._refresh_story_statuses()
        self._selection_changed()
        item = self.stories.currentItem()
        if (
            item is not None
            and self._story_audio_key(item.data(Qt.ItemDataRole.UserRole)) == key
        ):
            self._render_story_audio_check(saved, active, error)

    def _render_story_audio_check(self, coverage, active, error):
        self.story_audio_status.show()
        if error is not None:
            self.story_audio_status.setText(f"Audio needs attention: {error}")
            self.content_scroll.ensureWidgetVisible(self.story_audio_status)
            return
        if active is not None and not active.missing:
            coverage = active
        active_story = coverage is active
        item = self.stories.currentItem()
        playback_warning = (
            self._reading_override_reason(item.data(Qt.ItemDataRole.UserRole), coverage)
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
        self.content_scroll.ensureWidgetVisible(self.story_audio_status)

    def _refresh_story_statuses(self):
        content = self.current_content()
        if content is None:
            return
        selections = {value.selection_id: value for value in content.selections}
        with QSignalBlocker(self.stories):
            for row in range(self.stories.count()):
                item = self.stories.item(row)
                selection_id = item.data(Qt.ItemDataRole.UserRole)
                selection = selections[selection_id]
                saved_status = self._story_job_statuses.get(selection_id)
                status = "in_progress" if saved_status else "not_started"
                detail = (
                    "checking saved audio"
                    if saved_status == "ready"
                    else "saved progress; continue to finish"
                    if saved_status
                    else f"{selection.generation_lines} lines need speech"
                )
                checked = self._story_audio_checks.get(
                    self._story_audio_key(selection_id)
                )
                if checked is not None:
                    coverage, active, error = checked
                    if error is not None:
                        status, detail = "attention", str(error)
                    else:
                        if active is not None and not active.missing:
                            coverage = active
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
                            warning = self._reading_override_reason(
                                selection_id, coverage
                            )
                            if warning:
                                status = "attention"
                                detail += "; " + warning
                        elif coverage.manifest:
                            detail += "; not active in Reading"
                if (
                    self._job is not None
                    and self._job.story_index_sha256 == content.story_index_sha256
                    and selection_id in self._job.selected_story_ids
                    and self.has_pending_work()
                ):
                    status, detail = "preparing", self.progress_phase.text()
                label = {
                    "not_started": "Not prepared",
                    "preparing": "Preparing",
                    "in_progress": "Partially prepared",
                    "ready": "Ready",
                    "attention": "Needs attention",
                }[status]
                if status == "ready" and checked is not None and coverage.live:
                    label = "Ready with live speech"
                item.setText(
                    f"{selection.title} ({selection.line_count} line{'s' if selection.line_count != 1 else ''}) - "
                    f"{label}: {detail}"
                )
                item.setData(Qt.ItemDataRole.UserRole + 2, status)
        self._filter_stories()

    def _can_start_reading(self):
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

    def _saved_manifest_for_selection(self):
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

    def _load_saved_selection(self, content, selected, manifest):
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

    def _saved_pack_finished(self, result, error):
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

    def _populate_stories(self, content):
        self._story_audio_changed()
        self.stories.blockSignals(True)
        self.stories.clear()
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
            for selection in content.selections:
                item = QListWidgetItem(selection.title)
                item.setData(Qt.ItemDataRole.UserRole, selection.selection_id)
                item.setData(Qt.ItemDataRole.UserRole + 1, selection.title.casefold())
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if selection.selection_id in selected_ids
                    else Qt.CheckState.Unchecked
                )
                self.stories.addItem(item)
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
            self.selection_status.setText(message)
            self.resume_status.setText(message)
        else:
            self.selection_status.clear()
            self.resume_status.clear()
        self.stories.blockSignals(False)
        self._refresh_story_statuses()
        self._selection_changed()

    def _set_all_checked(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.stories.blockSignals(True)
        for row in range(self.stories.count()):
            item = self.stories.item(row)
            if not item.isHidden():
                item.setCheckState(state)
        self.stories.blockSignals(False)
        self._selection_changed()

    def _filter_stories(self, _value=None):
        query = self.story_search.text().strip().casefold()
        status = self.story_filter.currentData()
        shown = selected = hidden_selected = 0
        for row in range(self.stories.count()):
            item = self.stories.item(row)
            visible = query in item.data(Qt.ItemDataRole.UserRole + 1) and (
                status is None or status == item.data(Qt.ItemDataRole.UserRole + 2)
            )
            item.setHidden(not visible)
            checked = item.checkState() == Qt.CheckState.Checked
            shown += visible
            selected += checked
            hidden_selected += checked and not visible
        self.story_filter_status.setText(
            (
                f"{shown}/{self.stories.count()} shown; "
                if query or status is not None
                else ""
            )
            + f"{selected} selected"
            + (f"; {hidden_selected} hidden by filters." if hidden_selected else ".")
            + (
                " Clear filters to see other stories."
                if not shown and self.stories.count()
                else ""
            )
        )
        filtered = bool(query) or status is not None
        self.select_all_button.setText("Select shown" if filtered else "Select all")
        self.select_none_button.setText("Clear shown" if filtered else "Select none")

    def _selection_changed(self, _item=None):
        content = self.current_content()
        selected = self.selected_story_ids()
        if content is not None:
            if set(selected) != self._story_selection_drafts.get(
                content.story_index_sha256
            ):
                self._unsaved_story_selections[content.story_index_sha256] = (
                    content,
                    selected,
                )
            self._story_selection_drafts[content.story_index_sha256] = set(selected)
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
                "Checking saved audio..."
                if checking
                else "Start reading"
                if can_read
                else "Use prepared audio"
                if saved_manifest is not None
                else "Continue preparation"
                if any(
                    self.stories.item(row).checkState() == Qt.CheckState.Checked
                    and self.stories.item(row).data(Qt.ItemDataRole.UserRole + 2)
                    != "not_started"
                    for row in range(self.stories.count())
                )
                else (
                    "Choose a story"
                    if not selected
                    else "Prepare selected story"
                    if len(selected) == 1
                    else f"Prepare {len(selected)} stories"
                )
            )
        if checking:
            self.summary.setText(
                "Checking saved recordings for the selected stories. No audio will be generated."
            )
            self.continue_button.setEnabled(False)
            return
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
                        + (
                            " Open Voices to change the engine."
                            if self.game_narrator_chooser is not None
                            else ""
                        ),
                    ),
                ),
            )
            self.continue_button.setEnabled(False)
            return
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
            return
        if not selected:
            set_labeled_text(self.story_context, (("Stories", "No stories selected."),))
            self.story_context.setToolTip("")
            set_labeled_text(
                self.summary,
                (("Preparation", "Select at least one story or chapter."),),
            )
            self.continue_button.setEnabled(False)
            return
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
            ("Selected", f"{estimate.selected_lines} dialogue lines"),
            ("Original game audio", f"{estimate.original_audio_lines} lines"),
            (
                "Need speech",
                f"up to {estimate.generation_lines} lines across about "
                f"{estimate.speaker_count} voices",
            ),
            (
                "Rough storage",
                f"about {disk_megabytes} MB of audio for the full selection; saved recordings can be reused",
            ),
            (
                "Time",
                "Estimated after generation starts. First-time model setup and downloads take additional time.",
            ),
        ]
        if can_read:
            live_count = sum(
                self._story_audio_checks[self._story_audio_key(value)][1].live
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
        self.continue_button.setEnabled(True)

    def select_voice_affected_stories(self, results):
        """Offer the normal scoped preparation flow; saving a default never generates."""
        if self.has_pending_work():
            return
        selected = {value.selection_id for value in results if value.changed_line_ids}
        with QSignalBlocker(self.stories):
            for row in range(self.stories.count()):
                item = self.stories.item(row)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if item.data(Qt.ItemDataRole.UserRole) in selected
                    else Qt.CheckState.Unchecked
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

    def _prepare_again_requested(self):
        if self.has_pending_work() or not self._save_story_selection_drafts():
            return
        self.preparationRequested.emit()
        self._save_selection()

    def _continue_requested(self):
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

    def _save_story_selection_drafts(self):
        for checksum, (content, selected) in tuple(
            self._unsaved_story_selections.items()
        ):
            try:
                self.job_store.save_selection(content, selected)
            except (OSError, ValueError, PregenerationSetupError) as error:
                self.selection_status.setText(
                    f"Unable to save story selection: {error}. Check write access and retry."
                )
                return False
            del self._unsaved_story_selections[checksum]
        return True

    def _save_selection(self):
        if self._pack_result is not None:
            self.accept()
            return
        if self._awaiting_voice_confirmation:
            self._confirm_voice_plan()
            return
        if not self._generation_engine_available():
            self._selection_changed()
            return
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
        self._story_job_statuses = self.job_store.story_statuses(content)
        for selection_id in self._job.selected_story_ids:
            self._story_audio_checks.pop(self._story_audio_key(selection_id), None)
        self.planning_voices = True
        self.step.setText("Step 2 of 4 - Choose and confirm voices")
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
        if self._prepared_voice_job != job.job_id:
            self._prepared_voice_manifest = None
            self._prepared_voice_job = job.job_id
        manifest = self._prepared_voice_manifest
        if manifest is None and self.settings.voice_manifest:
            from vntts_artifacts.voice_manifest import load_voice_manifest

            from vntts.game_narrator import bind_game_narrator

            configured = load_voice_manifest(self.settings.voice_manifest)[0]
            narrator = configured.get("vntts.game_narrator")
            if narrator is not None:
                candidates = self.importer.prepare_voice_candidates(
                    job, self.voice_cancel_event, progress=self.decoderProgress.emit
                )
                if candidates is not None:
                    combined = bind_game_narrator(
                        self.settings,
                        self.settings.voice_manifest,
                        narrator["source_id"],
                        narrator["character"],
                        additional_manifest=candidates,
                    )
                    manifest = combined.voice_manifest
                    self._prepared_voice_manifest = manifest
        if (
            manifest is None
            and not self.settings.voice_manifest
            and find_default_voice_manifest() is None
        ):
            try:
                manifest = self.importer.prepare_voice_candidates(
                    job,
                    self.voice_cancel_event,
                    progress=self.decoderProgress.emit,
                )
            except GameContentImportCancelled as error:
                raise PregenerationVoiceCancelled(
                    "Voice candidate preparation was cancelled"
                ) from error
            self._prepared_voice_manifest = manifest
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

    def _decoder_progress(self, message):
        if self.planning_voices and not self._close_after_voice_cancel:
            self.resume_status.setText(message)

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
            self._preparation_paused("Voice matching paused", error)
            self.progress_cancel_consequence.setText(
                "Change the selection or choose Continue to retry."
            )
            if isinstance(error, PregenerationVoiceCancelled):
                if self._close_after_voice_cancel:
                    self.reject()
                else:
                    self.resume_status.setText("Voice matching cancelled.")
                return
            self._set_resume_error("Unable to match character voices", error)
            if isinstance(error, DecoderSetupRequired) and confirm_decoder_setup(
                self, error
            ):
                self.importer.allow_decoder_homebrew = True
                self._save_selection()
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
                "Review character voices",
                f"Check {audition_count} character voice sample"
                f"{'s' if audition_count != 1 else ''}.",
                "Cancel stops voice selection and closes this window. Reopen it "
                "to reuse the saved story selection and any completed choices.",
            )
            self.progress_bar.setRange(0, audition_count)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Voice choices remain")
            set_labeled_text(
                self.progress_counts,
                (
                    (
                        "Voice choices",
                        f"{audition_count} choice"
                        f"{'s' if audition_count != 1 else ''} require input before "
                        "generation.",
                    ),
                ),
            )
            return
        self.replanning_voice_decisions = False
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
        self._generation_input = None
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

    def _prepare_input_with_changes(self, job, plan):
        prepared = self.input_store.materialize(
            job, plan, cancellation=self.voice_cancel_event
        )
        changes = self.publisher.inspect_changes(job, prepared, self.voice_cancel_event)
        resources = (
            estimate_generation_resources(prepared)
            if isinstance(prepared, PregenerationInput)
            else None
        )
        return prepared, changes, resources

    def _generation_input_finished(self, prepared, error):
        self.preparing_inputs = False
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if self._close_after_voice_cancel:
            self.reject()
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
                    self.resume_status.setText("Offline preparation cancelled.")
                return
            self._set_resume_error("Unable to prepare generation", error)
            return
        self._generation_input, changes, resources = prepared
        if not isinstance(changes, OfflinePreparationChanges):
            self.selection_panel.show()
            self.resume_status.setText(
                "Unable to check the changes. Choose Continue to retry."
            )
            return
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
        self._show_voice_confirmation(self._voice_plan)

    def _start_generation(self):
        self.step.setText("Step 3 of 4 - Generate and check audio")
        self.voice_panel.shutdown()
        self.generating = True
        self._set_import_controls(False)
        self.cancel_button.setText("Cancel generation")
        self.cancel_button.setEnabled(True)
        self._start_generation_progress()
        self.generation_runner.start(
            self.generator.generate,
            self._generation_input,
            self._voice_plan,
            self.voice_cancel_event,
        )

    def _generation_finished(self, result, error):
        self._stop_generation_progress()
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
            self._preparation_paused("Generation paused", error)
            self._set_progress_timing(
                "Stopped. Counts show the last available progress report."
            )
            self.progress_cancel_consequence.setText(
                "Finished lines remain saved. Choose Continue to generate only "
                "unfinished lines, or Close to resume later."
            )
            if isinstance(error, OfflineGenerationCancelled):
                self.resume_status.setText(
                    "Generation cancelled. Continue later to resume saved lines."
                )
                return
            self._set_resume_error("Unable to generate offline audio", error)
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
        self.progress_timer.start()

    def _recovery_finished(self, result, error):
        self._stop_generation_progress()
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
            self._preparation_paused("Automatic recovery paused", error)
            self.progress_cancel_consequence.setText(
                "Finished lines remain saved. Choose Continue to retry unfinished "
                "lines, or Close to resume later."
            )
            if isinstance(error, OfflineGenerationCancelled):
                self.resume_status.setText(
                    "Automatic recovery cancelled. Continue later to resume saved lines."
                )
                return
            self._set_resume_error("Unable to recover offline audio", error)
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
            self._preparation_paused(
                "Automatic recovery paused",
                "Some lines still need safe automatic repair. Continue preparation to retry.",
            )
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
            self._preparation_paused("Final checks paused", error)
            if isinstance(error, OfflineGenerationCancelled):
                self.resume_status.setText(
                    "Final checks cancelled. Continue later to resume saved lines."
                )
                return
            self._set_resume_error("Unable to finish offline audio", error)
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
            self._preparation_paused("Final save paused", error)
            if isinstance(error, OfflineGenerationCancelled):
                self.resume_status.setText(
                    "Final save cancelled. Continue later to reuse prepared lines."
                )
                return
            self._set_resume_error("Unable to create offline game pack", error)
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
        if self._close_after_voice_cancel:
            self.source_status.setText("Game import cancelled.")
            self.source_status.show()
            self.reject()
            return
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_import_controls(True)
        if error is not None:
            self.source_status.setText(
                "Game import cancelled."
                if isinstance(error, GameContentImportCancelled)
                else f"Unable to import the installed game: {error}"
            )
            self.source_status.show()
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
        self.source_status.show()
        self.import_options_toggle.setChecked(False)
        self._update_import_options()

    def _set_import_controls(self, enabled):
        self._refresh_story_statuses()
        if enabled and not self.selection_panel.isHidden():
            show_terms = (
                self.game_narrator_chooser is None
                and self.settings.speech_backend == "pocket-tts"
            )
            self.pocket_voice_cloning.setVisible(show_terms)
            self.pocket_terms.setVisible(show_terms)
        self.engine_choice.setEnabled(enabled)
        self.model_choice.setEnabled(
            enabled and self.settings.speech_backend in {"coqui-xtts", "moss-tts"}
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
            and self.stories.currentItem() is not None
            and not self.coverage_runner.active
        )
        self.story_search.setEnabled(enabled)
        self.story_filter.setEnabled(enabled)
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

    def _cancel_or_reject(self):
        self._stop_generation_progress()
        if self.activating_saved:
            self.saved_pack_runner.cancel()
            self.activating_saved = False
            self.reject()
            return
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
        self._close_after_voice_cancel = True
        self.cancel_button.setEnabled(False)
        self.source_status.setText("Cancelling game import...")
        self.source_status.show()

    def closeEvent(self, event: QCloseEvent):
        self._stop_generation_progress()
        if self.activating_saved:
            self._cancel_or_reject()
            event.ignore()
            return
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
        if not self._confirm_selection_close():
            event.ignore()
            return
        self.import_runner.cancel()
        self.coverage_runner.cancel()
        self.saved_pack_runner.cancel()
        self.discovery_runner.cancel()
        self.voice_runner.cancel()
        self.input_runner.cancel()
        self.generation_runner.cancel()
        self.recovery_runner.cancel()
        self.acceptance_runner.cancel()
        self.publication_runner.cancel()
        self.progress_timer.stop()
        if self._narrator_player is not None:
            self._narrator_player.stop()
        self.voice_panel.shutdown()
        super().closeEvent(event)

    def done(self, result):
        if self._narrator_player is not None:
            self._narrator_player.stop()
        if self.has_pending_work():
            self._cancel_or_reject()
            return
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

    def _confirm_selection_close(self):
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

    def has_pending_work(self):
        return any(
            (
                self.importing,
                self.planning_voices,
                self.auditioning_voices,
                self.preparing_inputs,
                self.generating,
                self.recovering,
                self.accepting_audio,
                self.publishing_pack,
                self.activating_saved,
            )
        )


def _content_label(content):
    count = len(content.selections)
    return f"{content.display_name} - {count} {'story' if count == 1 else 'stories'}"


__all__ = ["OfflineAudioPreparationDialog"]
