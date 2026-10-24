"""Role/source host for the shared character voice editor."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from threading import Event
from typing import cast

from PySide6.QtCore import QSignalBlocker, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QPixmap, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.voice_manifest import write_voice_manifest

from vntts.action_icons import set_action_icon
from vntts.async_ui import LatestTaskRunner
from vntts.document_identity import sha256_file
from vntts.game_audio_decoder import DecoderSetupRequired, confirm_decoder_setup
from vntts.game_content_importer import Reverse1999GameImporter
from vntts.game_narrator import bind_voice_library_selection, narrator_preview_plan
from vntts.pregeneration_audition import (
    COMPARISON_PREVIEW_TEXT,
    VoiceAuditionPreviewService,
)
from vntts.pregeneration_audition_queue import VoiceAuditionSession
from vntts.pregeneration_audition_ui import VoiceAuditionPanel, VoiceAuditionUIError
from vntts.pregeneration_voices import (
    PregenerationVoiceError,
    VoiceCandidate,
    VoiceGroup,
    VoicePlan,
    pregeneration_narrator_source_id,
    resolve_pregeneration_settings,
    validated_player_voice_candidates,
)
from vntts.qt_audio import QtPcmPlayer
from vntts.release_backends import speech_backend_options
from vntts.settings import AppSettings
from vntts.tts_benchmark import create_backend
from vntts.ui_text import copy_text_button, make_text_copyable
from vntts.voice_library import VoiceBinding, VoiceBindingRollback, VoiceLibrary
from vntts.voices import (
    CharacterVoiceRegistry,
    application_voice_library,
    find_default_voice_manifest,
    is_narrator,
    normalize_character_name,
    pocket_tts_preset_voices,
    read_voice_reference_bytes,
    registry_with_voice_library,
    remember_voice_binding,
    voice_binding_label,
    voice_binding_source_id,
    voice_source_label,
)

Binder = Callable[..., AppSettings]
CurrentVoice = tuple[VoicePlan, str, tuple[tuple[str, VoiceBinding | None], ...]]


def _display_role_name(role: str) -> str:
    value = role.strip()
    return (
        value[1:-1].strip()
        if len(value) > 2 and value[0] == value[-1] == '"'
        else value
    )


class GameNarratorDialog(QDialog):
    decoderProgress = Signal(str)
    settingsChanged = Signal(bool)
    busyChanged = Signal(bool)

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        importer: Reverse1999GameImporter | None = None,
        preview_service: VoiceAuditionPreviewService
        | VoiceAuditionSession
        | None = None,
        thread_pool: QThreadPool | None = None,
        player: QtPcmPlayer | None = None,
        binder: Binder = bind_voice_library_selection,
        voice_library: VoiceLibrary | None = None,
        voice_binding_rollback: VoiceBindingRollback | None = None,
        use_offline_engine: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Narrator and character voices")
        self.resize(900, 680)
        self._initialize_state(
            settings,
            importer,
            thread_pool,
            binder,
            voice_library,
            voice_binding_rollback,
            use_offline_engine,
        )
        self.status = QLabel()
        self.status.hide()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.controls = QWidget()
        content = QVBoxLayout(self.controls)
        content.setAlignment(Qt.AlignmentFlag.AlignTop)
        content.setContentsMargins(8, 8, 8, 8)
        content.setSpacing(8)
        self._build_voice_sources(content)
        self.voice_panel = VoiceAuditionPanel(
            self,
            preview_service=preview_service
            or VoiceAuditionPreviewService(
                backend_factory=partial(
                    create_backend, terms_accepted=settings.xtts_terms_accepted
                )
            ),
            thread_pool=thread_pool,
            player=player,
            parent=self,
        )
        self.voice_panel.completed.connect(self._saved)
        self.voice_panel.cancelled.connect(self._panel_finished)
        self.voice_panel.activeChanged.connect(self._activity_changed)
        self.voice_panel.selectionChanged.connect(self._selection_changed)
        self.voice_panel.beforeSave.connect(self._capture_save)
        panel_layout = self.voice_panel.layout()
        assert panel_layout is not None
        panel_layout.removeWidget(self.voice_panel.phrase_box)
        content.addWidget(self.voice_panel.phrase_box)
        content.addWidget(self.new_voice_box)
        panel_layout.removeWidget(self.voice_panel.current_voice_box)
        content.addWidget(self.voice_panel.current_voice_box)
        self.form.addRow(self.voice_panel)
        self._build_editor_actions(content)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.controls)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        layout.addWidget(scroll, 1)
        self._connect_voice_controls()

    def _connect_voice_controls(self) -> None:
        self.source.currentIndexChanged.connect(self._source_changed)
        self.role.currentTextChanged.connect(self._role_changed)
        self.presets.currentIndexChanged.connect(self._source_changed)
        self.catalog_choice.currentIndexChanged.connect(self._source_changed)
        self.consent.toggled.connect(self._consent_changed)
        self.decoderProgress.connect(self._set_status)
        self._initializing = True
        self.set_voice_context()
        self._initializing = False
        for combo in (
            self.role,
            self.source,
            self.presets,
            self.catalog_choice,
            self.characters,
        ):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(10)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        make_text_copyable(self)
        QTimer.singleShot(0, self, self._role_changed)

    def _initialize_state(
        self,
        settings: AppSettings,
        importer: Reverse1999GameImporter | None,
        thread_pool: QThreadPool | None,
        binder: Binder,
        voice_library: VoiceLibrary | None,
        voice_binding_rollback: VoiceBindingRollback | None,
        use_offline_engine: bool,
    ) -> None:
        self.settings_value = (
            resolve_pregeneration_settings(settings) if use_offline_engine else settings
        )
        self._initial_settings_value = self.settings_value
        self.result_settings: AppSettings | None = None
        self.voice_library = voice_library or application_voice_library()
        self._voice_binding_rollback = voice_binding_rollback
        self._owns_voice_binding_rollback = voice_binding_rollback is None
        self.binder = binder
        self.importer = importer or Reverse1999GameImporter()
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._finished)
        self.runner.activeChanged.connect(self._activity_changed)
        self.current_voice_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.current_voice_runner.finished.connect(self._current_voice_finished)
        self.current_voice_runner.activeChanged.connect(self._activity_changed)
        self._current_voice_label = ""
        self._pending_current_voice: Callable[[], CurrentVoice] | None = None
        self.cancellation = Event()
        self._operation: str | None = None
        self._closing = self._closed = self._initializing = False
        self._panel_cancelled = False
        self._busy = self._settings_dirty = False
        self._character: str | None = None
        self._installation_root: Path | None = None
        self._catalog_manifest = (
            settings.voice_manifest or find_default_voice_manifest()
        )
        self._catalog_registry = CharacterVoiceRegistry()
        self._voice_context: VoicePlan | None = None
        self._story_titles: tuple[str, ...] = ()
        self._saving_role = "Narrator"
        self._saved_source: str | None = None
        self._narrator_binding: VoiceBinding | None = None
        self._saved_voice_binding: VoiceBinding | None = None
        self._recovery_role: str | None = None
        self._recovery_resume_live = self._suppress_auto_discovery = False
        self._candidate_manifest: Path | str | None = None
        self._narrator_directory = TemporaryDirectory(prefix="vntts-narrator-editor-")

    def _build_voice_sources(self, content: QVBoxLayout) -> None:
        self.new_voice_box = QGroupBox("New voice")
        self.new_voice_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self.form = QFormLayout(self.new_voice_box)
        self.form.setContentsMargins(8, 6, 8, 6)
        self.form.setVerticalSpacing(4)
        self.form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.context_note = QLabel()
        self.context_note.setWordWrap(True)
        self.context_note.hide()
        content.addWidget(self.context_note)
        self.role = QComboBox()
        self.role.setAccessibleName("Narrator or character role")
        self.portrait = QLabel()
        self.role_summary = QLabel()
        self.role_summary.setWordWrap(True)
        self.role_summary.hide()
        row = QHBoxLayout()
        row.addWidget(QLabel("Voice for"))
        row.addWidget(self.portrait)
        row.addWidget(self.role, 1)
        content.addLayout(row)
        self.source = QComboBox()
        self.source.setAccessibleName("Voice source")
        for label, mode in (
            ("Game character voice", "game"),
            ("Built-in voice", "preset"),
            ("Imported voice", "catalog"),
            ("Saved voice", "saved"),
            ("Choose automatically", "automatic"),
            ("Use narrator's voice", "narrator"),
        ):
            self.source.addItem(label, mode)
        self.form.addRow("Voice source", self.source)
        self.presets = QComboBox()
        self.presets.setAccessibleName("Built-in narrator candidate")
        for name in pocket_tts_preset_voices:
            self.presets.addItem(name.replace("_", " ").title(), f"preset:{name}")
        self.form.addRow("Voice", self.presets)
        self.catalog_choice = QComboBox()
        self.catalog_choice.setAccessibleName("Imported character voice candidate")
        self.form.addRow("Voice", self.catalog_choice)
        self._build_game_reference_controls()

    def _build_editor_actions(self, content: QVBoxLayout) -> None:
        self.cancel_button = QPushButton("Cancel")
        set_action_icon(self.cancel_button, "x")
        self.cancel_button.clicked.connect(self.reject)
        self.copy_details = copy_text_button(
            "Copy diagnostics", self._copy_details, self
        )
        actions = QHBoxLayout()
        self.copy_details.hide()
        self.voice_panel.reference_details_toggle.toggled.connect(
            self.copy_details.setVisible
        )
        self._build_details_controls()
        self.voice_panel.outcome_actions.removeWidget(
            self.voice_panel.retry_save_button
        )
        actions.addWidget(self.voice_panel.retry_save_button)
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        self.voice_panel.a_use.setText("Save")
        self.voice_panel.outcome_actions.removeWidget(self.voice_panel.a_use)
        actions.addWidget(self.voice_panel.a_use)
        panel_layout = self.voice_panel.layout()
        assert panel_layout is not None
        panel_layout.removeItem(self.voice_panel.outcome_actions)
        content.addLayout(actions)

    @property
    def active(self) -> bool:
        return (
            self.runner.active
            or self.current_voice_runner.active
            or self.voice_panel.active
        )

    def _set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _activity_changed(self, *_args: object) -> None:
        busy = self.active
        if busy != self._busy:
            self._busy = busy
            self.busyChanged.emit(busy)
        self._update()
        if self._closing:
            QTimer.singleShot(0, self, self._finish_close)

    def _build_game_reference_controls(self) -> None:
        self.terms = QLabel(
            "Game voice cloning with Pocket requires accepting the "
            '<a href="https://huggingface.co/kyutai/pocket-tts">model terms</a> '
            "and Hugging Face access. Built-in voices need neither."
        )
        self.terms.setWordWrap(True)
        self.terms.setOpenExternalLinks(True)
        self.consent = QCheckBox("I accepted Pocket's terms")
        self.consent.setChecked(self.settings_value.pocket_gated_model_accepted)
        self.form.addRow(self.terms)
        self.form.addRow(self.consent)
        self.game_controls = QWidget()
        game_form = QFormLayout(self.game_controls)
        game_form.setContentsMargins(0, 0, 0, 0)
        game_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.source_setup = QCheckBox("Game source setup")
        setup = QWidget()
        setup_layout = QHBoxLayout(setup)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        self.game_installation = QLineEdit("Automatically detected")
        self.game_installation.setReadOnly(True)
        self._show_known_installation()
        self.discover_button = QPushButton("Refresh")
        self.discover_button.setAccessibleName("Refresh voices")
        self.discover_button.setToolTip("Refresh voices")
        set_action_icon(self.discover_button, "refresh-cw")
        self.discover_button.clicked.connect(lambda: self.discover())
        self.folder_button = QPushButton("Folder...")
        self.folder_button.setAccessibleName("Change folder...")
        self.folder_button.setToolTip("Change folder...")
        set_action_icon(self.folder_button, "folder-open")
        self.folder_button.clicked.connect(self._choose_folder)
        setup_layout.addWidget(self.game_installation, 1)
        setup_layout.addWidget(self.folder_button)
        setup_layout.addWidget(self.discover_button)
        setup.hide()
        self.source_setup.toggled.connect(setup.setVisible)
        self.characters = QComboBox()
        self.characters.setAccessibleName("Game voice donor")
        self.characters.currentIndexChanged.connect(self._character_changed)
        donor = QHBoxLayout()
        donor.addWidget(self.characters, 1)
        donor.addWidget(self.source_setup)
        game_form.addRow("Donor", donor)
        game_form.addRow(setup)
        self.form.addRow(self.game_controls)

    def _build_details_controls(self) -> None:
        panel_layout = self.voice_panel.layout()
        assert panel_layout is not None
        panel_layout.removeWidget(self.voice_panel.reference_details_toggle)
        tools = QHBoxLayout()
        tools.addWidget(self.voice_panel.reference_details_toggle)
        tools.addWidget(self.copy_details)
        tools.addStretch()
        self.form.addRow(tools)

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
        rollback = self._voice_binding_rollback
        if rollback is not None and not self.voice_library.rollback_bindings(rollback):
            raise ValueError(
                "Voice bindings changed concurrently; rollback was skipped"
            )

    def set_voice_context(
        self,
        plan: VoicePlan | None = None,
        character: str | None = None,
        *,
        roles: Sequence[str] = (),
        story_titles: Sequence[str] = (),
    ) -> None:
        """Hydrate role metadata atomically before selecting its source."""
        if self.runner.active:
            return
        self._voice_context = plan
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
            self._set_status(f"Unable to load imported voices: {error}")
            self._catalog_registry = CharacterVoiceRegistry()
        with QSignalBlocker(self.catalog_choice):
            self.catalog_choice.clear()
            for voice in sorted(
                self._catalog_registry.unique_voices(),
                key=lambda voice: voice.character.casefold(),
            ):
                self.catalog_choice.addItem(
                    voice_source_label(voice.source_character or voice.character),
                    f"character:{normalize_character_name(voice.character)}",
                )
        selected = character or self.role.currentText() or "Narrator"
        available = {
            *roles,
            *(binding.role for binding in self.voice_library.bindings()),
            *(
                voice.character
                for voice in self._catalog_registry.unique_voices()
                if not voice.character.startswith(("Game narrator ", "Game voice "))
                and voice_source_label(voice.character) == voice.character
            ),
            *(
                (group.routing_role or group.character for group in plan.groups)
                if plan
                else ()
            ),
            selected,
        }
        display_roles: dict[str, str] = {}
        for role in available:
            identity = normalize_character_name(role)
            if identity and not is_narrator(role):
                display_roles.setdefault(identity, _display_role_name(role))
        with QSignalBlocker(self.role):
            self.role.clear()
            self.role.addItems(
                ["Narrator", *sorted(display_roles.values(), key=str.casefold)]
            )
            self.select_role(selected)
        self._role_changed()

    def _saved_role_voice(
        self, role: str, narrator: bool
    ) -> tuple[VoiceBinding | None, str | None]:
        saved_binding = self.voice_library.binding(
            role, variant_key=self.voice_library.linked_variant_key(role)
        )
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
            return "No saved voice (narrator fallback)"
        summary = voice_binding_label(saved_binding) or self._source_label(selected)
        return f"Saved: {summary}" if saved_binding is not None or narrator else summary

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
                if normalize_character_name(group.routing_role or group.character)
                == normalize_character_name(role)
            ),
            None,
        )
        if group is None:
            return
        if all(
            value.portrait_image and value.portrait_image_sha256
            for value in self._voice_context.groups
            if normalize_character_name(value.character) != "narrator"
        ):
            self._show_verified_portrait(
                group.portrait_image, group.portrait_image_sha256
            )

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
                    48,
                    48,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.portrait.show()
        except OSError:
            return

    def _show_story_titles(self) -> None:
        scope = (
            "Saving changes future speech and preparation for this role. "
            "Existing recordings keep priority."
        )
        self.role_summary.setToolTip("\n".join((scope, *self._story_titles)))
        self.role_summary.setAccessibleDescription(scope)

    def _select_role_source(self, narrator: bool, selected: str | None) -> None:
        binding, _saved = self._saved_role_voice(self.role.currentText(), narrator)
        saved_reference = binding is not None and bool(binding.source_sha256s)
        model = self.source.model()
        if isinstance(model, QStandardItemModel):
            model.item(self.source.findData("saved")).setEnabled(saved_reference)
            model.item(self.source.findData("automatic")).setEnabled(not narrator)
            model.item(self.source.findData("narrator")).setEnabled(not narrator)
        mode = "game"
        if not narrator and selected is None:
            mode = "automatic"
        elif not narrator and selected == "default":
            mode = "narrator"
        elif selected and selected.startswith("preset:"):
            with QSignalBlocker(self.presets):
                self.presets.setCurrentIndex(self.presets.findData(selected))
            mode = "preset"
        elif saved_reference and self.catalog_choice.findData(selected) < 0:
            mode = "saved"
        elif selected and self.catalog_choice.findData(selected) >= 0:
            with QSignalBlocker(self.catalog_choice):
                self.catalog_choice.setCurrentIndex(
                    self.catalog_choice.findData(selected)
                )
            mode = "catalog"
        with QSignalBlocker(self.source):
            self.source.setCurrentIndex(self.source.findData(mode))

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

    def set_recovery_context(self, role: str, *, resume_live: bool) -> None:
        self._recovery_role = normalize_character_name(role)
        self._recovery_resume_live = resume_live
        self._suppress_auto_discovery = True
        self.context_note.setText(
            f"Live reading is paused for voice editing: {role}. Save a voice to continue after any other voice choices."
            if resume_live
            else f"Choose a voice for {role} to change future speech. Reading stays stopped."
        )
        self.context_note.show()
        set_action_icon(self.cancel_button, "chevron-left" if resume_live else "x")
        self.cancel_button.setText("Back" if resume_live else "Cancel")
        self.cancel_button.setAccessibleName(
            "Back to recovery choices" if resume_live else "Cancel"
        )
        self.cancel_button.setToolTip(self.cancel_button.accessibleName())
        if self.voice_library.binding(role) is None:
            with QSignalBlocker(self.source):
                self.source.setCurrentIndex(self.source.findData("game"))
            self._source_changed()
            self.role_summary.setText("No saved voice (narrator fallback)")

    def _role_changed(self) -> None:
        if self._initializing or self._closing or self._closed:
            return
        role = self.role.currentText().strip()
        if role == "???":
            self.select_role("Narrator")
            return
        binding, selected = self._saved_role_voice(role, is_narrator(role))
        self.role_summary.setText(
            self._role_summary_text(role, is_narrator(role), selected, binding)
        )
        self._show_role_context(role)
        self._select_role_source(is_narrator(role), selected)
        self._load_current_voice()
        if self._story_group() is not None and self.source.currentData() != "saved":
            with QSignalBlocker(self.characters):
                self.characters.clear()
                self.characters.addItem(role)
            with QSignalBlocker(self.source):
                self.source.setCurrentIndex(self.source.findData("game"))
        self._source_changed()

    def _consent_changed(self, *_args: object) -> None:
        if not self._initializing:
            self._load_current_voice()
        self._source_changed()

    def _load_current_voice(self) -> None:
        role = self.role.currentText()
        binding, selected = self._saved_role_voice(role, is_narrator(role))
        self._current_voice_label = voice_binding_label(binding) or self._source_label(
            selected
        )
        self.voice_panel.set_current_voice(
            None, self._current_voice_label, error="Loading current voice..."
        )
        request = partial(
            self._resolve_current_voice,
            self._settings(),
            role,
            binding,
            selected,
            self._voice_context,
            self._story_group(),
            self._catalog_manifest,
            self._catalog_registry,
        )
        if self.current_voice_runner.active:
            self._pending_current_voice = request
        else:
            self.current_voice_runner.start(request)

    def _resolve_current_voice(
        self,
        settings: AppSettings,
        role: str,
        binding: VoiceBinding | None,
        selected: str | None,
        story_plan: VoicePlan | None,
        story_group: VoiceGroup | None,
        manifest: str | Path | None,
        registry: CharacterVoiceRegistry,
    ) -> CurrentVoice:
        authorities: tuple[tuple[str, VoiceBinding | None], ...] = ((role, binding),)
        label = voice_binding_label(binding) or selected or "Automatic"
        plan: VoicePlan | None = None
        if binding is not None and binding.source_sha256s:
            plan = self._narrator_plan(settings, role)
        elif selected and selected != "default":
            plan = narrator_preview_plan(
                settings, manifest, selected, COMPARISON_PREVIEW_TEXT
            )
        elif selected is None and story_plan is not None and story_group is not None:
            plan, label = self._current_story_voice(story_plan, story_group)
            if plan is None:
                selected = "default"
        elif selected is None:
            voice = registry.resolve(role)
            if voice is not None and voice.references:
                plan = narrator_preview_plan(
                    settings,
                    manifest,
                    f"character:{normalize_character_name(voice.character)}",
                    COMPARISON_PREVIEW_TEXT,
                )
                label = f"Automatic: {voice_source_label(voice.source_character or voice.character)}"
            else:
                selected = "default"
        if selected == "default":
            narrator_binding = self.voice_library.binding("Narrator")
            authorities = (*authorities, ("Narrator", narrator_binding))
            source = pregeneration_narrator_source_id(
                settings, voice_library=self.voice_library
            )
            plan = (
                narrator_preview_plan(settings, None, source, COMPARISON_PREVIEW_TEXT)
                if source.startswith("preset:")
                else self._narrator_plan(settings)
            )
            label = "Narrator: " + (
                voice_binding_label(narrator_binding)
                or voice_source_label(plan.groups[0].candidates[0].source_character)
            )
        self._check_current_voice_authority(authorities)
        if plan is None:
            raise ValueError("The current voice is unavailable. Choose a voice above.")
        if selected and selected.startswith("preset:"):
            label = self._source_label(selected)
        return plan, label, authorities

    @staticmethod
    def _current_story_voice(
        story_plan: VoicePlan, story_group: VoiceGroup
    ) -> tuple[VoicePlan | None, str]:
        if story_group.route == "voice":
            candidate = next(
                (
                    value
                    for value in (
                        *story_group.candidates,
                        *story_group.candidate_inventory,
                    )
                    if value.source_id == story_group.source_id
                ),
                None,
            )
            if candidate is None:
                raise ValueError("The current story voice reference is unavailable.")
            group = replace(
                story_group,
                candidates=(candidate,),
                candidate_inventory=(candidate,),
                narrator_candidate=None,
            )
            return (
                replace(story_plan, groups=(group,)),
                f"Automatic: {voice_source_label(candidate.source_character)}",
            )
        elif story_group.route == "narrator":
            return None, ""
        else:
            raise ValueError(
                "No single current voice is assigned to this story character. "
                "Choose a voice above."
            )

    def _check_current_voice_authority(
        self, authorities: tuple[tuple[str, VoiceBinding | None], ...]
    ) -> None:
        for role, expected in authorities:
            current = self.voice_library.binding(
                role, variant_key=self.voice_library.linked_variant_key(role)
            )
            if current != expected:
                raise ValueError("Current voice changed. Reopen the voice editor.")
            if current is not None and current.source_sha256s:
                self.voice_library.reference_paths(current)

    def _current_voice_finished(self, result: object, error: Exception | None) -> None:
        if self._closing:
            self._finish_close()
            return
        if self._pending_current_voice is not None:
            request, self._pending_current_voice = self._pending_current_voice, None
            self.current_voice_runner.start(request)
            return
        if error is not None:
            self.voice_panel.set_current_voice(
                None, self._current_voice_label, error=str(error)
            )
        else:
            plan, label, authorities = cast(CurrentVoice, result)
            self.voice_panel.set_current_voice(
                plan,
                label,
                playback_guard=partial(
                    self._check_current_voice_authority, authorities
                ),
            )
        self._update()

    def _source_label(self, source_id: str | None) -> str:
        if source_id is None:
            return "automatic assignment"
        if source_id == "default":
            return "narrator fallback"
        if source_id.startswith("preset:"):
            return source_id.partition(":")[2].replace("_", " ").title()
        voice = self._catalog_registry.resolve_source(source_id)
        return (
            voice_source_label(voice.source_character or voice.character)
            if voice
            else "Game voice"
        )

    def saved_assignment_summary(self) -> str:
        binding, selected = self._saved_role_voice(
            self._saving_role, is_narrator(self._saving_role)
        )
        label = voice_binding_label(binding) or self._source_label(selected)
        return f"{self._saving_role}: {label}"

    def _settings(self) -> AppSettings:
        return self.settings_value.updated(
            pocket_gated_model_accepted=self.consent.isChecked()
        )

    def _capture_save(self, _selections: object) -> None:
        self._selection_settings = self._settings()
        self._selection_manifest = self._candidate_manifest
        self._saving_role = self.role.currentText().strip()
        self._selection_engine_available = self._engine_available()

    def _check_narrator_authority(self) -> None:
        current = self.voice_library.binding("Narrator")
        if current != self._narrator_binding:
            raise ValueError(
                "Narrator voice changed. Select its source again before playback."
            )
        if current is not None and current.source_sha256s:
            self.voice_library.reference_paths(current)
            selection = self.voice_panel.current_selection
            candidate = selection[1].narrator_candidate if selection else None
            if (
                candidate is not None
                and candidate.reference_sha256s != current.source_sha256s
            ):
                raise ValueError(
                    "Narrator reference changed. Select its source again before playback."
                )

    def _selection_changed(self) -> None:
        if (
            self.source.currentData() == "saved"
            and self._saved_voice_binding is not None
        ):
            self.voice_panel.playback_guard = partial(
                self._check_saved_voice_authority, self._saved_voice_binding
            )
        elif self._selected_source() == "default":
            if self.source.currentData() != "narrator":
                self._narrator_binding = self.voice_library.binding("Narrator")
            self.voice_panel.playback_guard = self._check_narrator_authority
        else:
            self.voice_panel.playback_guard = None
        if not self._initializing:
            self._settings_choice_changed()

    def _selected_source(self) -> str | None:
        selection = (
            None if self.voice_panel.isHidden() else self.voice_panel.current_selection
        )
        return selection[2] if selection is not None else None

    def _check_saved_voice_authority(self, binding: VoiceBinding) -> None:
        current = self.voice_library.binding(
            binding.role, variant_key=binding.variant_key
        )
        if current != binding:
            raise ValueError(
                "Saved voice changed. Select its source again before playback."
            )
        self.voice_library.reference_paths(binding)
        selection = self.voice_panel.current_selection
        candidate = selection[1].candidates[0] if selection else None
        if (
            candidate is not None
            and candidate.reference_sha256s != binding.source_sha256s
        ):
            raise ValueError(
                "Saved reference changed. Select its source again before playback."
            )

    def _settings_choice_changed(self) -> None:
        binding, saved = self._saved_role_voice(
            self.role.currentText(), is_narrator(self.role.currentText())
        )
        source = self._selected_source()
        if self.source.currentData() == "saved" and (
            source is None or source == voice_binding_source_id(binding)
        ):
            source = saved
        if source == "automatic":
            source = None
        changed = (
            source != saved
            or self.consent.isChecked()
            != self._initial_settings_value.pocket_gated_model_accepted
        )
        if changed != self._settings_dirty:
            self._settings_dirty = changed
            self.settingsChanged.emit(changed)

    def _source_changed(self, *_args: object) -> None:
        if self._initializing or self._closing or self._closed:
            return
        self._set_status("")
        mode = self.source.currentData()
        self.voice_panel.playback_guard = None
        self.form.setRowVisible(self.presets, mode == "preset")
        self.form.setRowVisible(self.catalog_choice, mode == "catalog")
        self.form.setRowVisible(self.game_controls, mode == "game")
        pocket = self.settings_value.speech_backend == "pocket-tts"
        self.terms.setVisible(
            pocket and mode in {"game", "catalog", "narrator", "saved"}
        )
        self.consent.setVisible(self.terms.isVisibleTo(self.controls))
        if mode == "game":
            group = self._story_group()
            if (
                group is not None
                and self._voice_context is not None
                and (
                    not self.characters.currentText()
                    or normalize_character_name(self.characters.currentText())
                    == normalize_character_name(self.role.currentText())
                )
            ):
                with QSignalBlocker(self.characters):
                    self.characters.clear()
                    self.characters.addItems(
                        sorted(
                            {
                                self.role.currentText(),
                                *(
                                    value.routing_role or value.character
                                    for value in self._voice_context.groups
                                ),
                            },
                            key=str.casefold,
                        )
                    )
                    self.characters.setCurrentText(self.role.currentText())
                self._character = self.role.currentText()
                self._show_plan_candidates(self._voice_context.voice_manifest, group)
            elif self.characters.currentText():
                self._prepare()
            elif not self.runner.active and not self._suppress_auto_discovery:
                self.discover()
            else:
                self.voice_panel.hide()
        elif mode == "automatic":
            self._show_policy("automatic", "Restore automatic voice matching")
        elif mode == "narrator":
            self._show_narrator()
        elif mode == "saved":
            self._saved_voice_binding = self.voice_library.binding(
                self.role.currentText(),
                variant_key=self.voice_library.linked_variant_key(
                    self.role.currentText()
                ),
            )
            self.voice_panel.hide()
            self._start(
                "saved",
                "Loading the saved voice reference...",
                self._narrator_plan,
                self._settings(),
                self.role.currentText(),
            )
        else:
            source_id = (
                self.presets.currentData()
                if mode == "preset"
                else self.catalog_choice.currentData()
            )
            if source_id:
                self._show_source(
                    None if mode == "preset" else self._catalog_manifest, source_id
                )
            else:
                self.voice_panel.hide()
                self._set_status(
                    "No imported voices available. Choose a game voice or built-in voice."
                )
        self._update()
        self._settings_choice_changed()

    def _policy_group(self) -> VoiceGroup:
        role = self.role.currentText().strip()
        return VoiceGroup(
            group_id=normalize_character_name(role),
            character=role,
            speakers=(role,),
            portrait=None,
            source_bank=None,
            source_voice_id=None,
            line_ids=(),
            sample_text=COMPARISON_PREVIEW_TEXT,
            alternate_sample_text=None,
            route="narrator",
            source_id="default",
            source_character=None,
            source_speaker=None,
            reference_sha256s=(),
            decision_context_sha256="",
            control_sha256="",
            resolution="player-voice-policy",
        )

    def _show_policy(self, source_id: str, label: str) -> None:
        self._candidate_manifest = None
        group = self._policy_group()
        settings = self._settings()
        plan = VoicePlan(
            job_id="voice-policy",
            created_at="",
            story_index_sha256="",
            voice_manifest=None,
            voice_manifest_sha256=None,
            synthesis_backend=settings.speech_backend,
            synthesis_model=settings.tts_model,
            synthesis_language=settings.tts_language,
            synthesis_profile=settings.tts_profile,
            pocket_voice_cloning=settings.pocket_gated_model_accepted,
            xtts_terms_accepted=settings.xtts_terms_accepted,
            synthesis_controls_sha256="",
            groups=(group,),
        )
        self.voice_panel.start_policy(
            plan, group_id=group.group_id, label=label, choice_id=source_id
        )

    def _show_source(self, manifest: str | Path | None, source_id: str) -> None:
        try:
            plan = narrator_preview_plan(
                self._settings(), manifest, source_id, COMPARISON_PREVIEW_TEXT
            )
            group = replace(
                plan.groups[0],
                character=self.role.currentText(),
                speakers=(self.role.currentText(),),
                candidate_inventory=plan.groups[0].candidates,
            )
            self._candidate_manifest = manifest
            self.voice_panel.start(
                replace(plan, groups=(group,)), group_id=group.group_id
            )
            if not self._engine_available():
                self.voice_panel.a_play.setEnabled(False)
                self._set_status(
                    "Accept the XTTS model terms in Settings before previewing."
                    if self.settings_value.speech_backend == "coqui-xtts"
                    and not self.settings_value.xtts_terms_accepted
                    else "This engine is not included in this package. Choose an available engine in Settings."
                )
        except (OSError, ValueError) as error:
            self.voice_panel.hide()
            self._set_status(str(error))

    def _show_narrator(self) -> None:
        self._narrator_binding = self.voice_library.binding("Narrator")
        try:
            source = pregeneration_narrator_source_id(
                self._settings(), voice_library=self.voice_library
            )
        except (ValueError, PregenerationVoiceError) as error:
            self._show_policy("default", "Use narrator's voice")
            self._set_status(str(error))
            return
        if source.startswith("preset:"):
            try:
                plan = narrator_preview_plan(
                    self._settings(), None, source, COMPARISON_PREVIEW_TEXT
                )
                self._start_narrator_panel(plan)
            except ValueError as error:
                self._show_policy("default", "Use narrator's voice")
                self._set_status(str(error))
            return
        self._start(
            "narrator",
            "Loading the saved narrator reference...",
            self._narrator_plan,
            self._settings(),
        )

    def _narrator_plan(
        self, settings: AppSettings, role: str = "Narrator"
    ) -> VoicePlan:
        registry = registry_with_voice_library(
            self._catalog_registry, self.voice_library
        )
        binding = self.voice_library.binding(
            role, variant_key=self.voice_library.linked_variant_key(role)
        )
        source_id = voice_binding_source_id(binding)
        voice = (
            registry.resolve_source(source_id) if source_id else registry.resolve(role)
        )
        if voice is None or not voice.references:
            raise ValueError(f"Choose an available {role} voice before previewing it")
        # Concurrent current/candidate loads must never rewrite audio being read.
        folder = Path(mkdtemp(prefix="reference-", dir=self._narrator_directory.name))
        names = []
        for index, reference in enumerate(voice.references):
            if self.cancellation.is_set():
                raise RuntimeError("Narrator selection cancelled")
            name = f"reference-{index}.wav"
            (folder / name).write_bytes(read_voice_reference_bytes(voice, reference))
            names.append(name)
        manifest = folder / "manifest.json"
        write_voice_manifest(
            manifest,
            {
                "version": 2,
                "voices": [
                    {
                        "character": voice.character,
                        "speaker": voice.speaker,
                        "references": names,
                        "vntts.source_character": voice.source_character
                        or voice.character,
                    }
                ],
            },
        )
        return narrator_preview_plan(
            settings,
            manifest,
            f"character:{normalize_character_name(voice.character)}",
            COMPARISON_PREVIEW_TEXT,
            voice.reference_transcript,
        )

    def _start_narrator_panel(self, plan: VoicePlan) -> None:
        candidate = plan.groups[0].candidates[0]
        group = replace(
            plan.groups[0],
            character=self.role.currentText(),
            speakers=(self.role.currentText(),),
            candidates=(),
            candidate_inventory=(),
            narrator_candidate=candidate,
        )
        self._candidate_manifest = plan.voice_manifest
        self.voice_panel.start(replace(plan, groups=(group,)), group_id=group.group_id)

    def _story_group(self) -> VoiceGroup | None:
        return (
            next(
                (
                    group
                    for group in self._voice_context.groups
                    if normalize_character_name(group.routing_role or group.character)
                    == normalize_character_name(self.role.currentText())
                ),
                None,
            )
            if self._voice_context
            else None
        )

    def _show_plan_candidates(
        self, manifest: str | Path | None, group: VoiceGroup
    ) -> None:
        self._candidate_manifest = manifest
        if self._voice_context is None:
            return
        try:
            self.voice_panel.start(self._voice_context, group_id=group.group_id)
        except (ValueError, VoiceAuditionUIError) as error:
            self.voice_panel.hide()
            self._set_status(
                f"{error}. Choose another voice source or narrator fallback."
            )

    def _character_changed(self, *_args: object) -> None:
        if (
            not self._initializing
            and not self.runner.active
            and self.characters.currentText()
        ):
            self._prepare()

    def discover(self, installation_root: Path | None = None) -> None:
        if self.runner.active or self._closing:
            return
        if installation_root is not None:
            self._installation_root = installation_root
            self.game_installation.setText(str(installation_root))
        with QSignalBlocker(self.characters):
            self.characters.clear()
        self.voice_panel.hide()
        self._start(
            "discover",
            "Finding installed game voices...",
            self.importer.narrator_characters,
            self.cancellation,
            installation_root,
        )

    def _prepare(self) -> None:
        if self.runner.active or not self.characters.currentText():
            return
        self._character = self.characters.currentText()
        self.voice_panel.hide()
        self._start(
            "prepare",
            f"Preparing voice candidates for {self._character}...",
            partial(
                self.importer.prepare_voice_roles,
                (self._character,),
                self.cancellation,
                progress=self.decoderProgress.emit,
                narrator=False,
                speech_backend=self.settings_value.speech_backend,
            ),
        )

    def _show_character_candidates(self, manifest: Path | str) -> None:
        registry, variants = validated_player_voice_candidates(
            Path(manifest), speech_backend=self.settings_value.speech_backend
        )
        candidates = []
        for variant in variants:
            if normalize_character_name(
                str(variant.get("character", ""))
            ) != normalize_character_name(self._character):
                continue
            source = f"character:{normalize_character_name(variant['voice_character'])}"
            voice = registry.resolve_source(source)
            if voice is None:
                continue
            excerpts = cast(
                Sequence[dict[str, object]], variant.get("source_excerpts", ())
            )
            candidates.append(
                VoiceCandidate(
                    source_id=source,
                    source_character=voice.source_character or voice.character,
                    source_speaker=voice.speaker,
                    reference_sha256s=tuple(
                        sha256_file(path) for path in voice.references
                    ),
                    reference_duration_seconds=cast(
                        float | None, variant.get("duration_seconds")
                    ),
                    source_bank=cast(str | None, variant.get("source_bank")),
                    source_line_ids=tuple(
                        cast(Sequence[str], variant.get("source_line_ids", ()))
                    ),
                    source_voice_ids=tuple(
                        cast(Sequence[str], variant.get("source_voice_ids", ()))
                    ),
                    source_excerpts=tuple(
                        str(value.get("text", "")) for value in excerpts
                    ),
                    candidate_origin=cast(str | None, variant.get("candidate_origin")),
                    reference_transcript=voice.reference_transcript,
                )
            )
        if not candidates:
            self._set_status(
                "No usable voice candidates found. Choose another character."
            )
            return
        plan = narrator_preview_plan(
            self._settings(), manifest, candidates[0].source_id, COMPARISON_PREVIEW_TEXT
        )
        _binding, saved = self._saved_role_voice(
            self.role.currentText(), is_narrator(self.role.currentText())
        )
        group = replace(
            plan.groups[0],
            character=self.role.currentText(),
            speakers=(self.role.currentText(),),
            candidates=tuple(candidates),
            candidate_inventory=tuple(candidates),
            source_id=saved or candidates[0].source_id,
        )
        self._candidate_manifest = manifest
        self.voice_panel.start(replace(plan, groups=(group,)), group_id=group.group_id)

    def _start(
        self,
        operation: str,
        message: str,
        function: Callable[..., object],
        *arguments: object,
    ) -> None:
        if self.runner.active:
            return
        self._operation = operation
        self.cancellation.clear()
        self._set_status(message)
        self.runner.start(function, *arguments)
        self._update()

    def _finished(self, result: object, error: Exception | None) -> None:
        if self._closing:
            self._finish_close()
            return
        operation = self._operation
        self._operation = None
        if error is not None:
            self._operation_failed(operation, error)
            return
        self._set_status("")
        if operation == "discover":
            self._discovery_finished(result)
        elif operation == "prepare" and self.source.currentData() == "game":
            try:
                if not isinstance(result, (Path, str)):
                    raise ValueError("Game importer returned an invalid voice manifest")
                self._show_character_candidates(result)
            except (OSError, ValueError, PregenerationVoiceError) as failure:
                self._set_status(f"{failure}\nRetry or choose another game folder.")
        elif operation == "narrator":
            if isinstance(result, VoicePlan):
                self._start_narrator_panel(result)
        elif operation == "saved" and isinstance(result, VoicePlan):
            group = replace(
                result.groups[0],
                character=self.role.currentText(),
                speakers=(self.role.currentText(),),
                candidate_inventory=result.groups[0].candidates,
            )
            self._candidate_manifest = result.voice_manifest
            self.voice_panel.start(
                replace(result, groups=(group,)), group_id=group.group_id
            )
        self._update()

    def _operation_failed(self, operation: str | None, error: Exception) -> None:
        if isinstance(error, DecoderSetupRequired) and confirm_decoder_setup(
            self, error
        ):
            self.importer.allow_decoder_homebrew = True
            self._prepare()
            return
        if operation == "narrator":
            self._show_policy("default", "Use narrator's voice")
        self._set_status(f"{error}\nRetry or cancel. Nothing was assigned.")
        self._update()

    def _discovery_finished(self, result: object) -> None:
        if not isinstance(result, tuple) or not all(
            isinstance(value, str) for value in result
        ):
            self._set_status("Game importer returned invalid character names")
            return
        with QSignalBlocker(self.characters):
            self.characters.addItems(result)
            index = self.characters.findText(self.role.currentText())
            if index >= 0:
                self.characters.setCurrentIndex(index)
        self._show_known_installation()
        if result:
            self._prepare()
        else:
            self._set_status(
                "No voiced characters found. Choose the game folder to reimport."
            )

    def remember_many(self, selections: tuple[tuple[VoiceGroup, str], ...]) -> None:
        if len(selections) != 1:
            raise ValueError("Choose one role at a time")
        group, source_id = selections[0]
        settings = self._selection_settings
        role = self._saving_role
        if (
            self.source.currentData() == "saved"
            and self._saved_voice_binding is not None
        ):
            self._check_saved_voice_authority(self._saved_voice_binding)
        self._saved_source = source_id
        if self._owns_voice_binding_rollback:
            self._voice_binding_rollback = self.voice_library.binding_rollback()
        if source_id == "automatic":
            self.voice_library.clear(
                role,
                variant_key=self.voice_library.linked_variant_key(role),
                rollback=self._voice_binding_rollback,
            )
            self.result_settings = settings.updated(
                tts_speaker_wav=None if is_narrator(role) else settings.tts_speaker_wav
            )
            return
        if source_id == "default" or source_id.startswith("preset:"):
            if (
                source_id.startswith("preset:")
                and settings.speech_backend != "pocket-tts"
            ):
                raise ValueError(
                    "Built-in voices require Pocket TTS. Choose another engine in Settings."
                )
            remember_voice_binding(
                self.voice_library,
                self._catalog_registry,
                role,
                source_id,
                variant_key=self.voice_library.linked_variant_key(role),
                method="manual",
                evidence={"selected_in": "voice-picker"},
                algorithm="voice-picker-v1",
                rollback=self._voice_binding_rollback,
            )
            self.result_settings = settings.updated(
                tts_speaker_wav=None if is_narrator(role) else settings.tts_speaker_wav
            )
            return
        if (
            settings.speech_backend == "pocket-tts"
            and not settings.pocket_gated_model_accepted
        ):
            raise ValueError("Accept Pocket's terms before saving a game voice")
        if not self._selection_engine_available:
            raise ValueError(
                "Choose an available engine and accept its model terms in Settings"
            )
        candidate = next(
            (
                value
                for value in (*group.candidates, *group.candidate_inventory)
                if value.source_id == source_id
            ),
            None,
        )
        if candidate is None:
            raise ValueError("Choose a checked voice candidate")
        self._bind_voice_candidate(settings, role, source_id, candidate)

    def _bind_voice_candidate(
        self,
        settings: AppSettings,
        role: str,
        source_id: str,
        candidate: VoiceCandidate,
    ) -> None:
        context: dict[str, object] = {}
        if (
            self.binder is bind_voice_library_selection
            or getattr(self.binder, "func", None) is bind_voice_library_selection
        ):
            context.update(
                root=self.voice_library.root,
                rollback=self._voice_binding_rollback,
                reference_transcript=candidate.reference_transcript,
            )
        if not is_narrator(role):
            context["target_character"] = role
        manifest = self._selection_manifest
        if manifest is None:
            raise ValueError("Choose a checked voice manifest")
        registry = CharacterVoiceRegistry.from_file(manifest)
        selected = registry.resolve_source(source_id)
        if (
            selected is None
            or tuple(sha256_file(path) for path in selected.references)
            != candidate.reference_sha256s
        ):
            raise ValueError(
                "Selected voice reference changed. Reopen the voice editor."
            )
        self.result_settings = self.binder(
            settings,
            manifest,
            source_id,
            candidate.source_character,
            **context,
        )

    def _saved(self) -> None:
        self._closing = True
        self._panel_cancelled = True
        self.voice_panel.shutdown()
        self._finish_close()

    def _panel_finished(self) -> None:
        self._panel_cancelled = True
        if self._closing:
            self.voice_panel.shutdown()
            self._finish_close()

    def _finish_close(self) -> None:
        if (
            self._closed
            or not self._closing
            or self.active
            or not self._panel_cancelled
        ):
            return
        self._closed = True
        if self._narrator_directory is not None:
            self._narrator_directory.cleanup()
        if self.result_settings is not None:
            super().accept()
        else:
            super().reject()

    def reject(self) -> None:
        if self._closed:
            super().reject()
            return
        self._closing = True
        self.cancellation.set()
        self._set_status("Cancelling. Waiting for voice operations to stop...")
        self.voice_panel.cancel()
        self._finish_close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closed:
            super().closeEvent(event)
        else:
            event.ignore()
            self.reject()

    def _engine_available(self) -> bool:
        backend = self.settings_value.speech_backend
        return (
            backend != "coqui-xtts" or self.settings_value.xtts_terms_accepted
        ) and any(
            option == backend and available
            for _label, option, available in speech_backend_options(backend)
        )

    def _update(self, *_args: object) -> None:
        idle = not self.active and not self._closing
        self.progress.setVisible(self.runner.active)
        for control in (
            self.role,
            self.source,
            self.presets,
            self.catalog_choice,
            self.characters,
            self.discover_button,
            self.folder_button,
            self.consent,
        ):
            control.setEnabled(idle)

    def _copy_details(self) -> str:
        return "\n".join(
            (
                self.saved_assignment_summary(),
                self.status.text(),
                self.voice_panel.status.toPlainText(),
            )
        )
