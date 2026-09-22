"""Guided game narrator discovery, reference listening and synthesis preview."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from traceback import format_exception
from typing import Protocol, TypeGuard, runtime_checkable

from PySide6.QtCore import QSignalBlocker, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.file_integrity import sha256_file

from vntts.async_ui import LatestTaskRunner
from vntts.game_audio_decoder import DecoderSetupRequired, confirm_decoder_setup
from vntts.game_content_importer import Reverse1999GameImporter
from vntts.game_narrator import (
    OriginalReference,
    bind_voice_library_selection,
    load_original_reference,
    narrator_preview_plan,
)
from vntts.pregeneration_audition import (
    VoiceAuditionPreview,
    VoiceAuditionPreviewService,
)
from vntts.pregeneration_setup import GameContent, PregenerationJobStore
from vntts.pregeneration_voices import (
    VoiceDecisionStore,
    VoicePlan,
    pregeneration_narrator_source_id,
    resolve_pregeneration_settings,
    validated_player_voice_candidates,
)
from vntts.qt_audio import QtPcmPlayer
from vntts.release_backends import speech_backend_options
from vntts.settings import AppSettings
from vntts.speech_presentation import (
    compact_runtime_label,
    engine_model_label,
    speech_runtime_label,
)
from vntts.tts_benchmark import create_backend
from vntts.ui_text import copy_text_button, make_text_copyable
from vntts.voice_default_impact import StoryVoiceImpact, inspect_voice_default_impact
from vntts.voice_library import VoiceBinding, VoiceLibrary
from vntts.voices import (
    CharacterVoiceRegistry,
    application_voice_library,
    find_default_voice_manifest,
    is_narrator,
    normalize_character_name,
    pocket_tts_preset_voices,
    remember_voice_binding,
)

Binder = Callable[..., AppSettings]
ImpactContext = tuple[GameContent, PregenerationJobStore, VoiceDecisionStore]


def _is_story_voice_impact(
    result: object,
) -> TypeGuard[tuple[StoryVoiceImpact, ...]]:
    return isinstance(result, tuple) and all(
        isinstance(value, StoryVoiceImpact) for value in result
    )


@runtime_checkable
class _NarratorReference(Protocol):
    collection_title: str | None
    line_id: str
    text: str


def _is_string_tuple(value: object) -> TypeGuard[tuple[str, ...]]:
    return isinstance(value, tuple) and all(isinstance(item, str) for item in value)


def _is_narrator_references(
    value: object,
) -> TypeGuard[tuple[_NarratorReference, ...]]:
    return isinstance(value, tuple) and all(
        isinstance(item, _NarratorReference) for item in value
    )


def _display_role_name(role: str) -> str:
    """Use one display spelling for the role identity used by synthesis."""
    value = role.strip()
    if len(value) > 2 and value[0] == value[-1] == '"':
        return value[1:-1].strip()
    return value


def _candidate_origin_label(variant: dict[str, object]) -> str:
    origin = variant.get("candidate_origin")
    if origin == "exact_bank_unrouted_media":
        return "exact bank media"
    if origin == "story_line_route":
        return "story line"
    source_voice_ids = variant.get("source_voice_ids")
    if isinstance(source_voice_ids, list):
        return "exact bank media" if not source_voice_ids else "story line"
    return "prepared reference"


class GameNarratorDialog(QDialog):
    impactContextRequested = Signal()
    decoderProgress = Signal(str)
    settingsChanged = Signal(bool)

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        importer: Reverse1999GameImporter | None = None,
        preview_service: VoiceAuditionPreviewService | None = None,
        thread_pool: QThreadPool | None = None,
        player: QtPcmPlayer | None = None,
        binder: Binder = bind_voice_library_selection,
        voice_library: VoiceLibrary | None = None,
    ) -> None:
        super().__init__(parent)
        self._initialize_state(
            settings,
            importer,
            preview_service,
            binder,
            player,
            thread_pool,
            voice_library,
        )
        self._build_status_and_assignment_controls()
        self._build_voice_source_controls(settings)
        self._build_game_reference_controls(settings)
        self._build_preview_controls()
        layout = self._build_dialog_layout()
        self._build_impact_controls()
        self._build_actions(layout)
        self._connect_controls()
        self._finish_setup()

    def _initialize_state(
        self,
        settings: AppSettings,
        importer: Reverse1999GameImporter | None,
        preview_service: VoiceAuditionPreviewService | None,
        binder: Binder,
        player: QtPcmPlayer | None,
        thread_pool: QThreadPool | None,
        voice_library: VoiceLibrary | None,
    ) -> None:
        self.setWindowTitle("Narrator and character voices")
        self.resize(900, 560)
        self.settings_value = resolve_pregeneration_settings(settings)
        self._initial_settings_value = self.settings_value
        self._settings_dirty = False
        self.result_settings: AppSettings | None = None
        self.importer = importer or Reverse1999GameImporter()
        self.previews = preview_service or VoiceAuditionPreviewService(
            backend_factory=partial(
                create_backend, terms_accepted=settings.xtts_terms_accepted
            )
        )
        self.binder = binder
        self.voice_library = voice_library or application_voice_library()
        self._initial_voice_bindings = self.voice_library.bindings()
        self.player = player or QtPcmPlayer(self)
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._finished)
        self.cancellation = Event()
        self.decoderProgress.connect(self._decoder_progress)
        self._prepared: dict[str, Path] = {}
        self._candidate_source_ids: set[str] = set()
        self._preparing_character_candidates = False
        self._character: str | None = None
        self._operation: str | None = None
        self._warming_reference: str | None = None
        self._queued_action: str | None = None
        self._playback_requested = False
        self._closing = False
        self._closed = False
        self._preview_reused = False
        self._catalog_manifest = (
            settings.voice_manifest or find_default_voice_manifest()
        )
        self._catalog_registry = CharacterVoiceRegistry()
        self._voice_context: VoicePlan | None = None
        self._story_titles: tuple[str, ...] = ()
        self._saving_role = "Narrator"
        self._impact_context: ImpactContext | None = None
        self._loading_impact_context = False
        self._impact_results: tuple[StoryVoiceImpact, ...] | None = None
        self._impact_details = ""
        self._game_reference_dirty = False
        self._installation_root: Path | None = None
        self._recovery_role: str | None = None
        self._suppress_auto_discovery = False
        self.select_affected_after_save = False

    def _build_status_and_assignment_controls(self) -> None:
        self.status = QLabel("Choose a candidate. Nothing changes until you save.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Game narrator progress")
        self.runtime = QLabel()
        self.runtime.setWordWrap(True)
        self.runtime.setTextFormat(Qt.TextFormat.PlainText)
        self.runtime.setAccessibleName("Narrator preview compute device")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.controls = QWidget()
        form = QFormLayout(self.controls)
        self.form = form
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.context_note = QLabel()
        self.context_note.setWordWrap(True)
        self.context_note.setTextFormat(Qt.TextFormat.PlainText)
        self.context_note.setAccessibleName("Voice selection context")
        self.context_note.setStyleSheet("font-weight: 600;")
        self.context_note.hide()
        form.addRow(self.context_note)
        self.role = QComboBox()
        self.role.setAccessibleName("Narrator or character role")
        self.role.addItem("Narrator")
        form.addRow("Assign voice to", self.role)
        self.portrait = QLabel()
        self.portrait.setAccessibleName("Selected character portrait")
        self.role_summary = QLabel()
        self.role_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.role_summary.setWordWrap(True)
        self.role_summary.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        assignment = QHBoxLayout()
        assignment.addWidget(self.portrait)
        assignment.addWidget(self.role_summary, 1)
        form.addRow("Currently assigned", assignment)

    def _build_voice_source_controls(self, settings: AppSettings) -> None:
        form = self.form
        self.source = QComboBox()
        self.source.setAccessibleName("Voice source")
        self.source.addItem("Game character voice", "game")
        self.source.addItem("Built-in voice", "preset")
        self.source.addItem("Imported voice", "catalog")
        self.source.addItem("Choose automatically", "automatic")
        self.source.addItem("Use narrator's voice", "narrator")
        narrator_binding = self.voice_library.binding("Narrator")
        selected = (
            narrator_binding.source_id
            if narrator_binding is not None
            else pregeneration_narrator_source_id(
                settings, voice_library=self.voice_library
            )
        )
        if str(selected or "").startswith("preset:"):
            self.source.setCurrentIndex(1)
        form.addRow("Voice source", self.source)
        self.presets = QComboBox()
        self.presets.setAccessibleName("Built-in narrator candidate")
        for name in pocket_tts_preset_voices:
            self.presets.addItem(name.replace("_", " ").title(), f"preset:{name}")
        self.presets.setCurrentIndex(max(0, self.presets.findData(selected)))
        self.presets.currentIndexChanged.connect(self._stop_audio)
        form.addRow("Voice", self.presets)
        self.catalog_choice = QComboBox()
        self.catalog_choice.setAccessibleName("Imported character voice candidate")
        self.catalog_choice.currentIndexChanged.connect(lambda: self._stop_audio())
        self.catalog_original_button = QPushButton("Play original")
        self.catalog_original_button.setAccessibleName(
            "Play original imported voice reference"
        )
        self.catalog_original_button.clicked.connect(self._original)
        catalog_row = QHBoxLayout()
        catalog_row.addWidget(self.catalog_choice, 1)
        catalog_row.addWidget(self.catalog_original_button)
        self.catalog_row = catalog_row
        form.addRow("Voice", catalog_row)

    def _build_game_reference_controls(self, settings: AppSettings) -> None:
        form = self.form
        self.game_controls = QWidget()
        self.game_controls.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        game_form = QFormLayout(self.game_controls)
        self.game_form = game_form
        game_form.setContentsMargins(0, 0, 0, 0)
        game_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        game_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow(self.game_controls)
        self._build_game_terms_controls(settings)
        self._build_game_installation_controls(game_form)
        self._build_game_character_controls(game_form)
        self._build_game_reference_selection_controls(game_form)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        form.addRow(separator)

    def _build_game_terms_controls(self, settings: AppSettings) -> None:
        form = self.form
        self.terms = QLabel(
            "Game voice cloning with Pocket requires accepting the "
            '<a href="https://huggingface.co/kyutai/pocket-tts">model terms</a> '
            "and Hugging Face access on this computer. Built-in voices need neither."
        )
        self.terms.setWordWrap(True)
        self.terms.setOpenExternalLinks(True)
        self.consent = QCheckBox("I accepted Pocket's terms")
        self.consent.setChecked(settings.pocket_gated_model_accepted)
        self.consent.toggled.connect(self._update)
        pocket = self.settings_value.speech_backend == "pocket-tts"
        self.terms.setVisible(pocket)
        self.consent.setVisible(pocket)
        form.addRow(self.terms)
        form.addRow(self.consent)

    def _build_game_installation_controls(self, game_form: QFormLayout) -> None:
        self.game_installation = QLineEdit("Automatically detected")
        self.game_installation.setReadOnly(True)
        self.game_installation.setAccessibleName("Selected game installation")
        self._show_known_installation()
        self.discover_button = QPushButton("Refresh voices")
        self.discover_button.clicked.connect(lambda: self.discover())
        self.folder_button = QPushButton("Change folder...")
        self.folder_button.clicked.connect(self._choose_folder)
        installation = QHBoxLayout()
        installation.addWidget(self.game_installation, 1)
        installation.addWidget(self.folder_button)
        installation.addWidget(self.discover_button)
        game_form.addRow("Game installation", installation)

    def _build_game_character_controls(self, game_form: QFormLayout) -> None:
        self.characters = QComboBox()
        self.characters.setAccessibleName("Game character")
        self.characters.currentIndexChanged.connect(self._character_changed)
        game_form.addRow("Character voice", self.characters)

    def _build_game_reference_selection_controls(self, game_form: QFormLayout) -> None:
        self.references = QComboBox()
        self.references.setAccessibleName("Original game reference")
        self.references.currentIndexChanged.connect(self._reference_changed)
        self.original_button = QPushButton("Play original")
        self.original_button.setAccessibleName("Play original game reference")
        self.original_button.clicked.connect(self._original)
        reference = QHBoxLayout()
        reference.addWidget(self.references, 1)
        reference.addWidget(self.original_button)
        self.reference_row = reference
        game_form.addRow("Original reference", reference)
        self.reference_text = QLabel()
        self.reference_text.setWordWrap(True)
        self.reference_text.setTextFormat(Qt.TextFormat.PlainText)
        self.reference_text.setAccessibleName("Original reference transcript")
        game_form.addRow("Original transcript", self.reference_text)

    def _build_preview_controls(self) -> None:
        form = self.form
        self.text = QPlainTextEdit("The storm has passed. We can continue our journey.")
        self.text.setAccessibleName("Text to generate with the selected voice")
        self.text.setTabChangesFocus(True)
        line_height = self.text.fontMetrics().lineSpacing()
        self.text.setMinimumHeight(line_height * 3 + 16)
        self.text.setMaximumHeight(line_height * 5 + 20)
        self.preview_button = QPushButton("Generate preview")
        self.preview_button.setAccessibleName("Generate and play voice preview")
        self.preview_button.setToolTip(
            "Generate speech for this candidate, or replay its saved preview."
        )
        self.preview_button.clicked.connect(self._preview)
        action_width = max(
            button.sizeHint().width()
            for button in (
                self.catalog_original_button,
                self.original_button,
                self.preview_button,
            )
        )
        for button in (
            self.catalog_original_button,
            self.original_button,
            self.preview_button,
        ):
            button.setMinimumWidth(action_width)
        preview = QHBoxLayout()
        preview.addWidget(self.text, 1)
        preview.addWidget(self.preview_button, 0, Qt.AlignmentFlag.AlignTop)
        self.preview_row = preview
        form.addRow("Preview text", preview)
        self.save_button = QPushButton("Use this voice")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        self.stop_button = QPushButton("Stop audio")
        self.stop_button.clicked.connect(self._stop_audio)
        stop_policy = self.stop_button.sizePolicy()
        stop_policy.setRetainSizeWhenHidden(True)
        self.stop_button.setSizePolicy(stop_policy)
        self.stop_button.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)

    def _build_dialog_layout(self) -> QVBoxLayout:
        layout = QVBoxLayout(self)
        status_layout = QHBoxLayout()
        status_layout.addWidget(self.status, 1)
        status_layout.addWidget(self.stop_button)
        layout.addLayout(status_layout)
        layout.addWidget(self.runtime)
        layout.addWidget(self.progress)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.controls)
        layout.addWidget(self.scroll, 1)
        self.reference_details = QLabel()
        self.reference_details.setWordWrap(True)
        self.reference_details.setTextFormat(Qt.TextFormat.PlainText)
        self.reference_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.reference_details.setAccessibleName("Original audio source and checks")
        return layout

    def _build_impact_controls(self) -> None:
        form = self.form
        self.impact_note = QLabel(
            "The saved voice will be used for future speech and story preparation. "
            "Existing prepared audio will not change."
        )
        self.impact_note.setWordWrap(True)
        self.impact_note.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        form.addRow(self.impact_note)
        self.check_impact = QPushButton("Review stories to regenerate...")
        self.check_impact.clicked.connect(self._check_impact)
        self.impact_status = QLabel(
            "Open Stories to load prepared content for a voice comparison."
        )
        self.impact_status.hide()
        self.impact_status.setWordWrap(True)
        self.impact_status.setTextFormat(Qt.TextFormat.PlainText)
        self.impact_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.impact_status.setAccessibleName("Prepared stories affected by this voice")
        self.select_affected = QPushButton("Use voice and open affected stories")
        self.select_affected.clicked.connect(self._save_and_select_affected)
        self.select_affected.hide()
        form.addRow(self.check_impact)
        form.addRow(self.impact_status)
        form.addRow(self.select_affected)
        form.addItem(
            QSpacerItem(
                0,
                0,
                QSizePolicy.Policy.Minimum,
                QSizePolicy.Policy.Expanding,
            )
        )
        self.presets.currentIndexChanged.connect(self._clear_impact)
        self.catalog_choice.currentIndexChanged.connect(self._clear_impact)
        self.consent.toggled.connect(self._clear_impact)

    def _build_actions(self, layout: QVBoxLayout) -> None:
        actions = QHBoxLayout()
        self.copy_details = copy_text_button(
            "Copy diagnostics", self._copy_details, self
        )
        self.copy_details.setAccessibleName("Copy voice diagnostics")
        actions.addWidget(self.copy_details)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

    def _connect_controls(self) -> None:
        self.player.errorOccurred.connect(self._playback_error)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.mediaStatusChanged.connect(self._playback_media_changed)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.runtime_timer = QTimer(self)
        self.runtime_timer.setInterval(500)
        self.runtime_timer.timeout.connect(self._refresh_runtime)
        self.finished.connect(self.runtime_timer.stop)
        self.runtime_timer.start()
        self.role.currentTextChanged.connect(self._role_changed)

    def _finish_setup(self) -> None:
        self._initializing = True
        self.set_voice_context()
        self._initializing = False
        for choice in self.findChildren(QComboBox):
            choice.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            choice.setMinimumContentsLength(16)
            choice.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            choice.setToolTip(choice.currentText())
            choice.currentTextChanged.connect(choice.setToolTip)
        for button in (
            self.folder_button,
            self.discover_button,
            self.original_button,
            self.catalog_original_button,
            self.preview_button,
            self.check_impact,
            self.select_affected,
        ):
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.source.currentIndexChanged.connect(self._settings_choice_changed)
        self.presets.currentIndexChanged.connect(self._settings_choice_changed)
        self.catalog_choice.currentIndexChanged.connect(self._settings_choice_changed)
        self.consent.toggled.connect(self._settings_choice_changed)
        self.references.currentIndexChanged.connect(self._reference_choice_changed)
        tab_order = (
            self.role,
            self.source,
            self.presets,
            self.catalog_choice,
            self.catalog_original_button,
            self.game_installation,
            self.folder_button,
            self.discover_button,
            self.characters,
            self.references,
            self.original_button,
            self.consent,
            self.text,
            self.preview_button,
            self.check_impact,
            self.select_affected,
            self.copy_details,
            self.cancel_button,
            self.save_button,
        )
        for current, following in zip(tab_order, tab_order[1:], strict=False):
            QWidget.setTabOrder(current, following)
        make_text_copyable(self)
        outer_labels = [
            item.widget()
            for row in range(self.form.rowCount())
            if (item := self.form.itemAt(row, QFormLayout.ItemRole.LabelRole))
            is not None
            and item.widget() is not None
        ]
        label_width = max(label.sizeHint().width() for label in outer_labels)
        for row in range(self.game_form.rowCount()):
            item = self.game_form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item is not None and (label := item.widget()) is not None:
                label.setMinimumWidth(label_width)
        self._update()
        QTimer.singleShot(0, self._source_changed)

    def select_role(self, role: str) -> bool:
        """Select a trusted application-provided role without enabling free-form UI."""
        display_role = role.strip()
        identity = normalize_character_name(display_role)
        if not identity:
            return False
        index = next(
            (
                index
                for index in range(self.role.count())
                if normalize_character_name(self.role.itemText(index)) == identity
            ),
            -1,
        )
        if index < 0:
            self.role.addItem(display_role)
            index = self.role.count() - 1
        self.role.setCurrentIndex(index)
        return True

    def restore_initial_voice_bindings(self) -> None:
        """Restore bindings when the enclosing settings transaction fails."""
        self.voice_library.replace_bindings(self._initial_voice_bindings)

    def set_voice_context(
        self,
        plan: VoicePlan | None = None,
        character: str | None = None,
        *,
        roles: Sequence[str] = (),
        story_titles: Sequence[str] = (),
        impact_context: ImpactContext | None = None,
    ) -> None:
        """Use imported metadata without starting the reading engine."""
        if self.runner.active:
            return
        self._voice_context = plan
        self._impact_context = impact_context
        self._story_titles = tuple(story_titles)
        if plan is not None and plan.voice_manifest:
            self._catalog_manifest = plan.voice_manifest
        try:
            self._catalog_registry = (
                CharacterVoiceRegistry.from_file(self._catalog_manifest)
                if self._catalog_manifest
                else CharacterVoiceRegistry()
            )
        except (OSError, ValueError) as error:
            self.status.setText(f"Unable to load imported voices: {error}")
            self._catalog_registry = CharacterVoiceRegistry()
        self.catalog_choice.clear()
        for voice in sorted(
            self._catalog_registry.unique_voices(),
            key=lambda voice: voice.character.casefold(),
        ):
            self.catalog_choice.addItem(
                voice.source_character or voice.character,
                f"character:{normalize_character_name(voice.character)}",
            )
        selected = character or self.role.currentText() or "Narrator"
        available_roles = {
            *roles,
            *(binding.role for binding in self.voice_library.bindings()),
            *(
                voice.character
                for voice in self._catalog_registry.unique_voices()
                if not voice.character.startswith(("Game narrator ", "Game voice "))
            ),
            *((group.character for group in plan.groups) if plan is not None else ()),
            selected,
        }
        display_roles: dict[str, str] = {}
        for role in available_roles:
            identity = normalize_character_name(role)
            if not identity or is_narrator(role):
                continue
            display_roles.setdefault(identity, _display_role_name(role))
        with QSignalBlocker(self.role):
            self.role.clear()
            self.role.addItems(
                [
                    "Narrator",
                    *sorted(
                        display_roles.values(),
                        key=str.casefold,
                    ),
                ]
            )
            self.select_role(selected)
        self._role_changed()

    def set_story_impact_context(
        self,
        content: GameContent,
        jobs: PregenerationJobStore,
        decisions: VoiceDecisionStore,
    ) -> None:
        self._loading_impact_context = False
        self._impact_context = (content, jobs, decisions)
        self._clear_impact()
        self._update()

    def set_recovery_context(self, role: str, *, resume_live: bool) -> None:
        """Explain why the canonical editor opened for an unresolved live role."""
        display_role = role.strip() or "this character"
        self._recovery_role = normalize_character_name(display_role)
        self._suppress_auto_discovery = True
        if resume_live:
            message = (
                f"Live reading is paused because {display_role} has no assigned "
                "voice. Save a voice to resolve this character; reading resumes "
                "after any other waiting speakers are resolved. Back to recovery "
                "choices returns without saving; live reading stays paused."
            )
        else:
            message = (
                f"Speech is waiting because {display_role} has no assigned voice. "
                "Save a voice to resolve this character, or return to the recovery "
                "choices without saving."
            )
        self.context_note.setText(message)
        self.context_note.show()
        self.save_button.setText("Save voice")
        self.cancel_button.setText("Back to recovery choices")
        self.save_button.adjustSize()
        self.cancel_button.adjustSize()
        if self.voice_library.binding(display_role) is None:
            with QSignalBlocker(self.source):
                self.source.setCurrentIndex(self.source.findData("game"))
            self._source_changed()
            summary = "Not assigned - waiting for your choice"
            if self._story_titles:
                summary += "\nSelected stories: " + ", ".join(self._story_titles[:3])
            self.role_summary.setText(summary)

    def _role_changed(self) -> None:
        self._stop_audio()
        self._game_reference_dirty = False
        role = self.role.currentText().strip()
        if role == "???":
            self.role.setCurrentText("Narrator")
            return
        narrator = normalize_character_name(role) == "narrator"
        stale_game_references = (
            self.references.count() > 0
            and self._preparing_character_candidates == narrator
        )
        if stale_game_references:
            self._prepared.clear()
            self._candidate_source_ids.clear()
            self.references.clear()
        saved_binding, selected = self._saved_role_voice(role, narrator)
        self.role_summary.setText(
            self._role_summary_text(role, narrator, selected, saved_binding)
        )
        self._show_role_context(role)
        self._select_role_source(narrator, selected)
        self._source_changed()
        self._settings_choice_changed()
        if (
            stale_game_references
            and self.source.currentData() == "game"
            and self.characters.currentText()
            and not self.runner.active
            and not self._initializing
        ):
            self._prepare()

    def _saved_role_voice(
        self, role: str, narrator: bool
    ) -> tuple[VoiceBinding | None, str | None]:
        saved_binding = self.voice_library.binding(role)
        saved_evidence = (
            saved_binding.provenance.get("evidence")
            if saved_binding is not None
            else pregeneration_narrator_source_id(
                self.settings_value, voice_library=self.voice_library
            )
            if narrator
            else None
        )
        selected = (
            saved_binding.source_id
            or (
                saved_evidence.get("source_id")
                if isinstance(saved_evidence, dict)
                else None
            )
            or (
                "default"
                if saved_binding.route in {"narrator", "live-fallback"}
                else None
            )
            if saved_binding is not None
            else None
        )
        if selected is None and narrator:
            selected = pregeneration_narrator_source_id(
                self.settings_value, voice_library=self.voice_library
            )
        return saved_binding, selected

    def _role_summary_text(
        self,
        role: str,
        narrator: bool,
        selected: str | None,
        saved_binding: VoiceBinding | None,
    ) -> str:
        if (
            self._recovery_role == normalize_character_name(role)
            and saved_binding is None
        ):
            return "Not assigned - waiting for your choice"
        summary = self._source_label(selected)
        return (
            summary
            if narrator
            else f"{summary}\nOriginal and prepared recordings keep priority."
        )

    def _show_role_context(self, role: str) -> None:
        self.portrait.clear()
        self.portrait.hide()
        self._show_role_plan(role)
        self._show_story_titles()

    def _show_role_plan(self, role: str) -> None:
        if self._voice_context is None:
            return
        group = next(
            (
                group
                for group in self._voice_context.groups
                if normalize_character_name(group.character)
                == normalize_character_name(role)
            ),
            None,
        )
        if group is None:
            return
        self.role_summary.setText(
            self.role_summary.text()
            + f"\nPlanned: {self._source_label(group.source_id)}\nLines: {len(group.line_ids)}"
        )
        self._show_verified_portrait(group.portrait_image, group.portrait_image_sha256)

    def _show_verified_portrait(
        self, portrait_image: str | None, portrait_image_sha256: str | None
    ) -> None:
        if not portrait_image or not portrait_image_sha256:
            return
        try:
            if sha256_file(portrait_image) != portrait_image_sha256:
                return
            pixmap = QPixmap(portrait_image)
            if pixmap.isNull():
                return
            self.portrait.setPixmap(
                pixmap.scaled(
                    96,
                    96,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.portrait.show()
        except OSError:
            return

    def _show_story_titles(self) -> None:
        if self._story_titles:
            self.role_summary.setText(
                self.role_summary.text()
                + "\nSelected stories: "
                + ", ".join(self._story_titles[:3])
                + (
                    f" and {len(self._story_titles) - 3} more"
                    if len(self._story_titles) > 3
                    else ""
                )
            )
        self.role_summary.setToolTip("\n".join(self._story_titles))

    def _select_role_source(self, narrator: bool, selected: str | None) -> None:
        self.source.model().item(self.source.findData("automatic")).setEnabled(
            not narrator
        )
        self.source.model().item(self.source.findData("narrator")).setEnabled(
            not narrator
        )
        mode = "game"
        if not narrator and selected is None:
            mode = "automatic"
        elif not narrator and selected == "default":
            mode = "narrator"
        elif selected and selected.startswith("preset:"):
            with QSignalBlocker(self.presets):
                self.presets.setCurrentIndex(self.presets.findData(selected))
            mode = "preset"
        elif selected and self.catalog_choice.findData(selected) >= 0:
            with QSignalBlocker(self.catalog_choice):
                self.catalog_choice.setCurrentIndex(
                    self.catalog_choice.findData(selected)
                )
            mode = "catalog"
        with QSignalBlocker(self.source):
            self.source.setCurrentIndex(self.source.findData(mode))

    def _source_label(self, source_id: str | None) -> str:
        if source_id is None:
            return "automatic assignment"
        if source_id == "default":
            return "narrator fallback"
        if source_id.startswith("preset:"):
            return source_id.partition(":")[2].replace("_", " ").title()
        index = self.catalog_choice.findData(source_id)
        if index >= 0:
            return self.catalog_choice.itemText(index)
        index = self.references.findData(source_id)
        if index >= 0:
            return self.references.itemText(index)
        return (
            "Game voice"
            if source_id.startswith("character:")
            else "unavailable saved voice"
        )

    def saved_assignment_summary(self) -> str:
        """Describe the accepted role and voice for the enclosing dashboard."""
        role = self._saving_role or self.role.currentText().strip() or "Narrator"
        mode = self.source.currentData()
        voice = {
            "automatic": "automatic assignment",
            "narrator": "narrator fallback",
            "preset": self.presets.currentText(),
            "catalog": self.catalog_choice.currentText(),
            "game": self.characters.currentText(),
        }.get(mode, self.source.currentText())
        return f"{role}: {voice or 'selected voice'}"

    def _engine_details(self) -> str:
        settings = self._settings()
        return (
            str(
                engine_model_label(
                    settings.speech_backend,
                    settings.tts_model,
                    pocket_cloning=settings.pocket_gated_model_accepted,
                )
            )
            + f"\nBackend ID: {settings.speech_backend}"
            + f"\nConfigured model: {settings.tts_model or '(default)'}"
        )

    def _voice_details(self) -> str:
        role = self.role.currentText().strip() or "Narrator"
        source_id = (
            self.presets.currentData()
            if self.source.currentData() == "preset"
            else self.catalog_choice.currentData()
            if self.source.currentData() == "catalog"
            else self.references.currentData()
            if self.source.currentData() == "game"
            else self.source.currentData()
        )
        return (
            f"{self.role_summary.text()}\nRole: {role}\n"
            f"Candidate source: {source_id or '(none)'}"
        )

    def _reference_copy_text(self) -> str:
        asset = self.references.currentText()
        source_id = self.references.currentData() or "(none)"
        transcript = self.reference_text.text() or "Transcript unavailable."
        return f"{asset}\nSource ID: {source_id}\nTranscript: {transcript}"

    def _playback_copy_text(self) -> str:
        return "\n".join(
            value
            for value in (
                self.reference_details.text(),
                self.reference_details.toolTip(),
            )
            if value
        )

    def _impact_copy_text(self) -> str:
        return "\n\n".join(
            value
            for value in (self.impact_status.text(), self._impact_details)
            if value
        )

    def _copy_details(self) -> str:
        return "\n\n".join(
            value
            for value in (
                self.status.text(),
                self.runtime.toolTip() or self.runtime.text(),
                self._engine_details(),
                self._voice_details(),
                self._reference_copy_text(),
                self._playback_copy_text(),
                self._impact_copy_text(),
            )
            if value
        )

    def _reference_choice_changed(self, *_args: object) -> None:
        if self._initializing or self._closing or self._closed:
            return
        # Initial population is signal-blocked. A later selection changes what Save
        # would bind, without treating automatic reference loading as an edit.
        self._game_reference_dirty = bool(self.references.currentData())
        self._settings_choice_changed()

    def _settings_choice_changed(self, *_args: object) -> None:
        if self._initializing or self._closing or self._closed:
            return
        current = self._settings()
        initial = self._initial_settings_value
        changed = (
            current.pocket_gated_model_accepted != initial.pocket_gated_model_accepted
        )
        role = self.role.currentText().strip()
        narrator = normalize_character_name(role) == "narrator"
        binding = self.voice_library.binding(role)
        evidence = binding.provenance.get("evidence") if binding is not None else None
        saved = (
            binding.source_id
            or (evidence.get("source_id") if isinstance(evidence, dict) else None)
            or ("default" if binding.route in {"narrator", "live-fallback"} else None)
            if binding is not None
            else pregeneration_narrator_source_id(
                initial, voice_library=self.voice_library
            )
            if narrator
            else None
        )
        candidate = {
            "automatic": None,
            "narrator": "default",
            "preset": self.presets.currentData(),
            "catalog": self.catalog_choice.currentData(),
            "game": self.references.currentData()
            if self._game_reference_dirty
            else saved,
        }.get(self.source.currentData())
        changed = changed or candidate != saved
        if changed != self._settings_dirty:
            self._settings_dirty = changed
            self.settingsChanged.emit(changed)

    def _engine_available(self) -> bool:
        backend = self.settings_value.speech_backend
        return backend != "coqui-xtts" and any(
            option == backend and available
            for _label, option, available in speech_backend_options(backend)
        )

    def _engine_guidance_text(self) -> str:
        backend = self.settings_value.speech_backend
        if backend == "coqui-xtts":
            return (
                "XTTS is not supported for story preparation. "
                "Choose another engine in Settings."
            )
        if not self._engine_available():
            return (
                "This engine is not included in this package. "
                "Choose an available engine in Settings."
            )
        if self.source.currentData() == "preset" and backend != "pocket-tts":
            return (
                "Built-in voices require Pocket TTS. "
                "Choose a game voice or change the engine in Settings."
            )
        return ""

    def _source_changed(self) -> None:
        if self._closing or self._closed:
            return
        self._stop_audio()
        self._clear_impact()
        preset = self.source.currentData() == "preset"
        catalog = self.source.currentData() == "catalog"
        policy = self.source.currentData() in {"automatic", "narrator"}
        self.form.setRowVisible(self.presets, preset)
        self.form.setRowVisible(self.catalog_row, catalog)
        self.form.setRowVisible(self.game_controls, self.source.currentData() == "game")
        self.form.setRowVisible(self.preview_row, not policy)
        self.status.setText(
            "Save to restore automatic character matching. Existing recordings keep priority."
            if self.source.currentData() == "automatic"
            else "Missing recordings will use your saved narrator. Select Narrator above to preview that voice."
            if policy
            else "Choose an imported voice. Play its original or generate a preview before saving."
            if catalog
            else "Built-in voices need no game references or Hugging Face account. Generate a preview, then save."
            if preset
            else "Choose a game character and load an original reference."
        )
        self._update()
        if not self._engine_available() or (
            preset and self.settings_value.speech_backend != "pocket-tts"
        ):
            self.status.setText(self._engine_guidance_text())
        if (
            self.source.currentData() == "game"
            and not self.characters.count()
            and not self.runner.active
            and not self._initializing
            and not self._suppress_auto_discovery
        ):
            self.discover()

    def _settings(self) -> AppSettings:
        return self.settings_value.updated(
            pocket_gated_model_accepted=self.consent.isChecked(),
        )

    def _update(self) -> None:
        self._refresh_runtime()
        settings = self._settings()
        pocket = settings.speech_backend == "pocket-tts"
        preset = self.source.currentData() == "preset"
        catalog = self.source.currentData() == "catalog"
        policy = self.source.currentData() in {"automatic", "narrator"}
        available = self._engine_available()
        allowed = (
            policy
            or available
            and (pocket if preset else not pocket or self.consent.isChecked())
        )
        ready = (
            bool(self.catalog_choice.currentData())
            if catalog
            else preset or policy or self.references.count() > 0
        )
        idle = not self.runner.active and not self._closed and not self._closing
        warming = self.runner.active and self._operation == "warm"
        self.role.setEnabled(idle)
        self.catalog_choice.setEnabled(idle)
        self.source.model().item(self.source.findData("preset")).setEnabled(pocket)
        self.presets.setEnabled(idle)
        self.consent.setEnabled(idle)
        self.text.setEnabled(idle)
        self.terms.setVisible(pocket and not preset and not policy)
        self.consent.setVisible(pocket and not preset and not policy)
        self.characters.setEnabled(idle)
        self.source.setEnabled(idle)
        self.discover_button.setEnabled(idle)
        self.folder_button.setEnabled(idle)
        self.original_button.setEnabled(
            ready and not preset and not policy and (idle or warming)
        )
        self.original_button.setToolTip(
            "Built-in voices have no original game reference."
            if preset
            else "Play the original recording, not generated speech."
        )
        self.catalog_original_button.setEnabled(catalog and ready and idle)
        self.preview_button.setEnabled(
            ready and allowed and not policy and (idle or warming)
        )
        self.save_button.setEnabled(
            ready
            and allowed
            and bool(normalize_character_name(self.role.currentText()))
            and (idle or warming)
        )
        self.check_impact.setEnabled(
            not self._loading_impact_context
            and idle
            and ready
            and allowed
            and (
                self.source.currentData() != "game"
                or self.references.currentData() in self._prepared
            )
        )
        self.select_affected.setEnabled(idle and self.save_button.isEnabled())

    def _clear_impact(self, *_args: object) -> None:
        self._impact_results = None
        self._impact_details = ""
        self.select_affected_after_save = False
        self.select_affected.hide()
        self.impact_status.hide()
        self.impact_status.setText(
            "Check this selection against recorded voices in prepared stories."
            if self._impact_context is not None
            else "Check affected stories to load and compare prepared content."
        )
        self.impact_status.setToolTip("")

    def _check_impact(self) -> None:
        if not self.check_impact.isEnabled():
            return
        if self._impact_context is None:
            self._loading_impact_context = True
            self.impact_status.show()
            self.impact_status.setText("Loading prepared stories for comparison...")
            self._update()
            self.impactContextRequested.emit()
            return
        self._saving_role = self.role.currentText().strip()
        mode = self.source.currentData()
        manifest = (
            self._catalog_manifest
            if mode == "catalog"
            else self._prepared.get(self.references.currentData())
        )
        source_id = (
            self.catalog_choice.currentData()
            if mode == "catalog"
            else self.references.currentData()
            if mode == "game"
            and self.references.currentData() in self._candidate_source_ids
            else None
        )
        settings = self._settings()
        if mode == "preset":
            source_id = self.presets.currentData()
        elif mode == "narrator":
            source_id = "default"
        self._start(
            "impact",
            "Comparing prepared recordings with this voice selection...",
            self._perform_impact,
            settings,
            mode,
            manifest,
            source_id,
        )

    def _perform_impact(
        self,
        settings: AppSettings,
        mode: str,
        manifest: Path | str | None,
        source_id: str | None,
    ) -> tuple[StoryVoiceImpact, ...]:
        if self._impact_context is None:
            raise RuntimeError("Prepared-story context is unavailable")
        content, jobs, decisions = self._impact_context
        with TemporaryDirectory(prefix="vntts-voice-choice-") as temporary:
            proposed_library = self.voice_library.copy_to(
                Path(temporary) / "proposed-voices"
            )
            if mode == "automatic":
                proposed_library.clear(self._saving_role)
            elif mode == "narrator":
                proposed_library.select(
                    self._saving_role,
                    route=(
                        "live-fallback"
                        if is_narrator(self._saving_role)
                        else "narrator"
                    ),
                    method="manual",
                    evidence={"selected_in": "voice-impact-preview"},
                    algorithm="voice-picker-v1",
                )
            else:
                if source_id is None:
                    if manifest is None:
                        raise ValueError("Voice manifest is required")
                    choices = CharacterVoiceRegistry.from_file(manifest).choices()
                    if len(choices) != 1:
                        raise ValueError(
                            "Expected exactly one selected voice reference"
                        )
                    source_id = choices[0].id
                if mode in {"preset", "catalog"}:
                    registry = self._catalog_registry
                else:
                    if manifest is None:
                        raise ValueError("Voice manifest is required")
                    registry = CharacterVoiceRegistry.from_file(manifest)
                remember_voice_binding(
                    proposed_library,
                    registry,
                    self._saving_role,
                    source_id,
                    method="manual",
                    evidence={"selected_in": "voice-impact-preview"},
                    algorithm="voice-picker-v1",
                )
            results = inspect_voice_default_impact(
                content,
                jobs,
                decisions,
                settings,
                self._saving_role,
                current_voice_library=self.voice_library,
                proposed_voice_library=proposed_library,
                cancellation=self.cancellation,
            )
            if not _is_story_voice_impact(results):
                raise TypeError("Voice-impact service returned an invalid result")
            return results

    def _show_impact(self, results: tuple[StoryVoiceImpact, ...]) -> None:
        self._impact_results = results
        self.impact_status.show()
        affected = [value for value in results if value.changed_line_ids]
        lines = sum(len(value.changed_line_ids) for value in affected)
        details = [
            f"{value.title}: {len(value.changed_line_ids)} changed, {value.matching} same voice, "
            f"{value.original} originals kept, {value.unknown} recorded voice unknown, {value.needs_choice} need a voice choice."
            for value in results
        ]
        self._impact_details = "\n".join(details)
        self.impact_status.setText(
            (
                f"{lines} prepared lines in {len(affected)} stories would change voice. "
                + (
                    "Affected: "
                    + ", ".join(value.title for value in affected[:3])
                    + ". "
                    if affected
                    else ""
                )
                + "Existing audio stays playable until preparation succeeds."
            )
            if results
            else "No prepared stories found. This default applies to future preparation."
        )
        self.impact_status.setToolTip(self._impact_details)
        self.select_affected.setVisible(bool(affected))

    def _save_and_select_affected(self) -> None:
        if self._impact_results is None or not any(
            value.changed_line_ids for value in self._impact_results
        ):
            return
        self.select_affected_after_save = True
        self._save()

    def _refresh_runtime(self) -> None:
        if self._operation == "preview":
            message = (
                "Saved preview: no generation for this playback."
                if self._preview_reused
                else speech_runtime_label(getattr(self.previews, "backend", None))
            )
        else:
            message = "No preview generation. Original recordings play without TTS."
        self.runtime.setText(compact_runtime_label(message))
        self.runtime.setToolTip(message)
        self.runtime.setVisible(self._operation == "preview")

    def _start(
        self,
        operation: str,
        message: str,
        function: Callable[..., object],
        *arguments: object,
    ) -> None:
        if self.runner.active:
            return
        self.player.stop()
        self.cancellation.clear()
        self._operation = operation
        self._preview_reused = False
        self.status.setText(message)
        self.controls.setEnabled(operation == "warm")
        self.progress.show()
        self.cancel_button.setText("Cancel and close")
        self.cancel_button.adjustSize()
        self.runner.start(function, *arguments)
        self._update()

    def discover(self, installation_root: Path | None = None) -> None:
        if self.runner.active or self._closing:
            return
        if installation_root is not None:
            self._installation_root = installation_root
            self.game_installation.setText(str(installation_root))
            self.game_installation.setToolTip(str(installation_root))
        self.characters.clear()
        self._start(
            "discover",
            "Finding installed game voices. First import may take a few minutes...",
            self.importer.narrator_characters,
            self.cancellation,
            installation_root,
        )

    def _show_known_installation(self) -> None:
        selected = getattr(self.importer, "selected_installation_root", None)
        root = selected() if callable(selected) else None
        if not isinstance(root, (str, Path)):
            return
        self._installation_root = Path(root)
        self.game_installation.setText(str(self._installation_root))
        self.game_installation.setToolTip(str(self._installation_root))

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose installed Reverse: 1999 folder"
        )
        if path:
            self.discover(Path(path))

    def _character_changed(self) -> None:
        self.player.stop()
        self._prepared.clear()
        self._candidate_source_ids.clear()
        self.references.clear()
        self._game_reference_dirty = False
        self._clear_impact()
        self._update()
        self._settings_choice_changed()
        if (
            self.characters.currentText()
            and not self.runner.active
            and not self._initializing
        ):
            self._prepare()

    def _prepare(self) -> None:
        if self.runner.active or not self.characters.currentText():
            return
        self._character = self.characters.currentText()
        self._prepared.clear()
        self._candidate_source_ids.clear()
        self.references.clear()
        self._preparing_character_candidates = not is_narrator(self.role.currentText())
        if self._preparing_character_candidates:
            self._start(
                "prepare",
                f"Preparing voice candidates for {self._character}. Please wait...",
                partial(
                    self.importer.prepare_voice_roles,
                    (self._character,),
                    self.cancellation,
                    progress=self.decoderProgress.emit,
                    narrator=False,
                ),
            )
            return
        self._start(
            "prepare",
            f"Listing spoken references for {self._character}. Please wait...",
            self.importer.narrator_references,
            self._character,
        )

    def _show_character_candidates(self, manifest: Path | str) -> None:
        """List the exact sources published for Stories, without re-ranking them."""
        manifest_path = Path(manifest)
        registry, variants = validated_player_voice_candidates(manifest_path)
        candidate_details: list[tuple[str, dict[str, object]]] = []
        for variant in variants:
            if normalize_character_name(
                str(variant.get("character", ""))
            ) != normalize_character_name(self._character):
                continue
            voice_character = variant["voice_character"]
            source_id = f"character:{normalize_character_name(voice_character)}"
            if registry.resolve_source(source_id) is not None:
                candidate_details.append((source_id, variant))
        self.references.blockSignals(True)
        self.references.clear()
        self._candidate_source_ids.clear()
        self._prepared.clear()
        for index, (source_id, variant) in enumerate(candidate_details, 1):
            duration = variant.get("duration_seconds")
            duration_label = (
                f"{float(duration):.3f} s"
                if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                else "duration unavailable"
            )
            origin = _candidate_origin_label(variant)
            self.references.addItem(
                f"Candidate {index} - {duration_label} - {origin}", source_id
            )
            self.references.setItemData(
                index - 1,
                "\n".join(
                    (
                        f"Source: {variant.get('voice_character', source_id)}",
                        f"Duration: {duration_label}",
                        f"Origin: {origin}",
                        f"Source ID: {source_id}",
                    )
                ),
                Qt.ItemDataRole.ToolTipRole,
            )
            self._candidate_source_ids.add(source_id)
            self._prepared[source_id] = manifest_path
        _binding, selected_source = self._saved_role_voice(
            self.role.currentText(), narrator=False
        )
        selected_index = self.references.findData(selected_source)
        if selected_index >= 0:
            self.references.setCurrentIndex(selected_index)
        self.references.blockSignals(False)
        self.status.setText(
            f"{len(candidate_details)} prepared voice candidates from Stories. "
            "Short or unusable clips are omitted. "
            "The selected original audio prepares automatically."
            if candidate_details
            else "No usable voice candidates found. Choose another character."
        )
        self._reference_changed()

    def _reference_changed(self) -> None:
        self._stop_audio()
        self._clear_impact()
        self.reference_text.setText(
            self.references.currentData(Qt.ItemDataRole.ToolTipRole)
            or ("Transcript unavailable." if self.references.count() else "")
        )
        self._warm_selected()

    def _stop_audio(self) -> None:
        self.player.stop()
        self.stop_button.hide()
        self.reference_details.clear()
        self.reference_details.setToolTip("")
        if self._playback_requested:
            self.status.setText(
                "Original reference playback stopped."
                if self._operation == "audio"
                else "Preview playback stopped."
            )
        self._queued_action = None
        self._playback_requested = False

    def _warm_selected(self) -> None:
        reference = self.references.currentData()
        if (
            self._closing
            or self._closed
            or self.runner.active
            or self.source.currentData() != "game"
            or not reference
        ):
            return
        if reference in self._prepared:
            self.status.setText(
                "Original reference ready. Press Play original to listen."
            )
            queued = self._queued_action
            self._queued_action = None
            if queued:
                self._candidate_action(queued)
            return
        self._warming_reference = reference
        self._start(
            "warm",
            "Preparing selected audio... You can browse or press Play to queue playback.",
            self._perform_candidate_action,
            "warm",
            self._settings(),
            self._character,
            reference,
            self.text.toPlainText().strip(),
        )

    def _decoder_progress(self, message: str) -> None:
        if self.runner.active and not self._closing:
            self.status.setText(message)

    def _original(self) -> None:
        self._candidate_action("audio")

    def _preview(self) -> None:
        self._candidate_action("preview")

    def _candidate_action(self, operation: str) -> None:
        if operation != "audio" and (
            not self._engine_available()
            or self.source.currentData() == "preset"
            and self.settings_value.speech_backend != "pocket-tts"
        ):
            self.status.setText(self._engine_guidance_text())
            return
        if (
            operation != "audio"
            and self.source.currentData() in {"game", "catalog"}
            and self.settings_value.speech_backend == "pocket-tts"
            and not self.consent.isChecked()
        ):
            self.status.setText(
                "Accept Pocket's terms before generating or saving a game voice."
            )
            return
        if self.source.currentData() == "catalog":
            self._playback_requested = operation in {"audio", "preview"}
            self._start(
                operation,
                "Preparing the imported voice...",
                self._perform_catalog_action,
                operation,
                self._settings(),
                self.catalog_choice.currentData(),
                self.text.toPlainText().strip(),
            )
            return
        self._playback_requested = operation in {"audio", "preview"}
        if self.runner.active and self._operation == "warm":
            self._queued_action = operation
            self.status.setText(
                "Preparing audio... Playback will start when ready."
                if operation in {"audio", "preview"}
                else "Preparing audio... Your narrator will be saved when ready."
            )
            return
        self._start(
            operation,
            {
                "audio": "Preparing the selected original reference...",
                "preview": (
                    "Generating your preview..."
                    if self.source.currentData() == "preset"
                    else "Preparing the selected reference and generating your preview..."
                ),
                "save": "Saving the selected narrator. Character voices stay unchanged...",
            }[operation],
            self._perform_candidate_action,
            operation,
            self._settings(),
            self._character,
            self.presets.currentData()
            if self.source.currentData() == "preset"
            else self.references.currentData(),
            self.text.toPlainText().strip(),
        )

    def _perform_candidate_action(
        self,
        operation: str,
        settings: AppSettings,
        character: str | None,
        reference: str,
        text: str,
    ) -> AppSettings | OriginalReference | Path | VoiceAuditionPreview | None:
        manifest = None
        source_id = reference
        if not reference.startswith("preset:"):
            if character is None:
                raise ValueError("Choose a game character first")
            if reference not in self._prepared:
                self._prepared[reference] = self.importer.prepare_voice_roles(
                    (character,),
                    self.cancellation,
                    progress=self.decoderProgress.emit,
                    narrator=True,
                    narrator_line_id=reference,
                )
            manifest = self._prepared[reference]
            if reference not in self._candidate_source_ids:
                choices = CharacterVoiceRegistry.from_file(manifest).choices()
                if len(choices) != 1:
                    raise ValueError("Expected exactly one selected narrator reference")
                source_id = choices[0].id
        if self.cancellation.is_set():
            raise RuntimeError("Narrator selection cancelled")
        if operation == "warm":
            return manifest
        if operation == "save":
            return self._bind_selected_voice(settings, manifest, source_id, character)
        if operation == "audio":
            return load_original_reference(manifest, source_id)
        plan = narrator_preview_plan(settings, manifest, source_id, text)
        return self.previews.generate(
            plan,
            plan.groups[0],
            plan.groups[0].source_id,
            cancel_event=self.cancellation,
            progress=self.decoderProgress.emit,
        )

    def _bind_selected_voice(
        self,
        settings: AppSettings,
        manifest: Path | str | None,
        source_id: str,
        character: str | None,
        *,
        root: Path | str | None = None,
    ) -> AppSettings:
        role = self._saving_role
        context: dict[str, str | Path] = (
            {"additional_manifest": self._voice_context.voice_manifest}
            if self._voice_context is not None and self._voice_context.voice_manifest
            else {}
        )
        if root is not None:
            context["root"] = root
        if normalize_character_name(role) == "narrator":
            return self.binder(settings, manifest, source_id, character, **context)
        return self.binder(
            settings,
            manifest,
            source_id,
            character,
            target_character=role,
            **context,
        )

    def _perform_catalog_action(
        self, operation: str, settings: AppSettings, source_id: str, text: str
    ) -> AppSettings | OriginalReference | VoiceAuditionPreview:
        voice = self._catalog_registry.resolve_source(source_id)
        if voice is None:
            raise ValueError(f"Unknown voice source {source_id!r}")
        if operation == "save":
            return self._bind_selected_voice(
                settings,
                self._catalog_manifest,
                source_id,
                voice.source_character or voice.character,
            )
        if operation == "audio":
            return load_original_reference(self._catalog_manifest, source_id)
        plan = narrator_preview_plan(settings, self._catalog_manifest, source_id, text)
        return self.previews.generate(
            plan,
            plan.groups[0],
            source_id,
            cancel_event=self.cancellation,
            progress=self.decoderProgress.emit,
        )

    def _policy_settings(self, settings: AppSettings) -> AppSettings:
        role = self.role.currentText().strip()
        narrator = normalize_character_name(role) == "narrator"
        policy = self.source.currentData()
        if policy == "automatic":
            self.voice_library.clear(role)
        else:
            remember_voice_binding(
                self.voice_library,
                self._catalog_registry,
                role,
                ("default" if policy == "narrator" else self.presets.currentData()),
                method="manual",
                evidence={"selected_in": "voice-picker"},
                algorithm="voice-picker-v1",
            )
        return settings.updated(
            tts_speaker_wav=None if narrator else settings.tts_speaker_wav,
        )

    def _save(self) -> None:
        if not self.save_button.isEnabled():
            return
        self._saving_role = self.role.currentText().strip()
        if self.source.currentData() in {"preset", "automatic", "narrator"}:
            self.result_settings = self._policy_settings(self._settings())
            self._cleanup()
            return
        self._candidate_action("save")

    def _finished(self, result: object, error: Exception | None) -> None:
        operation = self._operation
        if operation == "close":
            self._closed = True
            if self.result_settings is not None and not self._closing:
                super().accept()
            else:
                super().reject()
            return
        if self._closing:
            self._cleanup()
            return
        self.controls.setEnabled(True)
        self.progress.hide()
        self.cancel_button.setText(
            "Back to recovery choices" if self._recovery_role else "Cancel"
        )
        self.cancel_button.adjustSize()
        if operation == "warm":
            if self.references.currentData() != self._warming_reference:
                self._warm_selected()
                self._update()
                return
            queued = self._queued_action
            self._queued_action = None
            if error is None:
                self.status.setText(
                    "Original reference ready. Press Play original to listen."
                )
                if queued:
                    self._candidate_action(queued)
            elif isinstance(error, DecoderSetupRequired):
                # Browsing must not open an installation prompt without a user action.
                self.status.setText(
                    "Audio decoder setup needed. Press Play to set it up."
                )
                if queued and confirm_decoder_setup(self, error):
                    self.importer.allow_decoder_homebrew = True
                    self._candidate_action(queued)
            else:
                self.status.setText(
                    f"{error}\nPress Play to retry or choose another reference."
                )
            self._update()
            return
        if error is not None:
            from vntts.support import record_game_import

            record_game_import(
                "voice-dialog",
                outcome="failed",
                command_kind=operation,
                exception_type=type(error).__name__,
                reason=str(error),
                traceback_tail="".join(format_exception(error))[-12000:],
            )
            if isinstance(error, DecoderSetupRequired) and confirm_decoder_setup(
                self, error
            ):
                self.importer.allow_decoder_homebrew = True
                if operation is not None:
                    self._candidate_action(operation)
                return
            self.status.setText(
                (
                    f"{error}\nNothing was assigned. Select Generate preview to "
                    "try again, or Cancel to leave.\n"
                    if operation == "preview"
                    else f"{error}\nRetry, choose a game folder, or cancel. "
                    "Nothing was assigned.\n"
                )
                + "Failure details: Support and logs > Export support report."
            )
        elif operation == "impact":
            if not _is_story_voice_impact(result):
                raise TypeError("Voice-impact service returned an invalid result")
            self._show_impact(result)
            self.status.setText(
                "Voice comparison complete. Nothing has been saved or generated."
            )
        elif operation == "discover":
            if not _is_string_tuple(result):
                raise TypeError("Game importer returned invalid character names")
            with QSignalBlocker(self.characters):
                self.characters.addItems(result)
            self._show_known_installation()
            self.status.setText(
                "Choose a character to load its voice references."
                if result
                else "No voiced characters found. Choose the game folder to reimport."
            )
            if result:
                self._prepare()
                return
        elif operation == "prepare":
            if self._preparing_character_candidates:
                if not isinstance(result, (Path, str)):
                    raise TypeError("Game importer returned an invalid voice manifest")
                self._show_character_candidates(result)
            else:
                if not _is_narrator_references(result):
                    raise TypeError(
                        "Game importer returned invalid narrator references"
                    )
                choices = result
                self.references.blockSignals(True)
                self.references.clear()
                for index, choice in enumerate(choices, 1):
                    title = choice.collection_title or f"Reference {index}"
                    self.references.addItem(title, choice.line_id)
                    self.references.setItemData(
                        index - 1, choice.text, Qt.ItemDataRole.ToolTipRole
                    )
                self.references.blockSignals(False)
                self.status.setText(
                    f"All {len(choices)} suitable references, recommended order. "
                    "The selected original audio prepares automatically."
                    if choices
                    else "No usable references found. Choose another character."
                )
                self._reference_changed()
        elif operation in {"audio", "preview"}:
            preview_path: object | None = None
            if operation == "preview":
                preview_path = getattr(result, "path", None)
                if preview_path is None:
                    raise TypeError("Preview service returned an invalid preview")
                self._preview_reused = getattr(result, "reused", False) is True
            if not self._playback_requested:
                self.status.setText("Audio ready. Playback stopped.")
                self._update()
                return
            self.status.setText("Starting playback...")
            if operation == "audio":
                if not isinstance(result, OriginalReference):
                    raise TypeError("Reference loader returned an invalid reference")
                check = (
                    "Not suitable for cloning: " + ", ".join(result.rejection_reasons)
                    if result.rejection_reasons
                    else "Technical reference checks passed; voice quality is yours to judge."
                )
                self.reference_details.setText(
                    f"Original: {result.character} | {result.duration_seconds:.3f} s\n{check}"
                )
                self.reference_details.setToolTip(
                    f"{result.source_id}\n{result.path}\nSHA-256: {result.sha256}"
                )
                self.player.play_bytes(result.payload, source=str(result.path))
            else:
                self.player.setSource(QUrl.fromLocalFile(str(preview_path)))
                self.player.play()
        elif operation == "save":
            if not isinstance(result, AppSettings):
                raise TypeError("Voice binding returned invalid settings")
            self.result_settings = result
            self._cleanup()
            return
        self._update()

    def _playback_state_changed(self, state: object) -> None:
        if self.runner.active:
            return
        playing = (
            state == QMediaPlayer.PlaybackState.PlayingState
            and self._playback_requested
        )
        self.stop_button.setVisible(playing)
        if playing:
            self.status.setText(
                "Playing original reference."
                if self._operation == "audio"
                else "Playing saved preview (no generation)."
                if self._preview_reused
                else "Playing generated preview."
            )

    def _playback_error(self, _code: object, message: str) -> None:
        if self.runner.active or not self._playback_requested:
            return
        self._playback_requested = False
        self.stop_button.hide()
        subject = (
            "Original reference"
            if self._operation == "audio"
            else "Preview"
            if self._operation == "preview"
            else "Audio"
        )
        self.status.setText(f"{subject} playback failed: {message}")

    def _playback_media_changed(self, status: object) -> None:
        if (
            not self.runner.active
            and status == QMediaPlayer.MediaStatus.EndOfMedia
            and self._playback_requested
        ):
            self.status.setText(
                "Original reference finished. Press Play original to listen again."
                if self._operation == "audio"
                else "Preview finished. Press Generate preview to listen again."
            )
            self._playback_requested = False
            self.stop_button.hide()

    def _cleanup(self) -> None:
        self._start("close", "Closing voice preview...", self.previews.close)

    def reject(self) -> None:
        if self._closed:
            super().reject()
            return
        self._closing = True
        self.player.stop()
        self.cancellation.set()
        self.previews.cancel()
        if self.runner.active:
            self.status.setText(
                "Cancelling. Waiting for the current operation to stop..."
            )
        else:
            self._cleanup()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closed:
            super().closeEvent(event)
        else:
            event.ignore()
            self.reject()
