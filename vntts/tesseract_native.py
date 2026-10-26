"""Persistent Tesseract C API engine, owned by one isolated OCR worker."""

from __future__ import annotations

import ctypes as ct
import os
import shutil
import sys
from collections.abc import Callable
from ctypes.util import find_library
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PIL import Image
from pytesseract import pytesseract as runtime

from vntts.tesseract_config import SAUVOLA_PARAMETERS

if TYPE_CHECKING:
    from vntts.ocr import OcrData


_TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth"
    "\theight\tconf\ttext\n"
)


class _DllDirectory(Protocol):
    def close(self) -> None: ...


def _library_candidates(executable: str) -> list[str]:
    locations: list[Path] = []
    resolved = shutil.which(executable)
    if resolved:
        directory = Path(resolved).resolve().parent
        locations.extend((directory, directory.parent / "lib"))
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        locations.append(Path(bundle_root))
    candidates: list[str] = []
    for location in locations:
        for pattern in (
            "libtesseract*.dll",
            "tesseract[0-9]*.dll",
            "libtesseract*.dylib",
            "libtesseract.so*",
        ):
            candidates.extend(str(path) for path in sorted(location.glob(pattern)))
    found = find_library("tesseract")
    if found:
        candidates.append(found)
    if sys.platform == "darwin":
        for prefix in ("/opt/homebrew", "/usr/local"):
            path = Path(prefix) / "lib/libtesseract.dylib"
            if path.is_file():
                candidates.append(str(path))
    return list(dict.fromkeys(candidates))


def _load_library(
    executable: str, library: str | None
) -> tuple[ct.CDLL, _DllDirectory | None]:
    errors = []
    for candidate in [library] if library else _library_candidates(executable):
        dll_directory = None
        try:
            if sys.platform == "win32" and Path(candidate).is_absolute():
                # Keep dependency resolution active for this DLL's whole lifetime.
                dll_directory = os.add_dll_directory(str(Path(candidate).parent))
            return ct.CDLL(candidate), dll_directory
        except OSError as error:
            if dll_directory is not None:
                dll_directory.close()
            errors.append(f"{candidate}: {error}")
    details = "; ".join(errors) or "no shared library found"
    raise RuntimeError(
        "Unable to load native Tesseract. Install a matching Tesseract shared "
        "library and its dependencies, or set VNTTS_TESSERACT_LIBRARY to its "
        f"full path. Configured executable: {executable}. {details}"
    )


def _tessdata_directory(executable: str, configured: str | None) -> str | None:
    if configured is not None:
        return str(Path(configured).expanduser())
    prefix = os.environ.get("TESSDATA_PREFIX")
    if prefix:
        return str(Path(prefix).expanduser())
    resolved = shutil.which(executable)
    if resolved:
        directory = Path(resolved).resolve().parent
        for candidate in (
            directory / "tessdata",
            directory.parent / "share/tessdata",
            directory.parent / "share/tesseract-ocr/5/tessdata",
        ):
            if candidate.is_dir():
                return str(candidate)
    return None


