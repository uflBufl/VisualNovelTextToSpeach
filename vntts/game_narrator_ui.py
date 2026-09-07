"""Guided game narrator discovery, reference listening and synthesis preview."""

from functools import partial
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
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
from vntts.speech_presentation import engine_model_label, narrator_voice_label
from vntts.tts_benchmark import create_backend
from vntts.voices import (
    CharacterVoiceRegistry,
    find_voice_assignment,
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
        self.setWindowTitle("Choose narrator")
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
        self._manifest = None
        self._character = None
        self._operation = None
        self._closing = False
        self._closed = False

        self.status = QLabel("Choose a candidate. Nothing changes until you save.")
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
        self.engine = QLabel()
        self.engine.setWordWrap(True)
        form.addRow(self.engine)
        current = QLabel(f"Saved narrator: {narrator_voice_label(settings)}")
        current.setWordWrap(True)
        form.addRow(current)
        self.source = QComboBox()
        self.source.setAccessibleName("Narrator voice source")
        self.source.addItem("Game character", "game")
        if self.settings_value.speech_backend == "pocket-tts":
            self.source.addItem("Built-in Pocket voice", "preset")
        selected = find_voice_assignment(
            settings.voice_assignments, "Narrator"
        ) or pregeneration_narrator_source_id(self.settings_value)
        if selected.startswith("preset:") and self.source.count() > 1:
            self.source.setCurrentIndex(1)
        form.addRow("Voice source", self.source)
        self.presets = QComboBox()
        self.presets.setAccessibleName("Built-in narrator candidate")
        for name in pocket_tts_preset_voices:
            self.presets.addItem(name.replace("_", " ").title(), f"preset:{name}")
        self.presets.setCurrentIndex(max(0, self.presets.findData(selected)))
        self.presets.currentIndexChanged.connect(lambda: self.player.stop())
        form.addRow(self.presets)
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
        game_form.addRow(self.terms)
        game_form.addRow(self.consent)
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
        self.references.currentIndexChanged.connect(lambda: self.player.stop())
        game_form.addRow("Reference", self.references)
        self.original_button = QPushButton("Play original reference")
        self.original_button.clicked.connect(self._original)
        self.text = QLineEdit("The storm has passed. We can continue our journey.")
        form.addRow("Preview text", self.text)
        self.preview_button = QPushButton("Generate and play preview")
        self.preview_button.clicked.connect(self._preview)
        self.save_button = QPushButton("Save narrator")
        self.save_button.clicked.connect(self._save)
        self.stop_button = QPushButton("Stop audio")
        self.stop_button.clicked.connect(self.player.stop)
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
            "Applies to future preparation and live fallback. Existing recordings and character voices stay unchanged."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        actions = QHBoxLayout()
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        self.player.errorOccurred.connect(
            lambda _code, message: self.status.setText(message)
        )
        self.source.currentIndexChanged.connect(self._source_changed)
        self._update()
        QTimer.singleShot(0, self._source_changed)

    def _source_changed(self):
        if self._closing or self._closed:
            return
        self.player.stop()
        preset = self.source.currentData() == "preset"
        self.form.setRowVisible(self.presets, preset)
        self.form.setRowVisible(self.game_controls, not preset)
        self.status.setText(
            "Built-in voices need no game references or Hugging Face account. Generate a preview, then save."
            if preset
            else "Choose a game character and load an original reference."
        )
        self._update()
        if not preset and not self.characters.count() and not self.runner.active:
            self.discover()

    def _settings(self):
        return self.settings_value.updated(
            pocket_gated_model_accepted=self.consent.isChecked()
        )

    def _update(self):
        settings = self._settings()
        preset = self.source.currentData() == "preset"
        self.engine.setText(
            engine_model_label(
                settings.speech_backend,
                settings.tts_model,
                pocket_cloning=not preset,
            )
        )
        allowed = (
            preset
            or settings.speech_backend != "pocket-tts"
            or self.consent.isChecked()
        )
        ready = preset or self.references.count() > 0
        idle = not self.runner.active and not self._closed and not self._closing
        self.prepare_button.setEnabled(self.characters.count() > 0)
        self.original_button.setEnabled(ready and not preset and idle)
        self.original_button.setToolTip(
            "Built-in voices have no original game reference."
            if preset
            else "Play the original recording, not generated speech."
        )
        self.preview_button.setEnabled(ready and allowed and idle)
        self.save_button.setEnabled(ready and allowed and idle)

    def _start(self, operation, message, function, *arguments):
        if self.runner.active:
            return
        self.player.stop()
        self.cancellation.clear()
        self._operation = operation
        self.status.setText(message)
        self.controls.setEnabled(False)
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
        self._manifest = None
        self.references.clear()
        self._update()

    def _prepare(self):
        self._character = self.characters.currentText()
        self._start(
            "prepare",
            f"Extracting references for {self._character}. Please wait...",
            self._load_references,
            self._character,
        )

    def _load_references(self, character):
        manifest = self.importer.prepare_voice_roles(
            (character,),
            self.cancellation,
            progress=self.decoderProgress.emit,
            narrator=True,
        )
        registry = CharacterVoiceRegistry.from_file(manifest)
        return manifest, registry.choices()

    def _decoder_progress(self, message):
        if self.runner.active and self._operation == "prepare" and not self._closing:
            self.status.setText(message)

    def _plan(self):
        return narrator_preview_plan(
            self._settings(),
            self._manifest,
            self.presets.currentData()
            if self.source.currentData() == "preset"
            else self.references.currentData(),
            self.text.text().strip(),
        )

    def _original(self):
        try:
            plan = self._plan()
        except Exception as error:
            self.status.setText(str(error))
            return
        self._start(
            "audio",
            "Checking original reference...",
            self.previews.reference_audio,
            plan,
            plan.groups[0],
            plan.groups[0].source_id,
        )

    def _preview(self):
        try:
            plan = self._plan()
        except Exception as error:
            self.status.setText(str(error))
            return
        self._start(
            "preview",
            "Loading the selected model and generating your preview...",
            self._generate,
            plan,
        )

    def _generate(self, plan):
        return self.previews.generate(
            plan,
            plan.groups[0],
            plan.groups[0].source_id,
            cancel_event=self.cancellation,
        ).path

    def _save(self):
        if self.source.currentData() == "preset":
            assignments = {
                name: value
                for name, value in self._settings().voice_assignments.items()
                if normalize_character_name(name) != "narrator"
            }
            assignments["Narrator"] = self.presets.currentData()
            self.result_settings = self._settings().updated(
                voice_assignments=assignments
            )
            self._cleanup()
            return
        self._start(
            "save",
            "Saving narrator references. Existing character voices are preserved...",
            self.binder,
            self._settings(),
            self._manifest,
            self.references.currentData(),
            self._character,
        )

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
        if error is not None:
            if isinstance(error, DecoderSetupRequired) and confirm_decoder_setup(
                self, error
            ):
                self.importer.allow_decoder_homebrew = True
                self._prepare()
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
            self._manifest, choices = result
            self.references.clear()
            for index, choice in enumerate(choices, 1):
                self.references.addItem(
                    f"{self._character} - reference {index}", choice.id
                )
            self.status.setText(
                "Listen to the original and generated preview, then save your narrator."
                if choices
                else "No usable references found. Choose another character."
            )
        elif operation in {"audio", "preview"}:
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
