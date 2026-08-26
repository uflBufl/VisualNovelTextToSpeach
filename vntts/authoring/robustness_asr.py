"""Diagnostic-only ASR comparison for a speech robustness corpus."""

from __future__ import annotations

import copy
import io
import json
import re
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    canonical_document_sha256,
    capture_authority_file,
    write_json_document_no_replace,
)
from vntts.authoring.bulk_generation import BulkGenerationError, sha256_control_path
from vntts.authoring.robustness_corpus import (
    SpeechRobustnessCorpusError,
    load_speech_robustness_corpus,
)

SPEECH_ROBUSTNESS_ASR_SCHEMA = "vntts.speech-robustness-asr-report"
SPEECH_ROBUSTNESS_ASR_VERSION = 1


class SpeechRobustnessAsrError(RuntimeError):
    """ASR diagnostic evidence cannot be produced or validated."""


@dataclass(frozen=True)
class SpeechRobustnessAsrReport:
    """One validated text/content diagnostic report."""

    report_id: str
    document: dict
    corpus_directory: Path

    def to_dict(self):
        return copy.deepcopy(self.document)


def _words(text):
    return tuple(
        token.lower().replace("’", "'")
        for token in re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text, flags=re.UNICODE)
    )


def _edit_counts(expected, observed):
    rows = len(expected) + 1
    columns = len(observed) + 1
    table = [[None] * columns for _ in range(rows)]
    table[0][0] = (0, 0, 0, 0)
    for row in range(1, rows):
        table[row][0] = (row, 0, 0, row)
    for column in range(1, columns):
        table[0][column] = (column, 0, column, 0)
    for row in range(1, rows):
        for column in range(1, columns):
            if expected[row - 1] == observed[column - 1]:
                table[row][column] = table[row - 1][column - 1]
                continue
            substitution = table[row - 1][column - 1]
            insertion = table[row][column - 1]
            deletion = table[row - 1][column]
            candidates = (
                (
                    substitution[0] + 1,
                    substitution[1] + 1,
                    substitution[2],
                    substitution[3],
                ),
                (
                    insertion[0] + 1,
                    insertion[1],
                    insertion[2] + 1,
                    insertion[3],
                ),
                (
                    deletion[0] + 1,
                    deletion[1],
                    deletion[2],
                    deletion[3] + 1,
                ),
            )
            table[row][column] = min(candidates)
    distance, substitutions, insertions, deletions = table[-1][-1]
    return {
        "distance": distance,
        "substitutions": substitutions,
        "insertions": insertions,
        "deletions": deletions,
    }


def compare_speech_transcript(expected_text, observed_text):
    """Return deterministic word-level content metrics for one transcript."""
    expected = _words(expected_text)
    observed = _words(observed_text)
    edits = _edit_counts(expected, observed)
    denominator = max(1, len(expected))
    return {
        "expected_word_count": len(expected),
        "observed_word_count": len(observed),
        **edits,
        "word_error_rate": round(edits["distance"] / denominator, 6),
        "missing_word_rate": round(edits["deletions"] / denominator, 6),
        "inserted_word_rate": round(edits["insertions"] / denominator, 6),
    }


