"""One-at-a-time voice verification for self-service pregeneration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from traceback import format_exception
from typing import Protocol, TypeAlias

from PySide6.QtCore import QSignalBlocker, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.file_integrity import sha256_file

from vntts.async_ui import LatestTaskRunner
from vntts.pregeneration_audition import (
    VoiceAuditionError,
    VoiceAuditionPreviewService,
)
from vntts.pregeneration_voices import VoiceCandidate, VoiceGroup, VoicePlan
from vntts.qt_audio import QtPcmPlayer as QMediaPlayer
from vntts.speech_presentation import speech_runtime_label
from vntts.voices import default_voice_choice_id


class _PreviewAudio(Protocol):
    @property
    def path(self) -> Path: ...


CandidateEntry: TypeAlias = tuple[VoiceCandidate, str, bool]
DisplayedEntry: TypeAlias = tuple[VoiceCandidate, _PreviewAudio | None, str]
PreviewKey: TypeAlias = tuple[str, str | None]
PendingDecision: TypeAlias = tuple[VoiceGroup, str]


class _VoiceDecisionRecorder(Protocol):
    def remember_many(self, selections: tuple[PendingDecision, ...]) -> None: ...


class _VoiceAuditionPreviewer(Protocol):
    @property
    def backend(self) -> object | None: ...

    def generate(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
    ) -> _PreviewAudio: ...

    def reference_audio(
        self, plan: VoicePlan, group: VoiceGroup, candidate_source_id: str
    ) -> Path | None: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


class _PreviewPlayer(Protocol):
    def stop(self) -> None: ...

    def play_bytes(self, payload: bytes, source: str) -> object | None: ...


_preview_service_factory: Callable[[], _VoiceAuditionPreviewer] = (
    VoiceAuditionPreviewService
)
_speech_runtime_label: Callable[[object | None], str] = speech_runtime_label


class VoiceAuditionUIError(RuntimeError):
    """The player audition card received an invalid unresolved plan."""


class VoiceAuditionPanel(QGroupBox):
    completed = Signal()
    cancelled = Signal()
    saveFailed = Signal()

    def __init__(
        self,
        decisions: _VoiceDecisionRecorder,
        *,
        preview_service: _VoiceAuditionPreviewer | None = None,
        thread_pool: QThreadPool | None = None,
        player: _PreviewPlayer | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Inspect story voice", parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.decisions = decisions
        self._owns_preview_service = preview_service is None
        self.preview_service: _VoiceAuditionPreviewer = (
            preview_service or _preview_service_factory()
        )
        self.preview_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preview_runner.finished.connect(self._preview_finished)
        self.decision_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decision_runner.finished.connect(self._decision_finished)
        self.player: _PreviewPlayer | None = player
        self._plan: VoicePlan | None = None
        self._groups: tuple[VoiceGroup, ...] = ()
        self._group_index = 0
        self._candidate_offset = 0
        self._candidate_entries: tuple[CandidateEntry, ...] = ()
        self._previews: dict[PreviewKey, _PreviewAudio] = {}
        self._failed_candidate_source_ids: set[str] = set()
        self._displayed: tuple[DisplayedEntry, ...] = ()
        self._sample_text: str | None = None
        self._pending_decisions: list[PendingDecision] = []
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._ignore_preview_result = False
        self._shutdown_requested = False
        self._inspection_mode = False
        self._playing_source: str | None = None

        self.summary = QLabel()
        self.summary.setAccessibleName("Voice choice estimate")
        self.summary.setWordWrap(True)
        self.choose_all_button = QPushButton("Choose all automatically")
        self.choose_all_button.clicked.connect(self.choose_all_automatically)
        summary_row = QHBoxLayout()
        summary_row.addWidget(self.summary, 1)
        summary_row.addWidget(self.choose_all_button)

        self.character = QLabel()
        self.character.setAccessibleName("Character needing a voice choice")
        self.character.setWordWrap(True)
        self.character.setStyleSheet("font-weight: 600;")
        self.portrait_image = QLabel()
        self.portrait_image.setAccessibleName("Exact game character portrait")
        self.portrait_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.portrait_image.setVisible(False)
        self.scope = QLabel()
        self.scope.setAccessibleName("Voice choice scope")
        self.scope.setWordWrap(True)
        self.runtime = QLabel()
        self.runtime.setWordWrap(True)
        self.runtime.setTextFormat(Qt.TextFormat.PlainText)
        self.runtime.setAccessibleName("Character voice preview compute device")
        self.voice_reference = QComboBox()
        self.voice_reference.setAccessibleName("Voice reference")
        self.voice_reference.currentIndexChanged.connect(self._voice_reference_selected)
        voice_reference_row = QHBoxLayout()
        voice_reference_row.addWidget(QLabel("Voice reference"))
        voice_reference_row.addWidget(self.voice_reference, 1)

        self.preview_phrase = QComboBox()
        self.preview_phrase.setAccessibleName("Generated preview phrase")
        self.preview_phrase.currentIndexChanged.connect(self._preview_phrase_selected)

        self.a_box = QGroupBox()
        self.a_box.setAccessibleName("Voice sample")
        self.a_reason = QLabel()
        self.a_reason.setAccessibleName("Voice sample details")
        self.a_reason.setWordWrap(True)
        self.reference_details_toggle = QCheckBox("Technical reference details")
        self.reference_details = QLabel()
        self.reference_details.setWordWrap(True)
        self.reference_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.reference_details.hide()
        self.reference_details_toggle.toggled.connect(self.reference_details.setVisible)
        self.a_play = QPushButton("Play generated preview")
        self.a_play.setMinimumWidth(self.a_play.sizeHint().width())
        self.a_play.setText("Generate preview")
        self.a_play.setAccessibleName("Play generated voice preview")
        self.a_play.clicked.connect(self.play_a)
        self.a_original = QPushButton("Play original reference")
        self.a_original.setAccessibleName("Play original reference")
        self.a_original.clicked.connect(self._play_original)
        self.a_use = QPushButton("Use this voice")
        self.a_use.setAccessibleName("Use selected character voice")
        self.a_use.clicked.connect(self.use_a)
        reference_row = QHBoxLayout()
        reference_row.addWidget(self.a_reason, 1)
        reference_row.addWidget(self.a_original)
        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel("Preview phrase"))
        preview_row.addWidget(self.preview_phrase, 1)
        preview_row.addWidget(self.a_play)
        candidate_layout = QVBoxLayout(self.a_box)
        candidate_layout.addLayout(reference_row)
        candidate_layout.addWidget(self.reference_details_toggle)
        candidate_layout.addWidget(self.reference_details)

        self.auto_button = QPushButton("Choose for me")
        self.auto_button.setToolTip("Opt out of verifying this sample yourself")
        self.auto_button.clicked.connect(self.choose_for_me)
        self.retry_save_button = QPushButton("Retry saving choices")
        self.retry_save_button.clicked.connect(self._start_save)
        self.retry_save_button.setVisible(False)
        outcomes = QHBoxLayout()
        outcomes.addWidget(self.auto_button)
        outcomes.addStretch()
        outcomes.addWidget(self.retry_save_button)
        outcomes.addWidget(self.a_use)

        self.status = QLabel()
        self.status.setAccessibleName("Voice preview status")
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addLayout(summary_row)
        layout.addWidget(self.portrait_image)
        layout.addWidget(self.character)
        layout.addWidget(self.scope)
        layout.addLayout(voice_reference_row)
        layout.addWidget(self.a_box)
        layout.addLayout(preview_row)
        layout.addLayout(outcomes)
        layout.addWidget(self.status)
        layout.addWidget(self.runtime)
        self.runtime_timer = QTimer(self)
        self.runtime_timer.setInterval(500)
        self.runtime_timer.timeout.connect(self._refresh_runtime)
        self.preview_runner.activeChanged.connect(self._refresh_runtime)
        self._refresh_runtime()
        self._connect_player()
        self.setVisible(False)

    def _refresh_runtime(self) -> None:
        if self.preview_runner.active and self._shutdown_requested:
            message = "Stopping voice preview preparation..."
        elif self.preview_runner.active:
            message = "Preparing this voice preview. " + _speech_runtime_label(
                self.preview_service.backend
            )
        else:
            message = "No preview generation. Saved previews play without TTS."
        self.runtime.setText(message)
        self.runtime.setVisible(self.preview_runner.active or self._shutdown_requested)

    @property
    def active(self) -> bool:
        return bool(self.preview_runner.active or self.decision_runner.active)

    def start(self, plan: VoicePlan, *, group_id: str | None = None) -> None:
        if not isinstance(plan, VoicePlan):
            raise VoiceAuditionUIError("Voice audition plan is invalid")
        groups = tuple(
            group
            for group in plan.groups
            if (group_id is None and group.route == "needs-audition")
            or group.group_id == group_id
        )
        if not groups:
            raise VoiceAuditionUIError("Voice plan has no matching character")
        if any(
            not group.candidates and group.narrator_candidate is None
            for group in groups
        ):
            raise VoiceAuditionUIError("This character has no voice to inspect")
        if self._shutdown_requested and self._owns_preview_service:
            self.preview_service = _preview_service_factory()
        self._plan = plan
        self._inspection_mode = group_id is not None
        self._groups = groups
        self._group_index = 0
        self._pending_decisions = []
        self._failed_candidate_source_ids.clear()
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._ignore_preview_result = False
        self._shutdown_requested = False
        self.retry_save_button.setVisible(False)
        self.summary.setVisible(not self._inspection_mode)
        self.choose_all_button.setVisible(not self._inspection_mode)
        self.auto_button.setVisible(not self._inspection_mode)
        self.runtime_timer.start()
        self.setVisible(True)
        self._show_group()

    def current_group(self) -> VoiceGroup:
        return self._groups[self._group_index]

    def play_a(self) -> None:
        if self._playing_source == "preview":
            self._stop_playback()
            return
        if self.preview_runner.active or self.decision_runner.active:
            return
        candidate, _choice, _narrator = self._current_entry()
        preview = self._previews.get(self._preview_key(candidate))
        if preview is not None:
            self._displayed = ((candidate, preview, self._current_entry()[1]),)
            self._set_decision_actions(True)
            self._play_preview(preview)
            return
        self._stop_player()
        self._displayed = ()
        self._set_decision_actions(False)
        self.status.setText("Preparing this generated preview...")
        self.preview_runner.start(
            self.preview_service.generate,
            self._require_plan(),
            self.current_group(),
            candidate.source_id,
            text=self._sample_text,
        )

    def use_a(self) -> None:
        if not self._displayed:
            return
        _candidate, _preview, choice = self._displayed[0]
        self._record_choice(choice)

    def choose_for_me(self) -> None:
        if self.preview_runner.active or self.decision_runner.active:
            return
        if self._inspection_mode:
            self.cancel()
            return
        source_id = self._automatic_source_id(self.current_group())
        if source_id is None:
            self.status.setText(
                "No working automatic voice remains. Retry this preview, or return "
                "to the voice plan and use Edit selected role in Voices."
            )
            return
        self._record_choice(source_id)

    def choose_all_automatically(self) -> None:
        if self.decision_runner.active or self._cancel_requested:
            return
        choices: list[PendingDecision] = []
        for group in self._groups[self._group_index :]:
            source_id = self._automatic_source_id(group)
            if source_id is None:
                self.status.setText(
                    "No working automatic voice remains. Retry the failed preview, "
                    "or return to the voice plan and use Edit selected role in Voices."
                )
                return
            choices.append((group, source_id))
        self._pending_decisions.extend(choices)
        self._group_index = len(self._groups)
        self._ignore_preview_result = self.preview_runner.active
        if self.preview_runner.active:
            self.preview_service.cancel()
        self._stop_player()
        self._start_save(
            "VNTTS selected the recommended voice for every remaining character."
        )

    def _automatic_source_id(self, group: VoiceGroup) -> str | None:
        for candidate in group.candidates:
            if candidate.source_id not in self._failed_candidate_source_ids:
                source_id = candidate.source_id
                if isinstance(source_id, str):
                    return source_id
        narrator = group.narrator_candidate
        if (
            narrator is not None
            and narrator.source_id not in self._failed_candidate_source_ids
        ):
            return (
                default_voice_choice_id
                if isinstance(default_voice_choice_id, str)
                else None
            )
        return None

    def cancel(self) -> None:
        if self._terminal_emitted:
            return
        if self.decision_runner.active:
            self.status.setText(
                "Finishing the voice choice save before returning to the voice plan..."
            )
            return
        self._cancel_requested = True
        self._stop_player()
        self.preview_service.cancel()
        self._set_decision_actions(False)
        self.choose_all_button.setEnabled(False)
        self.status.setText("Cancelling voice selection...")
        if not self.active:
            self._emit_cancelled()

    def shutdown(self) -> None:
        self._stop_player()
        self._shutdown_requested = True
        self.runtime_timer.stop()
        self._refresh_runtime()
        if self.preview_runner.active:
            self.preview_service.cancel()
            return
        if not self.active:
            self.preview_service.close()

    def _show_group(self) -> None:
        self._candidate_offset = 0
        self._previews = {}
        self._displayed = ()
        self.reference_details_toggle.setChecked(False)
        group = self.current_group()
        candidates = (
            group.candidate_inventory if self._inspection_mode else group.candidates
        )
        entries: list[CandidateEntry] = [
            (candidate, candidate.source_id, False) for candidate in candidates
        ]
        if group.narrator_candidate is not None:
            entries.append((group.narrator_candidate, default_voice_choice_id, True))
        entries.sort(key=lambda entry: entry[0].source_id != group.source_id)
        self._candidate_entries = tuple(entries)
        with QSignalBlocker(self.voice_reference):
            self.voice_reference.clear()
            for candidate, _choice, narrator in self._candidate_entries:
                label = "Narrator fallback" if narrator else candidate.source_character
                self.voice_reference.addItem(label)
                self.voice_reference.setItemData(
                    self.voice_reference.count() - 1,
                    label,
                    Qt.ItemDataRole.ToolTipRole,
                )
            self.voice_reference.setCurrentIndex(0)
        self.summary.setText(
            "Review the suggested voices, or keep every automatic choice."
            if self._inspection_mode
            else f"Voice sample {self._group_index + 1} of {len(self._groups)}."
        )
        self.character.setText(group.character)
        self._show_portrait(group)
        count = len(group.line_ids)
        self.scope.setText(
            f"Used for {count} selected line{'s' if count != 1 else ''}. Saving also "
            f"changes {group.character}'s voice for future speech and preparation; "
            "existing prepared audio is unchanged."
        )
        with QSignalBlocker(self.preview_phrase):
            self.preview_phrase.clear()
            self.preview_phrase.addItem(group.sample_text, group.sample_text)
            self.preview_phrase.setItemData(
                0, group.sample_text, Qt.ItemDataRole.ToolTipRole
            )
            if group.alternate_sample_text is not None:
                self.preview_phrase.addItem(
                    group.alternate_sample_text, group.alternate_sample_text
                )
                self.preview_phrase.setItemData(
                    1,
                    group.alternate_sample_text,
                    Qt.ItemDataRole.ToolTipRole,
                )
            self.preview_phrase.setCurrentIndex(0)
        self._sample_text = group.sample_text
        self.voice_reference.setToolTip(self.voice_reference.currentText())
        self.preview_phrase.setToolTip(self.preview_phrase.currentText())
        self.voice_reference.setEnabled(len(self._candidate_entries) > 1)
        self.preview_phrase.setEnabled(self.preview_phrase.count() > 1)
        self.choose_all_button.setEnabled(True)
        self._show_current_candidate()

    def _voice_reference_selected(self, index: int) -> None:
        if (
            self.preview_runner.active
            or self.decision_runner.active
            or not 0 <= index < len(self._candidate_entries)
        ):
            return
        self._candidate_offset = index
        self.voice_reference.setToolTip(self.voice_reference.currentText())
        self._show_current_candidate()

    def _preview_phrase_selected(self, index: int) -> None:
        if self.preview_runner.active or self.decision_runner.active:
            return
        text = self.preview_phrase.itemData(index)
        if not isinstance(text, str) or not text:
            return
        self._sample_text = text
        self.preview_phrase.setToolTip(text)
        self._show_current_candidate()

    def _current_entry(self) -> CandidateEntry:
        return self._candidate_entries[self._candidate_offset]

    def _show_current_candidate(self) -> None:
        self._stop_player()
        self._displayed = ()
        candidate, _choice, narrator = self._current_entry()
        self.a_box.setTitle(
            "Original reference for narrator"
            if narrator
            else f"Original reference for {candidate.source_character}"
        )
        reference_count = len(candidate.reference_sha256s)
        reference_summary = (
            f"{reference_count} original reference"
            + ("s" if reference_count != 1 else "")
            + (
                f" · {candidate.reference_duration_seconds:.1f} s total"
                if candidate.reference_duration_seconds is not None
                else ""
            )
            if reference_count
            else "No original reference"
        )
        self.a_reason.setText(f"{reference_summary}\n{candidate.recommendation}")
        details = [f"Voice source: {candidate.source_speaker}"]
        details.extend(
            f"{index}. SHA-256 {checksum}"
            for index, checksum in enumerate(candidate.reference_sha256s, 1)
        )
        if candidate.source_line_ids:
            details.append("Source lines: " + ", ".join(candidate.source_line_ids))
        self.reference_details.setText(
            "\n".join(details) if details else "No recorded reference files."
        )
        preview = self._previews.get(self._preview_key(candidate))
        self.a_play.setText(
            "Play generated preview" if preview is not None else "Generate preview"
        )
        self.a_play.setEnabled(True)
        self.a_original.setEnabled(bool(candidate.reference_sha256s))
        self.a_original.setToolTip(
            "Listen to the original reference used for this voice."
            if candidate.reference_sha256s
            else "This voice has no recorded reference."
        )
        self.a_use.setText(
            "Use narrator voice" if narrator else f"Use {candidate.source_character}"
        )
        self.a_use.setAccessibleDescription(
            "Save this voice for future speech and story preparation"
        )
        self.a_use.setEnabled(preview is not None)
        self.a_box.setVisible(True)
        self.voice_reference.setEnabled(len(self._candidate_entries) > 1)
        self.preview_phrase.setEnabled(self.preview_phrase.count() > 1)
        self.auto_button.setText("Choose for me")
        self.auto_button.setEnabled(True)
        if preview is not None:
            self._displayed = ((candidate, preview, self._current_entry()[1]),)
            self.status.setText("Replay the preview or use this verified voice.")
        else:
            self.status.setText(
                "Listen to the reference or generate a preview, then use this voice if suitable."
                if candidate.reference_sha256s
                else "Generate a preview before using this voice."
            )

    def _preview_finished(
        self, preview: _PreviewAudio, error: Exception | None
    ) -> None:
        if self._shutdown_requested:
            if not self.active:
                self.preview_service.close()
            return
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if self._ignore_preview_result:
            self._ignore_preview_result = False
            self._maybe_complete()
            return
        candidate, choice, _narrator = self._current_entry()
        if error is not None:
            from vntts.support import record_game_import

            record_game_import(
                "voice-preview",
                outcome="failed",
                command_kind="preview",
                exception_type=type(error).__name__,
                reason=str(error),
                traceback_tail="".join(format_exception(error))[-12000:],
            )
            self._failed_candidate_source_ids.add(candidate.source_id)
            self.a_play.setEnabled(True)
            self.a_original.setEnabled(bool(candidate.reference_sha256s))
            self.a_use.setEnabled(False)
            self.voice_reference.setEnabled(len(self._candidate_entries) > 1)
            self.preview_phrase.setEnabled(self.preview_phrase.count() > 1)
            self.auto_button.setEnabled(True)
            self.status.setText(
                f"Could not prepare {candidate.source_character}'s preview: {error}. "
                + (
                    "Try another voice, retry this preview, or go back to keep the "
                    "automatic choice."
                    if self._inspection_mode
                    else "Try another voice, retry this preview, or choose automatically."
                )
            )
            return
        self._failed_candidate_source_ids.discard(candidate.source_id)
        self._previews[self._preview_key(candidate)] = preview
        self._displayed = ((candidate, preview, choice),)
        self._set_decision_actions(True)
        self.a_play.setText("Play generated preview")
        self.status.setText(
            "Playing the generated preview. Use this voice only if the sample is suitable."
        )
        if not self._play_preview(preview):
            self._displayed = ()
            self.a_use.setEnabled(False)

    def _play_preview(self, preview: _PreviewAudio) -> bool:
        try:
            payload = preview.path.read_bytes()
        except OSError as error:
            self.status.setText(f"Unable to play generated preview: {error}")
            return False
        player = self._ensure_player()
        player.stop()
        playing = player.play_bytes(payload, source=str(preview.path)) is not None
        if playing:
            self._set_playing_source("preview")
            self.status.setText(
                "Playing the generated preview. Use this voice only if the sample "
                "is suitable."
            )
        return playing

    def _preview_key(self, candidate: VoiceCandidate) -> PreviewKey:
        return candidate.source_id, self._sample_text

    def _play_original(self) -> None:
        if self._playing_source == "original":
            self._stop_playback()
            return
        if self._cancel_requested or self._shutdown_requested or self.active:
            return
        candidate, _choice, _narrator = self._current_entry()
        self._stop_player()
        self._displayed = ()
        self.a_use.setEnabled(False)
        try:
            reference = self.preview_service.reference_audio(
                self._require_plan(), self.current_group(), candidate.source_id
            )
        except (OSError, ValueError, VoiceAuditionError) as error:
            self.status.setText(f"Unable to play original reference: {error}")
            return
        if reference is None:
            self.status.setText("This voice has no recorded reference.")
            return
        try:
            payload = reference.read_bytes()
        except OSError as error:
            self.status.setText(f"Unable to play original reference: {error}")
            return
        player = self._ensure_player()
        self.status.setText(
            f"Starting the original reference for {candidate.source_character}..."
        )
        if player.play_bytes(payload, source=str(reference)) is None:
            return
        self._set_playing_source("original")
        self._displayed = ((candidate, None, self._current_entry()[1]),)
        self.a_use.setEnabled(True)

    def _record_choice(self, source_id: str, status_message: str | None = None) -> None:
        if self._group_index >= len(self._groups) or self.decision_runner.active:
            return
        self._stop_player()
        self._pending_decisions.append((self.current_group(), source_id))
        self._group_index += 1
        if self._group_index < len(self._groups):
            self._show_group()
            return
        self._start_save(status_message)

    def _start_save(self, status_message: str | None = None) -> None:
        if self.decision_runner.active or not self._pending_decisions:
            return
        self.retry_save_button.setVisible(False)
        self._set_decision_actions(False)
        self.choose_all_button.setEnabled(False)
        self.status.setText(
            f"{status_message} Saving in the background..."
            if status_message
            else "Saving voice choices in the background..."
        )
        self.decision_runner.start(
            self.decisions.remember_many,
            tuple(self._pending_decisions),
        )

    def _decision_finished(self, _result: object, error: Exception | None) -> None:
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if error is not None:
            self.retry_save_button.setVisible(True)
            self.retry_save_button.setEnabled(True)
            self.status.setText(
                "Unable to save the voice choices. Nothing was lost; retry when "
                "the application data directory is writable."
            )
            self.saveFailed.emit()
            return
        self._save_succeeded = True
        self._maybe_complete()

    def _maybe_complete(self) -> None:
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if not self._save_succeeded or self.active:
            return
        self._terminal_emitted = True
        self.runtime_timer.stop()
        self.setVisible(False)
        self.completed.emit()

    def _set_decision_actions(self, enabled: bool) -> None:
        enabled = bool(enabled) and not self._cancel_requested
        candidate = self._current_entry()[0] if self._candidate_entries else None
        self.a_play.setEnabled(enabled and self.a_box.isVisible())
        self.a_use.setEnabled(enabled and bool(self._displayed))
        self.a_original.setEnabled(
            enabled
            and self.a_box.isVisible()
            and candidate is not None
            and bool(candidate.reference_sha256s)
        )
        self.voice_reference.setEnabled(enabled and len(self._candidate_entries) > 1)
        self.preview_phrase.setEnabled(enabled and self.preview_phrase.count() > 1)
        self.auto_button.setEnabled(enabled)

    def _show_portrait(self, group: VoiceGroup) -> None:
        self.portrait_image.clear()
        self.portrait_image.setVisible(False)
        if not group.portrait_image or not group.portrait_image_sha256:
            return
        try:
            if sha256_file(group.portrait_image) != group.portrait_image_sha256:
                return
        except OSError:
            return
        pixmap = QPixmap(group.portrait_image)
        if pixmap.isNull():
            return
        self.portrait_image.setPixmap(
            pixmap.scaled(
                180,
                180,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.portrait_image.setVisible(True)

    def _emit_cancelled(self) -> None:
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        self._shutdown_requested = True
        self.runtime_timer.stop()
        self._refresh_runtime()
        self.setVisible(False)
        if self._owns_preview_service:
            self.preview_service.close()
        self.cancelled.emit()

    def _ensure_player(self) -> _PreviewPlayer:
        if self.player is None:
            player = QMediaPlayer(self)
            player.errorOccurred.connect(
                lambda _code, message: self.status.setText(
                    f"Unable to play voice sample: {message}"
                )
            )
            self.player = player
            self._connect_player()
        return self.player

    def _connect_player(self) -> None:
        signal = getattr(self.player, "playbackStateChanged", None)
        connect = getattr(signal, "connect", None)
        if callable(connect):
            connect(self._playback_state_changed)

    def _playback_state_changed(self, state: object) -> None:
        if getattr(state, "name", "") == "StoppedState":
            if self._playing_source is not None:
                self.status.setText("Playback finished. Use this voice if suitable.")
            self._set_playing_source(None)

    def _stop_playback(self) -> None:
        self._stop_player()
        self.status.setText("Playback stopped.")

    def _set_playing_source(self, source: str | None) -> None:
        self._playing_source = source
        self.a_original.setText(
            "Stop original" if source == "original" else "Play original reference"
        )
        self.a_original.setAccessibleName(
            "Stop original reference"
            if source == "original"
            else "Play original reference"
        )
        current = self._current_entry()[0] if self._candidate_entries else None
        preview_ready = bool(
            current is not None and self._previews.get(self._preview_key(current))
        )
        self.a_play.setText(
            "Stop preview"
            if source == "preview"
            else "Play generated preview"
            if preview_ready
            else "Generate preview"
        )
        self.a_play.setAccessibleName(
            "Stop generated voice preview"
            if source == "preview"
            else "Play generated voice preview"
            if preview_ready
            else "Generate voice preview"
        )

    def _stop_player(self) -> None:
        if self.player is not None:
            self.player.stop()
        self._set_playing_source(None)

    def _require_plan(self) -> VoicePlan:
        assert self._plan is not None
        return self._plan


__all__ = ["VoiceAuditionPanel", "VoiceAuditionUIError"]
