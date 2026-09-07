"""Guided game narrator discovery, reference listening and synthesis preview."""

from functools import partial
from pathlib import Path
from threading import Event

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.file_integrity import sha256_file

from vntts.async_ui import LatestTaskRunner
from vntts.game_audio_decoder import DecoderSetupRequired, confirm_decoder_setup
from vntts.game_content_importer import Reverse1999GameImporter
from vntts.game_narrator import bind_game_narrator, narrator_preview_plan
from vntts.pregeneration_audition import VoiceAuditionPreviewService
from vntts.pregeneration_voices import (
    pregeneration_narrator_source_id,
    resolve_pregeneration_settings,
)
from vntts.qt_audio import QtPcmPlayer
from vntts.release_backends import speech_backend_options
from vntts.speech_presentation import engine_model_label, narrator_voice_label
from vntts.tts_benchmark import create_backend
from vntts.voices import (
    CharacterVoiceRegistry,
    find_default_voice_manifest,
    find_voice_assignment,
    is_narrator,
    normalize_character_name,
    pocket_tts_preset_voices,
)


class GameNarratorDialog(QDialog):
    decoderProgress = Signal(str)

    def __init__(
        self,
        settings,
        parent=None,
        *,
        importer=None,
        preview_service=None,
        thread_pool=None,
        player=None,
        binder=bind_game_narrator,
    ):
        super().__init__(parent)
        self.setWindowTitle("Narrator and character voices")
        self.resize(640, 560)
        self.settings_value = resolve_pregeneration_settings(settings)
        self.result_settings = None
        self.importer = importer or Reverse1999GameImporter()
        self.previews = preview_service or VoiceAuditionPreviewService(
            backend_factory=partial(
                create_backend, terms_accepted=settings.xtts_terms_accepted
            )
        )
        self.binder = binder
        self.player = player or QtPcmPlayer(self)
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._finished)
        self.cancellation = Event()
        self.decoderProgress.connect(self._decoder_progress)
        self._prepared = {}
        self._character = None
        self._operation = None
        self._warming_reference = None
        self._queued_action = None
        self._playback_requested = False
        self._closing = False
        self._closed = False
        self._catalog_manifest = (
            settings.voice_manifest or find_default_voice_manifest()
        )
        self._catalog_registry = CharacterVoiceRegistry()
        self._voice_context = None
        self._story_titles = ()
        self._saving_role = "Narrator"

        self.status = QLabel("Choose a candidate. Nothing changes until you save.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Game narrator progress")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.controls = QWidget()
        form = QFormLayout(self.controls)
        self.form = form
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.engine_choice = QComboBox()
        self.engine_choice.setAccessibleName("Voice preview and preparation engine")
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
            self.engine_choice.findData(settings.speech_backend)
        )
        form.addRow("Speech engine", self.engine_choice)
        self.engine = QLabel()
        self.engine.setWordWrap(True)
        form.addRow(self.engine)
        self.engine_guidance = QLabel()
        self.engine_guidance.setWordWrap(True)
        form.addRow(self.engine_guidance)
        self.model_details = QPushButton("Custom model details")
        self.model_details.setCheckable(True)
        self.model_details.toggled.connect(self._update)
        form.addRow(self.model_details)
        self.model_choice = QLineEdit(self.settings_value.tts_model or "")
        self.model_choice.setAccessibleName(
            "Custom voice preview and preparation model"
        )
        self.model_choice.setPlaceholderText("Use the default model shown above")
        form.addRow("Custom model", self.model_choice)
        self.role = QComboBox()
        self.role.setEditable(True)
        self.role.setAccessibleName("Narrator or character role to edit")
        self.role.addItem("Narrator")
        form.addRow("Voice for", self.role)
        self.role_summary = QLabel()
        self.role_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.role_summary.setWordWrap(True)
        form.addRow(self.role_summary)
        self.portrait = QLabel()
        self.portrait.setAccessibleName("Selected character portrait")
        form.addRow(self.portrait)
        self.source = QComboBox()
        self.source.setAccessibleName("Voice source")
        self.source.addItem("Game character", "game")
        self.source.addItem("Built-in Pocket voice", "preset")
        self.source.addItem("Imported game voice", "catalog")
        self.source.addItem("Automatic assignment", "automatic")
        self.source.addItem("Narrator fallback", "narrator")
        selected = find_voice_assignment(
            settings.voice_assignments, "Narrator"
        ) or pregeneration_narrator_source_id(self.settings_value)
        if selected.startswith("preset:"):
            self.source.setCurrentIndex(1)
        form.addRow("Voice source", self.source)
        self.presets = QComboBox()
        self.presets.setAccessibleName("Built-in narrator candidate")
        for name in pocket_tts_preset_voices:
            self.presets.addItem(name.replace("_", " ").title(), f"preset:{name}")
        self.presets.setCurrentIndex(max(0, self.presets.findData(selected)))
        self.presets.currentIndexChanged.connect(lambda: self.player.stop())
        form.addRow(self.presets)
        self.catalog_choice = QComboBox()
        self.catalog_choice.setAccessibleName("Imported character voice candidate")
        self.catalog_choice.currentIndexChanged.connect(lambda: self._stop_audio())
        form.addRow(self.catalog_choice)
        self.game_controls = QWidget()
        game_form = QFormLayout(self.game_controls)
        game_form.setContentsMargins(0, 0, 0, 0)
        game_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow(self.game_controls)
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
        self.discover_button = QPushButton("Find game voices")
        self.discover_button.clicked.connect(lambda: self.discover())
        self.folder_button = QPushButton("Choose game folder...")
        self.folder_button.clicked.connect(self._choose_folder)
        game_form.addRow(self.discover_button, self.folder_button)
        self.characters = QComboBox()
        self.characters.setAccessibleName("Game character")
        self.characters.currentIndexChanged.connect(self._character_changed)
        self.prepare_button = QPushButton("Load this character's references")
        self.prepare_button.clicked.connect(self._prepare)
        game_form.addRow("Character", self.characters)
        game_form.addRow(self.prepare_button)
        self.references = QComboBox()
        self.references.setAccessibleName("Original game reference")
        self.references.currentIndexChanged.connect(self._reference_changed)
        game_form.addRow("Reference", self.references)
        self.reference_text = QLabel()
        self.reference_text.setWordWrap(True)
        self.reference_text.setTextFormat(Qt.TextFormat.PlainText)
        self.reference_text.setAccessibleName("Original reference transcript")
        game_form.addRow(self.reference_text)
        self.original_button = QPushButton("Play original")
        self.original_button.setAccessibleName("Play original game reference")
        self.original_button.clicked.connect(self._original)
        self.text = QLineEdit("The storm has passed. We can continue our journey.")
        form.addRow("Preview text", self.text)
        self.preview_button = QPushButton("Generate preview")
        self.preview_button.setAccessibleName("Generate and play voice preview")
        self.preview_button.setToolTip(
            "Generate speech for this candidate, or replay its saved preview."
        )
        self.preview_button.clicked.connect(self._preview)
        self.save_button = QPushButton("Save voice settings")
        self.save_button.clicked.connect(self._save)
        self.stop_button = QPushButton("Stop audio")
        self.stop_button.clicked.connect(self._stop_audio)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.controls)
        layout.addWidget(self.scroll, 1)
        transport = QHBoxLayout()
        transport.addWidget(self.original_button)
        transport.addWidget(self.preview_button)
        transport.addWidget(self.stop_button)
        layout.addLayout(transport)
        note = QLabel(
            "Defaults apply to future preparation and live fallback. Existing recordings keep their recorded voices. "
            "Select affected stories in Stories to prepare them again."
        )
        note.setWordWrap(True)
        form.addRow(note)
        self.announcements = QComboBox()
        self.announcements.setAccessibleName("Announce speaker names")
        self.announcements.addItem("Do not announce names", "off")
        self.announcements.addItem(
            "Announce narrator fallback roles", "narrator-fallback-roles"
        )
        self.announcements.addItem("Announce every speaker change", "all-speakers")
        self.announcements.setCurrentIndex(
            self.announcements.findData(settings.effective_speaker_announcement_mode)
        )
        form.addRow("Speaker names", self.announcements)
        actions = QHBoxLayout()
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        self.player.errorOccurred.connect(
            lambda _code, message: self.status.setText(message)
        )
        self.source.currentIndexChanged.connect(self._source_changed)
        self.engine_choice.currentIndexChanged.connect(self._engine_changed)
        self.model_choice.textChanged.connect(self._model_changed)
        self.role.currentTextChanged.connect(self._role_changed)
        self._initializing = True
        self.set_voice_context()
        self._initializing = False
        self._update()
        QTimer.singleShot(0, self._source_changed)

    def set_voice_context(
        self, plan=None, character=None, *, roles=(), story_titles=()
    ):
        """Use imported metadata without starting the reading engine."""
        if self.runner.active:
            return
        self._voice_context = plan
        self._story_titles = story_titles
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
        roles = {
            *roles,
            *self.settings_value.voice_assignments,
            *self.settings_value.character_voice_defaults,
            *(
                voice.character
                for voice in self._catalog_registry.unique_voices()
                if not voice.character.startswith(("Game narrator ", "Game voice "))
            ),
            *((group.character for group in plan.groups) if plan is not None else ()),
        }
        with QSignalBlocker(self.role):
            selected = character or self.role.currentText() or "Narrator"
            self.role.clear()
            self.role.addItems(
                [
                    "Narrator",
                    *sorted(
                        (role for role in roles if not is_narrator(role)),
                        key=str.casefold,
                    ),
                ]
            )
            self.role.setCurrentText(selected)
        self._role_changed()

    def _role_changed(self):
        self._stop_audio()
        role = self.role.currentText().strip()
        if role == "???":
            self.role.setCurrentText("Narrator")
            return
        narrator = normalize_character_name(role) == "narrator"
        settings = self.settings_value
        selected = (
            (
                find_voice_assignment(settings.voice_assignments, "Narrator")
                or pregeneration_narrator_source_id(settings)
            )
            if narrator
            else find_voice_assignment(settings.character_voice_defaults, role)
        )
        manual = not narrator and find_voice_assignment(
            settings.voice_assignments, role
        )
        self.role_summary.setText(
            f"Saved narrator: {narrator_voice_label(settings)}"
            if narrator
            else f"{role}: saved default {self._source_label(selected)}. "
            + (
                "An existing live override is active; saving this default restores recording priority. "
                if manual
                else ""
            )
            + "Original and prepared recordings keep priority."
        )
        self.portrait.clear()
        self.portrait.hide()
        if self._voice_context is not None:
            group = next(
                (
                    group
                    for group in self._voice_context.groups
                    if normalize_character_name(group.character)
                    == normalize_character_name(role)
                ),
                None,
            )
            if group is not None:
                self.role_summary.setText(
                    self.role_summary.text()
                    + f" Planned: {self._source_label(group.source_id)}; {len(group.line_ids)} lines."
                )
                if group.portrait_image and group.portrait_image_sha256:
                    try:
                        if (
                            sha256_file(group.portrait_image)
                            == group.portrait_image_sha256
                        ):
                            pixmap = QPixmap(group.portrait_image)
                            if not pixmap.isNull():
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
                        pass
        if self._story_titles:
            self.role_summary.setText(
                self.role_summary.text()
                + " Selected stories: "
                + ", ".join(self._story_titles[:3])
                + (
                    f" and {len(self._story_titles) - 3} more"
                    if len(self._story_titles) > 3
                    else ""
                )
            )
        self.role_summary.setToolTip("\n".join(self._story_titles))
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
            self.presets.setCurrentIndex(self.presets.findData(selected))
            mode = "preset"
        elif selected and self.catalog_choice.findData(selected) >= 0:
            self.catalog_choice.setCurrentIndex(self.catalog_choice.findData(selected))
            mode = "catalog"
        with QSignalBlocker(self.source):
            self.source.setCurrentIndex(self.source.findData(mode))
        self._source_changed()

    def _source_label(self, source_id):
        if source_id is None:
            return "automatic assignment"
        if source_id == "default":
            return "narrator fallback"
        if source_id.startswith("preset:"):
            return source_id.partition(":")[2].replace("_", " ").title()
        index = self.catalog_choice.findData(source_id)
        return (
            self.catalog_choice.itemText(index)
            if index >= 0
            else "unavailable saved voice"
        )

    def _engine_available(self):
        item = self.engine_choice.model().item(self.engine_choice.currentIndex())
        return item is not None and item.isEnabled()

    def _engine_changed(self):
        if (
            self.runner.active
            or self._closing
            or self._closed
            or not self._engine_available()
        ):
            blocker = QSignalBlocker(self.engine_choice)
            self.engine_choice.setCurrentIndex(
                self.engine_choice.findData(self.settings_value.speech_backend)
            )
            del blocker
            return
        backend = self.engine_choice.currentData()
        self._stop_audio()
        self.settings_value = self.settings_value.updated(
            speech_backend=backend,
            tts_model=None,
            tts_profile="default" if backend == "pocket-tts" else "stable",
        )
        self.model_choice.clear()
        self._source_changed()

    def _model_changed(self, model):
        if self.runner.active or self._closing or self._closed:
            blocker = QSignalBlocker(self.model_choice)
            self.model_choice.setText(self.settings_value.tts_model or "")
            del blocker
            return
        self._stop_audio()
        self.settings_value = self.settings_value.updated(
            tts_model=model.strip() or None
        )
        self._update()

    def _source_changed(self):
        if self._closing or self._closed:
            return
        self._stop_audio()
        preset = self.source.currentData() == "preset"
        catalog = self.source.currentData() == "catalog"
        policy = self.source.currentData() in {"automatic", "narrator"}
        self.form.setRowVisible(self.presets, preset)
        self.form.setRowVisible(self.catalog_choice, catalog)
        self.form.setRowVisible(self.game_controls, self.source.currentData() == "game")
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
            self.status.setText(self.engine_guidance.text())
        if (
            self.source.currentData() == "game"
            and not self.characters.count()
            and not self.runner.active
            and not self._initializing
        ):
            self.discover()

    def _settings(self):
        return self.settings_value.updated(
            pocket_gated_model_accepted=self.consent.isChecked(),
            speaker_announcement_mode=self.announcements.currentData(),
            announce_speaker_changes=False,
        )

    def _update(self):
        settings = self._settings()
        pocket = settings.speech_backend == "pocket-tts"
        preset = self.source.currentData() == "preset"
        catalog = self.source.currentData() == "catalog"
        policy = self.source.currentData() in {"automatic", "narrator"}
        self.engine.setText(
            engine_model_label(
                settings.speech_backend,
                settings.tts_model,
                pocket_cloning=pocket
                and not preset
                and (not policy or settings.pocket_gated_model_accepted),
            )
        )
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
        self.engine_choice.setEnabled(idle)
        self.role.setEnabled(idle)
        self.catalog_choice.setEnabled(idle)
        self.announcements.setEnabled(idle)
        self.model_choice.setEnabled(idle)
        self.model_details.setEnabled(idle)
        custom_model = settings.speech_backend in {"moss-tts", "coqui-xtts"}
        self.form.setRowVisible(self.model_details, custom_model)
        self.form.setRowVisible(
            self.model_choice, custom_model and self.model_details.isChecked()
        )
        self.source.model().item(self.source.findData("preset")).setEnabled(pocket)
        self.presets.setEnabled(idle)
        self.consent.setEnabled(idle)
        self.text.setEnabled(idle)
        self.terms.setVisible(pocket and not preset and not policy)
        self.consent.setVisible(pocket and not preset and not policy)
        self.engine_guidance.setText(
            "XTTS is not supported for story preparation. Choose another engine."
            if settings.speech_backend == "coqui-xtts"
            else "This engine is not included in this package. Choose an available engine."
            if not available
            else "Built-in Pocket voices require Pocket TTS. Choose a game voice for this engine."
            if preset and not pocket
            else "Pocket TTS is recommended: built-in voices need no game references or account."
            if pocket
            else "This engine uses a game reference. The preview loads its model when needed."
        )
        self.prepare_button.setEnabled(self.characters.count() > 0 and idle)
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
        self.preview_button.setEnabled(
            ready and allowed and not policy and (idle or warming)
        )
        self.save_button.setEnabled(
            ready
            and allowed
            and bool(normalize_character_name(self.role.currentText()))
            and (idle or warming)
        )

    def _start(self, operation, message, function, *arguments):
        if self.runner.active:
            return
        self.player.stop()
        self.cancellation.clear()
        self._operation = operation
        self.status.setText(message)
        self.controls.setEnabled(operation == "warm")
        self.progress.show()
        self.cancel_button.setText("Cancel and close")
        self.runner.start(function, *arguments)
        self._update()

    def discover(self, installation_root=None):
        if self.runner.active or self._closing:
            return
        self.characters.clear()
        self._start(
            "discover",
            "Finding installed game voices. First import may take a few minutes...",
            self.importer.narrator_characters,
            self.cancellation,
            installation_root,
        )

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Choose installed Reverse: 1999 folder"
        )
        if path:
            self.discover(Path(path))

    def _character_changed(self):
        self.player.stop()
        self._prepared.clear()
        self.references.clear()
        self._update()

    def _prepare(self):
        self._character = self.characters.currentText()
        self._prepared.clear()
        self.references.clear()
        self._start(
            "prepare",
            f"Listing spoken references for {self._character}. Please wait...",
            self.importer.narrator_references,
            self._character,
        )

    def _reference_changed(self):
        self._stop_audio()
        self.reference_text.setText(
            self.references.currentData(Qt.ItemDataRole.ToolTipRole)
            or ("Transcript unavailable." if self.references.count() else "")
        )
        self._warm_selected()

    def _stop_audio(self):
        self.player.stop()
        self._queued_action = None
        self._playback_requested = False

    def _warm_selected(self):
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
            self.status.setText("Original reference ready. Press Play to listen.")
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
            self.text.text().strip(),
        )

    def _decoder_progress(self, message):
        if self.runner.active and not self._closing:
            self.status.setText(message)

    def _original(self):
        self._candidate_action("audio")

    def _preview(self):
        self._candidate_action("preview")

    def _candidate_action(self, operation):
        if operation != "audio" and (
            not self._engine_available()
            or self.source.currentData() == "preset"
            and self.settings_value.speech_backend != "pocket-tts"
        ):
            self.status.setText(self.engine_guidance.text())
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
                self.text.text().strip(),
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
                "preview": "Preparing the selected reference and generating your preview...",
                "save": "Saving the selected narrator. Character voices stay unchanged...",
            }[operation],
            self._perform_candidate_action,
            operation,
            self._settings(),
            self._character,
            self.presets.currentData()
            if self.source.currentData() == "preset"
            else self.references.currentData(),
            self.text.text().strip(),
        )

    def _perform_candidate_action(
        self, operation, settings, character, reference, text
    ):
        manifest = None
        source_id = reference
        if not reference.startswith("preset:"):
            if reference not in self._prepared:
                self._prepared[reference] = self.importer.prepare_voice_roles(
                    (character,),
                    self.cancellation,
                    progress=self.decoderProgress.emit,
                    narrator=True,
                    narrator_line_id=reference,
                )
            manifest = self._prepared[reference]
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
        plan = narrator_preview_plan(settings, manifest, source_id, text)
        if operation == "audio":
            return self.previews.reference_audio(plan, plan.groups[0], source_id)
        return self.previews.generate(
            plan,
            plan.groups[0],
            plan.groups[0].source_id,
            cancel_event=self.cancellation,
            progress=self.decoderProgress.emit,
        ).path

    def _bind_selected_voice(self, settings, manifest, source_id, character):
        role = self._saving_role
        context = (
            {"additional_manifest": self._voice_context.voice_manifest}
            if self._voice_context is not None and self._voice_context.voice_manifest
            else {}
        )
        if normalize_character_name(role) == "narrator":
            return self.binder(settings, manifest, source_id, character, **context)
        # A normal default must not leave an old forced-live override shadowing it.
        assignments = {
            name: value
            for name, value in settings.voice_assignments.items()
            if normalize_character_name(name) != normalize_character_name(role)
        }
        return self.binder(
            settings.updated(voice_assignments=assignments),
            manifest,
            source_id,
            character,
            target_character=role,
            **context,
        )

    def _perform_catalog_action(self, operation, settings, source_id, text):
        voice = self._catalog_registry.resolve_source(source_id)
        if operation == "save":
            return self._bind_selected_voice(
                settings,
                self._catalog_manifest,
                source_id,
                voice.source_character or voice.character,
            )
        plan = narrator_preview_plan(settings, self._catalog_manifest, source_id, text)
        if operation == "audio":
            return self.previews.reference_audio(plan, plan.groups[0], source_id)
        return self.previews.generate(
            plan,
            plan.groups[0],
            source_id,
            cancel_event=self.cancellation,
            progress=self.decoderProgress.emit,
        ).path

    def _save(self):
        if not self.save_button.isEnabled():
            return
        self._saving_role = self.role.currentText().strip()
        if self.source.currentData() in {"preset", "automatic", "narrator"}:
            role = self.role.currentText().strip()
            narrator = normalize_character_name(role) == "narrator"
            assignments = {
                name: value
                for name, value in (
                    self._settings().voice_assignments
                    if narrator
                    else self._settings().character_voice_defaults
                ).items()
                if normalize_character_name(name) != normalize_character_name(role)
            }
            if self.source.currentData() != "automatic":
                assignments[role] = (
                    "default"
                    if self.source.currentData() == "narrator"
                    else self.presets.currentData()
                )
            self.result_settings = self._settings().updated(
                **{
                    "voice_assignments"
                    if narrator
                    else "character_voice_defaults": assignments
                }
            )
            if narrator:
                self.result_settings = self.result_settings.updated(
                    tts_speaker_wav=None
                )
            else:
                self.result_settings = self.result_settings.updated(
                    voice_assignments={
                        name: value
                        for name, value in self.result_settings.voice_assignments.items()
                        if normalize_character_name(name)
                        != normalize_character_name(role)
                    }
                )
            self._cleanup()
            return
        self._candidate_action("save")

    def _finished(self, result, error):
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
        self.cancel_button.setText("Cancel")
        if operation == "warm":
            if self.references.currentData() != self._warming_reference:
                self._warm_selected()
                self._update()
                return
            queued = self._queued_action
            self._queued_action = None
            if error is None:
                self.status.setText("Original reference ready. Press Play to listen.")
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
            if isinstance(error, DecoderSetupRequired) and confirm_decoder_setup(
                self, error
            ):
                self.importer.allow_decoder_homebrew = True
                self._candidate_action(operation)
                return
            self.status.setText(
                f"{error}\nRetry, choose a game folder, or cancel. Nothing was assigned."
            )
        elif operation == "discover":
            self.characters.addItems(result)
            self.status.setText(
                "Choose a character, then load its references."
                if result
                else "No voiced characters found. Choose the game folder to reimport."
            )
        elif operation == "prepare":
            choices = result
            self.references.blockSignals(True)
            self.references.clear()
            for index, choice in enumerate(choices, 1):
                self.references.addItem(
                    f"{self._character} - {choice.collection_title or f'Voice {choice.source_audio_id}'}",
                    choice.line_id,
                )
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
            if not self._playback_requested:
                self.status.setText("Audio ready. Playback stopped.")
                self._update()
                return
            self.status.setText(
                "Playing original reference."
                if operation == "audio"
                else "Playing generated preview."
            )
            self.player.setSource(QUrl.fromLocalFile(str(result)))
            self.player.play()
        elif operation == "save":
            self.result_settings = result
            self._cleanup()
            return
        self._update()

    def _cleanup(self):
        self._start("close", "Closing voice preview...", self.previews.close)

    def reject(self):
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

    def closeEvent(self, event):
        if self._closed:
            super().closeEvent(event)
        else:
            event.ignore()
            self.reject()