class _WhisperTranscriber:
    def __init__(self, model_directory, *, device="cpu"):
        try:
            from transformers import (
                AutoModelForSpeechSeq2Seq,
                AutoProcessor,
                pipeline,
            )

            processor = AutoProcessor.from_pretrained(
                model_directory, local_files_only=True
            )
            model = AutoModelForSpeechSeq2Seq.from_pretrained(
                model_directory, local_files_only=True
            )
            model.eval()
            self._pipeline = pipeline(
                "automatic-speech-recognition",
                model=model,
                tokenizer=processor.tokenizer,
                feature_extractor=processor.feature_extractor,
                device=device,
            )
        except Exception as error:
            raise SpeechRobustnessAsrError(
                f"Unable to load local ASR model: {error}"
            ) from error

    @staticmethod
    def _input(payload):
        try:
            with wave.open(io.BytesIO(payload), "rb") as source:
                rate = source.getframerate()
                samples = np.frombuffer(
                    source.readframes(source.getnframes()), dtype="<i2"
                ).astype(np.float32)
        except Exception as error:
            raise SpeechRobustnessAsrError(
                f"Unable to decode robustness WAV for ASR: {error}"
            ) from error
        return {"array": samples / 32768.0, "sampling_rate": rate}

    @staticmethod
    def _text(result):
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str):
            raise SpeechRobustnessAsrError("ASR returned no transcript text")
        return text.strip()

    def __call__(self, payload):
        try:
            result = self._pipeline(
                self._input(payload),
                return_timestamps=True,
            )
        except Exception as error:
            raise SpeechRobustnessAsrError(
                f"Unable to transcribe robustness WAV: {error}"
            ) from error
        return self._text(result)

    def transcribe_many(self, payloads):
        try:
            # Transformers emits incompatible short/long-form preprocessing
            # keys for mixed-rate WAVs. Keep one loaded model but call it per
            # payload; the outer loop still checkpoints each bounded batch.
            results = [
                self._pipeline(self._input(payload), return_timestamps=True)
                for payload in payloads
            ]
        except Exception as error:
            raise SpeechRobustnessAsrError(
                f"Unable to transcribe robustness WAV batch: {error}"
            ) from error
        return [self._text(result) for result in results]


def _distribution(records, metric):
    values = sorted(float(record["comparison"][metric]) for record in records)
    if not values:
        return {"count": 0, "mean": None, "median": None, "maximum": None}
    midpoint = len(values) // 2
    median = (
        values[midpoint]
        if len(values) % 2
        else (values[midpoint - 1] + values[midpoint]) / 2
    )
    return {
        "count": len(values),
        "mean": round(sum(values) / len(values), 6),
        "median": round(median, 6),
        "maximum": round(values[-1], 6),
    }


def _summary(records):
    labels = sorted({record["human_label"] for record in records})
    providers = sorted({record["provider"] for record in records})
    groups = {}
    for label in labels:
        selected = [record for record in records if record["human_label"] == label]
        groups[f"label:{label}"] = {
            metric: _distribution(selected, metric)
            for metric in ("word_error_rate", "missing_word_rate", "inserted_word_rate")
        }
    for provider in providers:
        for label in labels:
            selected = [
                record
                for record in records
                if record["provider"] == provider and record["human_label"] == label
            ]
            if selected:
                groups[f"provider:{provider}:{label}"] = {
                    metric: _distribution(selected, metric)
                    for metric in (
                        "word_error_rate",
                        "missing_word_rate",
                        "inserted_word_rate",
                    )
                }
    return {
        "sample_count": len(records),
        "human_labels": dict(Counter(record["human_label"] for record in records)),
        "providers": dict(Counter(record["provider"] for record in records)),
        "groups": groups,
    }


def _validate_document(document):
    fields = {
        "schema",
        "schema_version",
        "report_id",
        "corpus_id",
        "corpus_schema_version",
        "asr",
        "policy",
        "records",
        "summary",
    }
    if not isinstance(document, dict) or set(document) != fields:
        raise SpeechRobustnessAsrError("ASR robustness report shape is invalid")
    if (
        document.get("schema") != SPEECH_ROBUSTNESS_ASR_SCHEMA
        or document.get("schema_version") != SPEECH_ROBUSTNESS_ASR_VERSION
        or document.get("policy")
        != {"diagnostic_only": True, "automatic_rejection": False}
    ):
        raise SpeechRobustnessAsrError("ASR robustness report policy is invalid")
    expected_id = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "report_id"}
    )
    if document.get("report_id") != expected_id:
        raise SpeechRobustnessAsrError("ASR robustness report identity is invalid")
    records = document.get("records")
    if not isinstance(records, list) or document.get("summary") != _summary(records):
        raise SpeechRobustnessAsrError("ASR robustness report summary is invalid")
    return document


