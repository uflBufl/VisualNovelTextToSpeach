"""Qt review surface for bounded terminal authority conflicts."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Callable, Literal, Protocol, TypeAlias, TypedDict, TypeGuard

from PySide6.QtCore import QObject, Qt, QThreadPool, QUrl
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.review_context_ui import (
    ReviewDecisionContext,
    review_form_layout,
    review_scroll_area,
)
from vntts.authoring.terminal_conflict_review import (
    NEITHER_ACCEPTABLE,
    TerminalConflictReviewError,
    load_terminal_conflict_candidate_audio,
    load_terminal_conflict_review_document,
    load_terminal_conflict_review_progress,
    record_terminal_conflict_decision,
)
from vntts.qt_audio import QtPcmPlayer as QMediaPlayer


class ReviewCandidate(TypedDict):
    candidate_id: str
    authority: Literal["approved", "rejected"]
    audio_sha256: str


class ReviewCase(TypedDict):
    case_id: str
    line_id: str
    speaker: str
    voice_character: str
    text: str
    candidates: list[ReviewCandidate]


class ReviewDocument(TypedDict):
    review_id: str
    cases: list[ReviewCase]


class ReviewDecision(TypedDict):
    case_id: str
    decision: str


class ReviewProgress(TypedDict):
    decisions: list[ReviewDecision]


class _SignalConnector(Protocol):
    def connect(self, slot: Callable[..., object]) -> object: ...


class _AudioPlayer(Protocol):
    errorOccurred: _SignalConnector
    mediaStatusChanged: _SignalConnector

    def stop(self) -> None: ...

    def play_bytes(self, payload: bytes, source: str) -> object | None: ...

    def setSource(self, source: QUrl) -> None: ...


CandidateLoader: TypeAlias = Callable[[Path, str, str], bytes]
DecisionRecorder: TypeAlias = Callable[[Path, str, str], object]
DecisionConfirmer: TypeAlias = Callable[[str], bool]
AudioPlayerFactory: TypeAlias = Callable[[QObject | None], _AudioPlayer]
CandidatePayload: TypeAlias = tuple[str, str, str, int, bytes]
ReviewDocumentLoader: TypeAlias = Callable[[Path], object]
ReviewProgressLoader: TypeAlias = Callable[[Path], object]
AudioBytesPlayer: TypeAlias = Callable[
    [_AudioPlayer, QObject | None, bytes, str], object | None
]
AudioBufferReleaser: TypeAlias = Callable[[_AudioPlayer, object | None], None]

_review_document_loader: ReviewDocumentLoader = load_terminal_conflict_review_document
_review_progress_loader: ReviewProgressLoader = load_terminal_conflict_review_progress
_default_candidate_loader: CandidateLoader = load_terminal_conflict_candidate_audio
_default_decision_recorder: DecisionRecorder = record_terminal_conflict_decision


def _play_audio_bytes(
    player: _AudioPlayer, _parent: QObject | None, payload: bytes, source: str
) -> object | None:
    return player.play_bytes(payload, source)


def _release_audio_buffer(player: _AudioPlayer, _buffer: object | None) -> None:
    player.setSource(QUrl())


_audio_bytes_player: AudioBytesPlayer = _play_audio_bytes
_audio_buffer_releaser: AudioBufferReleaser = _release_audio_buffer


def _is_review_candidate(value: object) -> TypeGuard[ReviewCandidate]:
    return (
        isinstance(value, dict)
        and isinstance(value.get("candidate_id"), str)
        and value.get("authority") in {"approved", "rejected"}
        and isinstance(value.get("audio_sha256"), str)
    )


def _is_review_case(value: object) -> TypeGuard[ReviewCase]:
    candidates = value.get("candidates") if isinstance(value, dict) else None
    return (
        isinstance(value, dict)
        and all(
            isinstance(value.get(field), str)
            for field in (
                "case_id",
                "line_id",
                "speaker",
                "voice_character",
                "text",
            )
        )
        and isinstance(candidates, list)
        and all(_is_review_candidate(candidate) for candidate in candidates)
    )


def _review_document(value: object) -> ReviewDocument:
    cases = value.get("cases") if isinstance(value, dict) else None
    if not (
        isinstance(value, dict)
        and isinstance(value.get("review_id"), str)
        and isinstance(cases, list)
        and all(_is_review_case(case) for case in cases)
    ):
        raise TerminalConflictReviewError(
            "Terminal conflict review document is malformed"
        )
    return {"review_id": value["review_id"], "cases": cases}


def _review_progress(value: object) -> ReviewProgress:
    decisions = value.get("decisions") if isinstance(value, dict) else None
    if not (
        isinstance(decisions, list)
        and all(
            isinstance(decision, dict)
            and isinstance(decision.get("case_id"), str)
            and isinstance(decision.get("decision"), str)
            for decision in decisions
        )
    ):
        raise TerminalConflictReviewError(
            "Terminal conflict review progress is malformed"
        )
    return {
        "decisions": [
            {"case_id": decision["case_id"], "decision": decision["decision"]}
            for decision in decisions
        ]
    }


def _is_candidate_payload(value: object) -> TypeGuard[CandidatePayload]:
    return (
        isinstance(value, tuple)
        and len(value) == 5
        and isinstance(value[0], str)
        and isinstance(value[1], str)
        and isinstance(value[2], str)
        and isinstance(value[3], int)
        and isinstance(value[4], bytes)
    )


def _create_audio_player(parent: QObject) -> _AudioPlayer:
    factory: AudioPlayerFactory = QMediaPlayer
    return factory(parent)


class TerminalConflictReviewDialog(QDialog):
    """Play every distinct WAV and save one explicit winner per conflict."""

    def __init__(
        self,
        directory: str | Path,
        parent: QWidget | None = None,
        *,
        thread_pool: QThreadPool | None = None,
        candidate_loader: CandidateLoader = _default_candidate_loader,
        decision_recorder: DecisionRecorder = _default_decision_recorder,
        confirmer: DecisionConfirmer | None = None,
    ) -> None:
        super().__init__(parent)
        self.directory = Path(directory).expanduser().resolve()
        self.document = _review_document(_review_document_loader(self.directory))
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._decision_finished)
        self.playback_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.playback_runner.finished.connect(self._playback_prepared)
        self.playback_runner.activeChanged.connect(
            lambda _active: self._set_actions(True)
        )
        self.candidate_loader: CandidateLoader = candidate_loader
        self.decision_recorder: DecisionRecorder = decision_recorder
        self.confirmer: DecisionConfirmer = confirmer or self._confirm_decision
        self._active = False
        self._close_pending = False
        self._audio_buffer: object | None = None
        self._playing_candidate: str | None = None
        self._heard: set[str] = set()
        self._current: ReviewCase | None = None
        self._display_candidates: list[ReviewCandidate] = []

        self.setWindowTitle("Terminal audio conflict review")
        self.setMinimumSize(760, 420)
        self.progress = QLabel()
        self.progress.setAccessibleName("Terminal conflict review progress")
        self.decision_context = ReviewDecisionContext()
        self.identity = QLabel()
        self.identity.setWordWrap(True)
        self.identity.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.identity.setAccessibleName("Current terminal conflict identity")
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text.setAccessibleName("Current terminal conflict text")
        self.evidence = QLabel()
        self.evidence.setWordWrap(True)
        self.evidence.setAccessibleName("Required conflict listening evidence")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Terminal conflict review status")

        self.play_buttons: list[QPushButton] = []
        playback = review_form_layout()
        for index in range(2):
            button = QPushButton(f"Play candidate {chr(65 + index)}")
            button.setAccessibleName(f"Play terminal conflict candidate {index + 1}")
            button.setAccessibleDescription(
                "Play this checksum-distinct blind candidate through to the end"
            )
            button.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            button.clicked.connect(
                lambda _checked=False, value=index: self._play(value)
            )
            self.play_buttons.append(button)
        playback.addRow(*self.play_buttons)
        self.stop = QPushButton("Stop audio")
        self.stop.setAccessibleName("Stop terminal conflict audio")
        self.stop.setAccessibleDescription("Stop blind candidate playback")
        self.stop.setShortcut(QKeySequence("Ctrl+Space"))
        self.stop.clicked.connect(self._stop)
        self.stop.setEnabled(False)
        playback.addRow(self.stop)

        self.choose_buttons: list[QPushButton] = []
        decisions = review_form_layout()
        for index in range(2):
            button = QPushButton(f"Choose candidate {chr(65 + index)}")
            button.setAccessibleName(f"Choose terminal conflict candidate {index + 1}")
            button.setAccessibleDescription(
                "Keep this candidate as the terminal authority after both are heard"
            )
            button.setShortcut(QKeySequence(f"Alt+{index + 1}"))
            button.clicked.connect(
                lambda _checked=False, value=index: self._choose(value)
            )
            self.choose_buttons.append(button)
        decisions.addRow(*self.choose_buttons)
        self.neither = QPushButton("Neither is acceptable")
        self.neither.setAccessibleName("Reject both terminal conflict candidates")
        self.neither.setAccessibleDescription(
            "Require repair instead of keeping either terminal candidate"
        )
        self.neither.setShortcut(QKeySequence("Alt+N"))
        self.neither.clicked.connect(self._choose_neither)
        decisions.addRow(self.neither)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setAccessibleName("Close terminal conflict review")
        self.close_button.setAccessibleDescription(
            "Close this review without resolving the current conflict"
        )
        review_content = QWidget()
        review_layout = QVBoxLayout(review_content)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.addWidget(self.progress)
        review_layout.addWidget(self.decision_context)
        review_layout.addWidget(self.identity)
        review_layout.addWidget(self.text)
        review_layout.addLayout(playback)
        review_layout.addWidget(self.evidence)
        review_layout.addWidget(self.status)
        review_layout.addLayout(decisions)
        self.review_scroll = review_scroll_area(
            review_content,
            "Scrollable terminal conflict review",
        )
        layout = QVBoxLayout(self)
        layout.addWidget(self.review_scroll, 1)
        layout.addWidget(buttons)

        self.setTabOrder(self.decision_context.technical_toggle, self.play_buttons[0])
        self.setTabOrder(self.play_buttons[0], self.play_buttons[1])
        self.setTabOrder(self.play_buttons[1], self.stop)
        self.setTabOrder(self.stop, self.choose_buttons[0])
        self.setTabOrder(self.choose_buttons[0], self.choose_buttons[1])
        self.setTabOrder(self.choose_buttons[1], self.neither)
        self.setTabOrder(self.neither, self.close_button)

        self.player = _create_audio_player(self)
        self.player.errorOccurred.connect(self._playback_error)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self._load_next()

    def _decisions(self) -> dict[str, str]:
        progress = self.directory / "progress.json"
        if not progress.is_file():
            return {}
        document = _review_progress(_review_progress_loader(self.directory))
        return {value["case_id"]: value["decision"] for value in document["decisions"]}

    def _load_next(self) -> None:
        self._stop()
        self.document = _review_document(_review_document_loader(self.directory))
        decisions = self._decisions()
        total = len(self.document["cases"])
        self.progress.setText(f"Progress: {len(decisions)}/{total}")
        self._current = next(
            (
                case
                for case in self.document["cases"]
                if case["case_id"] not in decisions
            ),
            None,
        )
        current = self._current
        self._heard.clear()
        if current is None:
            self.decision_context.set_context(
                {
                    "purpose": "Resolve contradictory terminal WAV authorities",
                    "effect": "Review complete; no further decision is required",
                }
            )
            self.identity.setText("All terminal conflicts have an explicit decision.")
            self.text.clear()
            self.evidence.setText("No additional listening is required in this bundle.")
            self.status.setText(
                "Decisions are saved as review evidence. Source workspaces remain unchanged."
            )
            self._set_actions(False)
            return
        candidates = current["candidates"]
        if len(candidates) != 2:
            raise TerminalConflictReviewError(
                "The current UI supports exactly two distinct candidates per conflict"
            )
        self._display_candidates = sorted(
            candidates,
            key=lambda candidate: hashlib.sha256(
                (
                    self.document["review_id"]
                    + ":"
                    + current["case_id"]
                    + ":"
                    + candidate["candidate_id"]
                ).encode("utf-8")
            ).hexdigest(),
        )
        for index, button in enumerate(self.choose_buttons):
            button.setText(f"Choose candidate {chr(65 + index)}")
            button.setAccessibleName(f"Choose terminal conflict candidate {index + 1}")
        self.identity.setText(
            f"Line: {current['line_id']} | Speaker: {current['speaker']} | "
            f"Voice: {current['voice_character']}"
        )
        self.decision_context.set_context(
            {
                "purpose": "Resolve two contradictory historical WAV decisions",
                "game_speaker": current["speaker"],
                "synthesis_voice": current["voice_character"],
                "reference": "Hidden because the two candidates are compared blind",
                "backend": "Hidden with candidate authority until both are heard",
                "model": "Hidden with candidate authority until both are heard",
                "generation_profile": (
                    "Hidden with candidate authority until both are heard"
                ),
                "controls": "Two checksum-distinct historical WAV candidates",
                "effect": (
                    "keep one terminal authority, or require repair if neither is "
                    "acceptable"
                ),
            },
            technical=(
                f"Review: {self.document['review_id']}\n"
                f"Conflict: {current['case_id']}\n"
                f"Line: {current['line_id']}"
            ),
        )
        self.text.setText(current["text"])
        self.evidence.setText("Listen to both blind candidates before choosing.")
        self.status.setText("No source workspace will be changed by this decision.")
        self._set_actions(True)

    def _play(self, index: int) -> None:
        if self._active or self._current is None:
            return
        candidate = self._display_candidates[index]
        self._stop()
        self.status.setText(
            f"Preparing checksum-verified candidate {chr(65 + index)} in background..."
        )
        self.playback_runner.start(
            self._load_candidate_payload,
            self.candidate_loader,
            self.directory,
            self._current["case_id"],
            candidate["candidate_id"],
            candidate["audio_sha256"],
            index,
        )

    @staticmethod
    def _load_candidate_payload(
        loader: CandidateLoader,
        directory: Path,
        case_id: str,
        candidate_id: str,
        expected_sha256: str,
        index: int,
    ) -> CandidatePayload:
        payload = loader(directory, case_id, candidate_id)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise TerminalConflictReviewError("Terminal conflict candidate WAV changed")
        return case_id, candidate_id, expected_sha256, index, payload

    def _playback_prepared(self, result: object, error: Exception | None) -> None:
        if error is not None:
            self.status.setText(f"PLAYBACK BLOCKED: {error}")
            self._set_actions(True)
            return
        if not _is_candidate_payload(result):
            self.status.setText("PLAYBACK BLOCKED: candidate payload is malformed")
            self._set_actions(True)
            return
        case_id, candidate_id, expected_sha256, index, payload = result
        if self._current is None or self._current["case_id"] != case_id:
            self.status.setText("PLAYBACK CANCELLED: conflict selection changed")
            self._set_actions(True)
            return
        candidate = self._display_candidates[index]
        if (
            candidate["candidate_id"] != candidate_id
            or candidate["audio_sha256"] != expected_sha256
        ):
            self.status.setText("PLAYBACK CANCELLED: candidate selection changed")
            self._set_actions(True)
            return
        self._audio_buffer = _audio_bytes_player(
            self.player,
            self,
            payload,
            f"memory:terminal-candidate-{index + 1}.wav",
        )
        if self._audio_buffer is None:
            self.status.setText("PLAYBACK BLOCKED: immutable audio buffer failed")
            self._set_actions(True)
            return
        self._playing_candidate = candidate["candidate_id"]
        self.stop.setEnabled(True)
        self.evidence.setText(
            f"Playing candidate {chr(65 + index)}. It counts only after audio ends."
        )

    def _stop(self) -> None:
        if hasattr(self, "playback_runner"):
            self.playback_runner.cancel()
        self.player.stop()
        _audio_buffer_releaser(self.player, self._audio_buffer)
        self._audio_buffer = None
        self._playing_candidate = None
        self.stop.setEnabled(False)

    def _choose(self, index: int) -> None:
        if self._current is None:
            return
        self._save(self._display_candidates[index]["candidate_id"])

    def _choose_neither(self) -> None:
        self._save(NEITHER_ACCEPTABLE)

    def _save(self, decision: str) -> None:
        if self._active or self._current is None or len(self._heard) != 2:
            return
        if not self.confirmer(decision):
            self.status.setText("Decision cancelled; conflict evidence is unchanged.")
            return
        self._stop()
        self._active = True
        self._set_actions(False)
        self.status.setText(
            "Saving in background: rechecking report, state, queue and both WAVs..."
        )
        self.runner.start(
            self.decision_recorder,
            self.directory,
            self._current["case_id"],
            decision,
        )

    def _confirm_decision(self, _decision: str) -> bool:
        answer = QMessageBox.question(
            self,
            "Save irreversible conflict decision?",
            "Save this terminal conflict decision? This review window cannot "
            "revise it afterward.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return bool(answer == QMessageBox.StandardButton.Yes)

    def _decision_finished(self, _result: object, error: Exception | None) -> None:
        self._active = False
        if error is not None:
            self.status.setText(
                f"SAVE FAILED: {error}. Replay and retry are available."
            )
            self._set_actions(True)
        else:
            try:
                self._load_next()
            except Exception as refresh_error:
                self.status.setText(f"SAVED, BUT REFRESH FAILED: {refresh_error}")
                self._set_actions(False)
        if self._close_pending:
            self._close_pending = False
            self.close()

    def _set_actions(self, enabled: bool) -> None:
        enabled = bool(
            enabled
            and self._current is not None
            and not self._active
            and not self.playback_runner.active
        )
        for button in self.play_buttons:
            button.setEnabled(enabled)
        self.stop.setEnabled(
            self._current is not None
            and not self._active
            and (self.playback_runner.active or self._playing_candidate is not None)
        )
        self._update_decision_buttons()

    def _update_decision_buttons(self) -> None:
        enabled = (
            self._current is not None
            and not self._active
            and len(self._heard) == 2
            and not self.playback_runner.active
        )
        for button in self.choose_buttons:
            button.setEnabled(enabled)
        self.neither.setEnabled(enabled)

        if enabled:
            consequences = []
            for index, (button, candidate) in enumerate(
                zip(self.choose_buttons, self._display_candidates, strict=True)
            ):
                authority = candidate["authority"]
                label = chr(65 + index)
                button.setText(f"Keep {authority.title()} candidate {label}")
                button.setAccessibleName(
                    f"Keep historically {authority} terminal candidate {index + 1}"
                )
                consequence = (
                    "enters the approved manifest"
                    if authority == "approved"
                    else "remains rejected outside the manifest"
                )
                consequences.append(f"{label} was {authority} and {consequence}")
            self.evidence.setText(
                "Both candidates finished. " + "; ".join(consequences) + "."
            )

    def _media_status_changed(self, status: object) -> None:
        if (
            status != QMediaPlayer.MediaStatus.EndOfMedia
            or self._playing_candidate is None
        ):
            return
        self._heard.add(self._playing_candidate)
        self._playing_candidate = None
        self.stop.setEnabled(False)
        self.evidence.setText(
            f"Heard {len(self._heard)}/2 candidates. Replay remains available."
        )
        self._update_decision_buttons()

    def _playback_error(self, _error: object, error_string: str) -> None:
        self._playing_candidate = None
        self.stop.setEnabled(False)
        self.status.setText(f"PLAYBACK FAILED: {error_string}")
        self._update_decision_buttons()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._active:
            self._close_pending = True
            self.status.setText(
                "Close requested; waiting for the current save to finish."
            )
            event.ignore()
            return
        self._stop()
        super().closeEvent(event)


def launch_terminal_conflict_review(directory: str | Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    dialog = TerminalConflictReviewDialog(directory)
    dialog.show()
    return app.exec()


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review exact competing terminal authoring WAVs"
    )
    parser.add_argument("directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    options = create_parser().parse_args(argv)
    try:
        return launch_terminal_conflict_review(options.directory)
    except TerminalConflictReviewError as error:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "Unable to open terminal conflict review",
            f"Review directory: {options.directory.expanduser()}\n\n{error}",
        )
        app.processEvents()
        return 2


__all__ = [
    "TerminalConflictReviewDialog",
    "launch_terminal_conflict_review",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
