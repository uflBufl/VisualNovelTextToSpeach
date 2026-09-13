import argparse
import os
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from typing import Protocol, TypeAlias

from PIL import Image
from vntts_artifacts.atomic_io import atomic_write_json

from vntts.cli import CLIReportResult
from vntts.ocr import (
    default_dialog_region,
    default_minimum_ocr_confidence,
    recognize_dialog_image_result,
)
from vntts.services.tts_engine import TTSEngine
from vntts.settings import get_local_data_directory
from vntts.voices import CharacterVoice, CharacterVoiceRegistry
from vntts.window_capture import WindowCaptureTarget

default_smoke_test_model = "tts_models/en/vctk/vits"
default_auto_advance_timeout_seconds = 8.0
PathInput: TypeAlias = str | os.PathLike[str]
SmokeReport: TypeAlias = dict[str, object]


class _Recognition(Protocol):
    character: str
    text: str
    confidence: float

    def is_confident(self, minimum: float) -> bool: ...


class _Recognizer(Protocol):
    def __call__(
        self,
        image: Image.Image,
        voice_registry: CharacterVoiceRegistry | None,
        *,
        minimum_confidence: float,
    ) -> _Recognition: ...


class _Capture(Protocol):
    def __call__(
        self,
        *,
        save_screenshot: bool,
        capture_target: WindowCaptureTarget,
    ) -> tuple[Image.Image, object]: ...


class _SpeechEngine(Protocol):
    def speak(self, text: str) -> object: ...


class _EngineFactory(Protocol):
    def __call__(self, *, model_name: str) -> _SpeechEngine: ...


def configure_release_smoke_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-smoke-test-image")
    parser.add_argument("--release-smoke-test-window-title")
    parser.add_argument("--release-smoke-test-report")
    parser.add_argument("--release-smoke-test-model", default=default_smoke_test_model)
    parser.add_argument("--release-smoke-test-expected-speaker")
    parser.add_argument("--release-smoke-test-auto-advance-expected-text")


def _write_report(report: SmokeReport, report_path: PathInput | None) -> Path:
    report_path = (
        get_local_data_directory() / "release-smoke-test.json"
        if report_path is None
        else Path(report_path).expanduser()
    )
    atomic_write_json(report_path, report)
    return report_path