def _progress_document(corpus_id, model_sha256, device, records):
    body = {
        "schema": "vntts.speech-robustness-asr-progress",
        "schema_version": 1,
        "corpus_id": corpus_id,
        "model_sha256": model_sha256,
        "device": device,
        "records": records,
    }
    return {**body, "progress_id": canonical_document_sha256(body)}


def _load_progress(path, *, corpus_id, model_sha256, device, samples):
    if path is None or not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SpeechRobustnessAsrError(
            f"Unable to load ASR progress: {error}"
        ) from error
    expected = {
        "schema",
        "schema_version",
        "corpus_id",
        "model_sha256",
        "device",
        "records",
        "progress_id",
    }
    if (
        not isinstance(document, dict)
        or set(document) != expected
        or document.get("schema") != "vntts.speech-robustness-asr-progress"
        or document.get("schema_version") != 1
        or document.get("corpus_id") != corpus_id
        or document.get("model_sha256") != model_sha256
        or document.get("device") != device
        or document.get("progress_id")
        != canonical_document_sha256(
            {key: value for key, value in document.items() if key != "progress_id"}
        )
        or not isinstance(document.get("records"), list)
        or len(document["records"]) > len(samples)
    ):
        raise SpeechRobustnessAsrError("ASR progress authority is invalid")
    for record, sample in zip(document["records"], samples, strict=False):
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("expected_text"), str)
            or not isinstance(record.get("observed_text"), str)
            or record.get("workspace_id") != sample["workspace_id"]
            or record.get("queue_id") != sample["queue_id"]
            or record.get("audio_sha256") != sample["audio_sha256"]
            or record.get("text_sha256") != sample["text_sha256"]
            or record.get("expected_text") != sample["text"]
            or record.get("comparison")
            != compare_speech_transcript(
                record["expected_text"], record["observed_text"]
            )
        ):
            raise SpeechRobustnessAsrError("ASR progress record authority is invalid")
    return document["records"]


