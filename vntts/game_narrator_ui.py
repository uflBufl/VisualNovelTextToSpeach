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
from vntts.speech_presentation import (
    engine_model_label,
    narrator_voice_label,
    speech_runtime_label,
)
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
        self._prepared = {}
        self._character = None
        self._operation = None
        self._warming_reference = None
        self._queued_action = None
        self._playback_requested = False
        self._closing = False
        self._closed = False
        self._preview_reused = False

        self.status = QLabel("Choose a candidate. Nothing changes until you save.")
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
        self.references.currentIndexChanged.connect(self._reference_changed)
        game_form.addRow("Reference", self.references)
        self.reference_text = QLabel()
        self.reference_text.setWordWrap(True)
        self.reference_text.setTextFormat(Qt.TextFormat.PlainText)
        self.reference_text.setAccessibleName("Original reference transcript")
        game_form.addRow(self.reference_text)
        self.original_button = QPushButton("Play original reference")
        self.original_button.clicked.connect(self._original)
        self.text = QLineEdit("The storm has passed. We can continue our journey.")
        form.addRow("Preview text", self.text)
        self.preview_button = QPushButton("Generate and play preview")
        self.preview_button.clicked.connect(self._preview)
        self.save_button = QPushButton("Save narrator")
        self.save_button.clicked.connect(self._save)
        self.stop_button = QPushButton("Stop audio")
        self.stop_button.clicked.connect(self._stop_audio)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.runtime)
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
        self.runtime_timer = QTimer(self)
        self.runtime_timer.setInterval(500)
        self.runtime_timer.timeout.connect(self._refresh_runtime)
        self.finished.connect(self.runtime_timer.stop)
        self.runtime_timer.start()
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
        self._refresh_runtime()
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
        warming = self.runner.active and self._operation == "warm"
        self.prepare_button.setEnabled(self.characters.count() > 0 and idle)
        self.characters.setEnabled(idle)
        self.source.setEnabled(idle)
        self.discover_button.setEnabled(idle)
        self.folder_button.setEnabled(idle)
        self.original_button.setEnabled(ready and not preset and (idle or warming))
        self.original_button.setToolTip(
            "Built-in voices have no original game reference."
            if preset
            else "Play the original recording, not generated speech."
        )
        self.preview_button.setEnabled(ready and allowed and (idle or warming))
        self.save_button.setEnabled(ready and allowed and (idle or warming))

    def _refresh_runtime(self):
        if self._operation == "preview":
            message = (
                "Saved preview: no generation for this playback."
                if self._preview_reused
                else speech_runtime_label(getattr(self.previews, "backend", None))
            )
        else:
            message = "No preview generation. Original recordings play without TTS."
        self.runtime.setText(message)

    def _start(self, operation, message, function, *arguments):
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
        if (
            operation != "audio"
            and self.source.currentData() == "game"
            and self.settings_value.speech_backend == "pocket-tts"
            and not self.consent.isChecked()
        ):
            self.status.setText(
                "Accept Pocket's terms before generating or saving a game voice."
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
            return self.binder(settings, manifest, source_id, character)
        plan = narrator_preview_plan(settings, manifest, source_id, text)
        if operation == "audio":
            return self.previews.reference_audio(plan, plan.groups[0], source_id)
        return self.previews.generate(
            plan,
            plan.groups[0],
            plan.groups[0].source_id,
            cancel_event=self.cancellation,
            progress=self.decoderProgress.emit,
        )

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
            if operation == "preview":
                self._preview_reused = getattr(result, "reused", False) is True
                result = result.path
            if not self._playback_requested:
                self.status.setText("Audio ready. Playback stopped.")
                self._update()
                return
            self.status.setText(
                "Playing original reference."
                if operation == "audio"
                else "Playing saved preview (no generation)."
                if self._preview_reused
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