class NativeTesseract:
    def __init__(
        self,
        language: str,
        *,
        executable: str = "tesseract",
        tessdata_dir: str | None = None,
        library: str | None = None,
    ) -> None:
        if not isinstance(language, str) or not language.strip():
            raise ValueError("Tesseract language must not be empty")
        self.language = language
        self._handle: int | None = None
        self._lib, self._dll_directory = _load_library(executable, library)
        try:
            self._bind()
            directory = _tessdata_directory(executable, tessdata_dir)
            self._handle = self._lib.TessBaseAPICreate()
            if not self._handle:
                raise RuntimeError("Unable to create native Tesseract engine")
            if (
                self._lib.TessBaseAPIInit3(
                    self._handle,
                    os.fsencode(directory) if directory is not None else None,
                    language.encode("utf-8"),
                )
                != 0
            ):
                raise RuntimeError(
                    f"Unable to load Tesseract models for {language!r} from "
                    f"{directory or 'the library default tessdata directory'}. "
                    "Install the requested traineddata files and set TESSDATA_PREFIX "
                    "to their directory."
                )
            self._check_loaded_languages()
            for name, value in SAUVOLA_PARAMETERS:
                if not self._lib.TessBaseAPISetVariable(
                    self._handle, name.encode("ascii"), value.encode("ascii")
                ):
                    raise RuntimeError(
                        f"Native Tesseract does not support required Sauvola setting "
                        f"{name}={value}. Install Tesseract 5 or select a matching "
                        "VNTTS_TESSERACT_LIBRARY."
                    )
        except BaseException:
            self.close()
            raise

    def _bind(self) -> None:
        signatures = {
            "TessBaseAPICreate": (ct.c_void_p, []),
            "TessBaseAPIInit3": (ct.c_int, [ct.c_void_p, ct.c_char_p, ct.c_char_p]),
            "TessBaseAPISetVariable": (
                ct.c_int,
                [ct.c_void_p, ct.c_char_p, ct.c_char_p],
            ),
            "TessBaseAPIGetLoadedLanguagesAsVector": (
                ct.POINTER(ct.c_char_p),
                [ct.c_void_p],
            ),
            "TessDeleteTextArray": (None, [ct.POINTER(ct.c_char_p)]),
            "TessBaseAPISetPageSegMode": (None, [ct.c_void_p, ct.c_int]),
            "TessBaseAPISetImage": (None, [ct.c_void_p, ct.c_char_p] + [ct.c_int] * 4),
            "TessBaseAPIRecognize": (ct.c_int, [ct.c_void_p, ct.c_void_p]),
            "TessBaseAPIGetUTF8Text": (ct.c_void_p, [ct.c_void_p]),
            "TessBaseAPIGetTsvText": (ct.c_void_p, [ct.c_void_p, ct.c_int]),
            "TessDeleteText": (None, [ct.c_void_p]),
            "TessBaseAPIClear": (None, [ct.c_void_p]),
            "TessBaseAPIClearAdaptiveClassifier": (None, [ct.c_void_p]),
            "TessBaseAPIDelete": (None, [ct.c_void_p]),
        }
        try:
            for name, (result, arguments) in signatures.items():
                function = getattr(self._lib, name)
                function.restype = result
                function.argtypes = arguments
        except AttributeError as error:
            raise RuntimeError(
                "The Tesseract shared library lacks the required C API; install "
                "Tesseract 5 or select a matching VNTTS_TESSERACT_LIBRARY."
            ) from error

    def _check_loaded_languages(self) -> None:
        languages = self._lib.TessBaseAPIGetLoadedLanguagesAsVector(self._handle)
        if not languages:
            raise RuntimeError("Unable to inspect loaded native Tesseract languages")
        loaded = set()
        try:
            index = 0
            while languages[index]:
                loaded.add(languages[index].decode("utf-8"))
                index += 1
        finally:
            self._lib.TessDeleteTextArray(languages)
        required = {
            code
            for code in self.language.split("+")
            if code and not code.startswith("~")
        }
        missing = required - loaded
        if missing:
            raise RuntimeError(
                f"Tesseract did not load the requested languages: {', '.join(sorted(missing))}. "
                "Install their traineddata files in TESSDATA_PREFIX."
            )

    def close(self) -> None:
        try:
            if self._handle:
                self._lib.TessBaseAPIDelete(self._handle)
                self._handle = None
        finally:
            if self._dll_directory is not None:
                self._dll_directory.close()
                self._dll_directory = None

    def _text(self, function: Callable[..., int | None], *arguments: int) -> str:
        pointer = function(self._handle, *arguments)
        if not pointer:
            raise RuntimeError("Native Tesseract did not return recognized output")
        try:
            return ct.string_at(pointer).decode("utf-8")
        finally:
            self._lib.TessDeleteText(pointer)

    def recognize(
        self, image: Image.Image, psm: int, *, text: bool, data: bool
    ) -> tuple[str, OcrData]:
        if not self._handle:
            raise RuntimeError("Native Tesseract engine is closed")
        if image.mode != "L" or image.width <= 0 or image.height <= 0:
            raise ValueError("Native Tesseract requires a nonempty grayscale L image")
        if isinstance(psm, bool) or not isinstance(psm, int) or not 0 <= psm <= 13:
            raise ValueError("Tesseract page segmentation mode must be in 0..13")
        if not text and not data:
            raise ValueError("At least one Tesseract output must be requested")
        pixels = image.tobytes()
        self._lib.TessBaseAPIClearAdaptiveClassifier(self._handle)
        try:
            self._lib.TessBaseAPISetPageSegMode(self._handle, psm)
            self._lib.TessBaseAPISetImage(
                self._handle, pixels, image.width, image.height, 1, image.width
            )
            if self._lib.TessBaseAPIRecognize(self._handle, None) != 0:
                raise RuntimeError("Native Tesseract recognition failed")
            recognized = self._text(self._lib.TessBaseAPIGetUTF8Text) if text else ""
            result = {}
            if data:
                # The C API emits rows; the CLI TSV renderer adds the header.
                tsv = self._text(self._lib.TessBaseAPIGetTsvText, 0)
                result = runtime.file_to_dict(_TSV_HEADER + tsv, "\t", -1)
            return recognized, result
        finally:
            self._lib.TessBaseAPIClear(self._handle)
            self._lib.TessBaseAPIClearAdaptiveClassifier(self._handle)