def build_speech_robustness_asr_report(
    corpus_directory,
    model_directory,
    *,
    device="cpu",
    transcriber=None,
    progress_path=None,
):
    """Transcribe a v2 corpus and compare exact requested/observed words."""
    try:
        corpus = load_speech_robustness_corpus(corpus_directory)
    except SpeechRobustnessCorpusError as error:
        raise SpeechRobustnessAsrError(str(error)) from error
    if corpus.document["schema_version"] < 2:
        raise SpeechRobustnessAsrError(
            "ASR comparison requires a v2 robustness corpus with exact text"
        )
    model = Path(model_directory).expanduser().resolve()
    try:
        model_sha256 = sha256_control_path(model)
    except BulkGenerationError as error:
        raise SpeechRobustnessAsrError(str(error)) from error
    progress = (
        None if progress_path is None else Path(progress_path).expanduser().resolve()
    )
    if progress is not None:
        try:
            progress.relative_to(corpus.directory)
        except ValueError:
            pass
        else:
            raise SpeechRobustnessAsrError(
                "ASR progress must be outside the immutable corpus directory"
            )
    records = _load_progress(
        progress,
        corpus_id=corpus.corpus_id,
        model_sha256=model_sha256,
        device=device,
        samples=corpus.document["samples"],
    )
    remaining = corpus.document["samples"][len(records) :]
    transcribe = transcriber
    if remaining and transcribe is None:
        transcribe = _WhisperTranscriber(model, device=device)
    batch_size = 8 if hasattr(transcribe, "transcribe_many") else 1
    for offset in range(0, len(remaining), batch_size):
        batch = remaining[offset : offset + batch_size]
        payloads = []
        for sample in batch:
            try:
                audio = capture_authority_file(
                    corpus.directory / sample["audio"],
                    "robustness ASR audio",
                    root=corpus.directory,
                )
            except AuthoringAuthorityError as error:
                raise SpeechRobustnessAsrError(str(error)) from error
            if audio.sha256 != sample["audio_sha256"]:
                raise SpeechRobustnessAsrError(
                    "Robustness ASR audio changed after corpus validation"
                )
            payloads.append(audio.payload)
        observed_texts = (
            transcribe.transcribe_many(payloads)
            if hasattr(transcribe, "transcribe_many")
            else [transcribe(payloads[0])]
        )
        if len(observed_texts) != len(batch) or not all(
            isinstance(value, str) for value in observed_texts
        ):
            raise SpeechRobustnessAsrError(
                "ASR transcriber returned invalid batch text"
            )
        for sample, observed in zip(batch, observed_texts, strict=True):
            records.append(
                {
                    "workspace_id": sample["workspace_id"],
                    "queue_id": sample["queue_id"],
                    "audio_sha256": sample["audio_sha256"],
                    "text_sha256": sample["text_sha256"],
                    "human_label": sample["human_label"],
                    "provider": str(sample["synthesis"].get("provider") or "unknown"),
                    "expected_text": sample["text"],
                    "observed_text": observed,
                    "comparison": compare_speech_transcript(sample["text"], observed),
                }
            )
        if progress is not None:
            progress.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                progress,
                _progress_document(corpus.corpus_id, model_sha256, device, records),
                sort_keys=True,
            )
    try:
        if sha256_control_path(model) != model_sha256:
            raise SpeechRobustnessAsrError("ASR model changed during evaluation")
        if (
            load_speech_robustness_corpus(corpus.directory).corpus_id
            != corpus.corpus_id
        ):
            raise SpeechRobustnessAsrError(
                "Robustness corpus changed during evaluation"
            )
    except (BulkGenerationError, SpeechRobustnessCorpusError) as error:
        raise SpeechRobustnessAsrError(str(error)) from error
    body = {
        "schema": SPEECH_ROBUSTNESS_ASR_SCHEMA,
        "schema_version": SPEECH_ROBUSTNESS_ASR_VERSION,
        "corpus_id": corpus.corpus_id,
        "corpus_schema_version": corpus.document["schema_version"],
        "asr": {
            "model_directory": model.name,
            "model_sha256": model_sha256,
            "device": device,
            "decoding": "deterministic_greedy_default",
        },
        "policy": {"diagnostic_only": True, "automatic_rejection": False},
        "records": records,
        "summary": _summary(records),
    }
    document = {**body, "report_id": canonical_document_sha256(body)}
    _validate_document(document)
    return SpeechRobustnessAsrReport(document["report_id"], document, corpus.directory)


def write_speech_robustness_asr_report(report, output_path):
    """Publish one immutable ASR report without replacing existing evidence."""
    if not isinstance(report, SpeechRobustnessAsrReport):
        raise SpeechRobustnessAsrError(
            "ASR report publication requires validated corpus authority"
        )
    output = Path(output_path).expanduser().resolve()
    try:
        output.relative_to(report.corpus_directory)
    except ValueError:
        pass
    else:
        raise SpeechRobustnessAsrError(
            "ASR report must be outside the immutable corpus directory"
        )
    document = report.document
    _validate_document(document)
    try:
        return write_json_document_no_replace(
            output, document, "speech robustness ASR report"
        )
    except AuthoringAuthorityError as error:
        raise SpeechRobustnessAsrError(str(error)) from error


__all__ = [
    "SPEECH_ROBUSTNESS_ASR_SCHEMA",
    "SPEECH_ROBUSTNESS_ASR_VERSION",
    "SpeechRobustnessAsrError",
    "SpeechRobustnessAsrReport",
    "build_speech_robustness_asr_report",
    "compare_speech_transcript",
    "write_speech_robustness_asr_report",
]