def run_release_smoke_test(
    *,
    image_path: PathInput | None = None,
    window_title: str | None = None,
    report_path: PathInput | None = None,
    model_name: str = default_smoke_test_model,
    expected_speaker: str | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    recognize: _Recognizer | None = None,
    engine_factory: _EngineFactory | None = None,
    capture: _Capture | None = None,
    auto_advance_expected_text: str | None = None,
    auto_advance: Callable[[], bool] | None = None,
    auto_advance_timeout_seconds: float = default_auto_advance_timeout_seconds,
) -> CLIReportResult:
    checks: list[SmokeReport] = []
    recognized_text = ""
    recognized_speaker = ""
    confidence = 0.0
    auto_advance_dispatched = False
    auto_advance_acknowledged = False
    try:
        if bool(image_path) == bool(window_title):
            raise ValueError("Provide exactly one smoke-test image or window title")
        if auto_advance_expected_text and not window_title:
            raise ValueError("Auto-advance verification requires a window title")

        if image_path:
            image_path = Path(image_path).expanduser().resolve()
            with Image.open(image_path) as screenshot:
                image = default_dialog_region.crop(screenshot.convert("RGB"))
            capture_source = str(image_path)
        else:
            if capture is None:
                from vntts.dialog_capture import capture_dialog

                capture = capture_dialog
            image, _output = capture(
                save_screenshot=False,
                capture_target=WindowCaptureTarget(window_title),
            )
            capture_source = f"window:{window_title}"
        checks.append(
            {
                "name": "Dialog capture",
                "status": "ok",
                "message": capture_source,
            }
        )

        recognizer: _Recognizer = (
            recognize_dialog_image_result if recognize is None else recognize
        )
        voice_registry = None
        if expected_speaker:
            voice_registry = CharacterVoiceRegistry(
                [CharacterVoice(expected_speaker, "release-smoke-test")]
            )
        result = recognizer(
            image,
            voice_registry,
            minimum_confidence=minimum_confidence,
        )
        recognized_text = result.text
        recognized_speaker = result.character
        confidence = result.confidence
        if not recognized_text.strip():
            raise RuntimeError("OCR did not recognize dialog text")
        if not result.is_confident(minimum_confidence):
            raise RuntimeError(
                f"OCR confidence {confidence:.0f}% is below {minimum_confidence:g}%"
            )
        if (
            expected_speaker
            and recognized_speaker.casefold() != expected_speaker.casefold()
        ):
            raise RuntimeError(
                f"Expected speaker {expected_speaker!r}, got {recognized_speaker!r}"
            )
        checks.append(
            {
                "name": "Tesseract OCR",
                "status": "ok",
                "message": (
                    f"{recognized_speaker}: {recognized_text} "
                    f"({confidence:.0f}% confidence)"
                ),
            }
        )

        selected_engine_factory: _EngineFactory = (
            TTSEngine if engine_factory is None else engine_factory
        )
        engine = selected_engine_factory(model_name=model_name)
        engine.speak(recognized_text)
        checks.append(
            {
                "name": "Speech synthesis and playback",
                "status": "ok",
                "message": model_name,
            }
        )
        if auto_advance_expected_text:
            assert window_title is not None
            assert capture is not None
            selected_auto_advance = auto_advance or (
                lambda: _production_auto_advance(window_title)
            )
            if selected_auto_advance() is not True:
                raise RuntimeError(
                    "Production controller did not dispatch auto advance"
                )
            auto_advance_dispatched = True
            deadline = monotonic() + auto_advance_timeout_seconds
            while monotonic() < deadline:
                image, _output = capture(
                    save_screenshot=False,
                    capture_target=WindowCaptureTarget(window_title),
                )
                advanced = recognizer(
                    image,
                    voice_registry,
                    minimum_confidence=minimum_confidence,
                )
                speaker_matches = (
                    not expected_speaker
                    or advanced.character.casefold() == expected_speaker.casefold()
                )
                if (
                    advanced.is_confident(minimum_confidence)
                    and speaker_matches
                    and auto_advance_expected_text.casefold()
                    in advanced.text.casefold()
                ):
                    auto_advance_acknowledged = True
                    break
                sleep(0.1)
            if not auto_advance_acknowledged:
                raise RuntimeError(
                    "Auto advance was dispatched but the fixture did not show the "
                    "expected next dialog"
                )
            checks.append(
                {
                    "name": "Production auto advance",
                    "status": "ok",
                    "message": auto_advance_expected_text,
                }
            )
    except Exception as error:
        checks.append(
            {
                "name": "Release smoke test",
                "status": "error",
                "message": str(error),
            }
        )

    successful = all(check["status"] == "ok" for check in checks)
    report = {
        "success": successful,
        "source": str(image_path or f"window:{window_title}"),
        "model": model_name,
        "speaker": recognized_speaker,
        "text": recognized_text,
        "confidence": confidence,
        "auto_advance_dispatched": auto_advance_dispatched,
        "auto_advance_acknowledged": auto_advance_acknowledged,
        "auto_advance_controller": "AppController._auto_advance_dialog",
        "checks": checks,
    }
    return CLIReportResult(successful, _write_report(report, report_path))


def _production_auto_advance(window_title: str) -> bool:
    from vntts.controller import AppController
    from vntts.settings import AppSettings

    controller = AppController(
        AppSettings(
            capture_mode="window",
            game_window_title=window_title,
            auto_advance_enabled=True,
            live_sequence_mode="off",
        )
    )
    try:
        return bool(controller._auto_advance_dialog())
    finally:
        controller.shutdown()
