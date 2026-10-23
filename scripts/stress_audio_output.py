"""Stress Rust prepared playback with deliberate GIL holds on an approved WAV."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Any

import numpy as np
import soundfile as sf

from vntts.audio_output import (
    match_output_sample_rate,
    prepared_output_stream,
    resolve_audio_output,
    write_pcm_chunks,
)
from vntts.rust_audio import portaudio_library
from vntts.support import configure_audio_lifecycle_log


def run_trial(
    samples: np.ndarray,
    sample_rate: int,
    gil_hold_ms: int,
) -> dict[str, Any]:
    log = configure_audio_lifecycle_log()
    output = resolve_audio_output(None)
    samples, rate = match_output_sample_rate(output, samples, sample_rate)
    samples = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1, 1)
    stop = Event()
    playing = Event()
    holds: list[float] = []
    first_pcm: float | None = None
    started = monotonic()
    old_interval = sys.getswitchinterval()

    def stress() -> None:
        if not playing.wait(10):
            return
        while not stop.wait(0.1):
            began = monotonic()
            # Deliberate diagnostic load, distinct from actual OCR/model generation.
            while monotonic() - began < gil_hold_ms / 1000 and not stop.is_set():
                pass
            holds.append((monotonic() - began) * 1000)

    def first_write() -> None:
        nonlocal first_pcm
        first_pcm = monotonic()
        playing.set()

    thread = None
    try:
        if gil_hold_ms:
            sys.setswitchinterval(gil_hold_ms / 1000 + 0.05)
            thread = Thread(
                target=stress, name="vntts-diagnostic-gil-load", daemon=True
            )
            thread.start()
        with prepared_output_stream(
            output, samplerate=rate, channels=1, dtype="float32", latency="low"
        ) as stream:
            completed, underflowed = write_pcm_chunks(
                stream,
                samples,
                rate,
                stop.is_set,
                first_write=first_write,
                stop_requested=stop,
            )
        ended = monotonic()
    finally:
        stop.set()
        playing.set()
        if thread is not None:
            thread.join(2)
        sys.setswitchinterval(old_interval)
    events = log.snapshot()
    summaries = [
        event
        for event in events
        if event.get("operation")
        in {"pcm-write-summary", "system-write-summary", "rust-pcm-summary"}
    ]
    return {
        "backend": "rust",
        "gil_hold_ms": gil_hold_ms,
        "completed": completed,
        "underflowed": underflowed,
        "sample_rate": rate,
        "channels": 1,
        "frames": len(samples),
        "pcm_sha256": hashlib.sha256(samples.astype("<f4").tobytes()).hexdigest(),
        "expected_ms": len(samples) * 1000 / rate,
        "wall_ms": (ended - started) * 1000,
        "first_callback_receipt_or_write_return_ms": None
        if first_pcm is None
        else (first_pcm - started) * 1000,
        "gil_hold_count": len(holds),
        "max_gil_hold_ms": max(holds, default=0),
        "summaries": summaries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--gil-hold-ms", type=int, default=200)
    options = parser.parse_args(argv)
    if not 1 <= options.repeats <= 5 or not 0 <= options.gil_hold_ms <= 500:
        parser.error("repeats must be 1..5; gil-hold-ms must be 0..500")
    if options.output.exists():
        parser.error("output already exists; choose a new receipt path")
    source_hash = hashlib.sha256(options.wav.read_bytes()).hexdigest()
    samples, sample_rate = sf.read(options.wav, dtype="float32", always_2d=True)
    if samples.shape[1] != 1 or not len(samples) or not np.isfinite(samples).all():
        parser.error("stress playback requires a nonempty finite mono WAV")
    library = portaudio_library()
    library_path = Path(library)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "source_sha256": source_hash,
        "portaudio_library": library,
        "portaudio_sha256": hashlib.sha256(library_path.read_bytes()).hexdigest()
        if library_path.is_file()
        else None,
        "load": "deliberate Python GIL hold; no real OCR/generation load claimed",
        "onset_scope": "Python observed callback event or blocking write return, not measured DAC onset",
        "trials": [],
    }
    try:
        for load in dict.fromkeys((0, options.gil_hold_ms)):
            for repeat in range(options.repeats):
                trial = run_trial(samples, sample_rate, load)
                trial["repeat"] = repeat
                receipt["trials"].append(trial)
                options.output.write_text(json.dumps(receipt, indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            k: trial[k]
                            for k in (
                                "backend",
                                "gil_hold_ms",
                                "completed",
                                "underflowed",
                                "wall_ms",
                            )
                        }
                    ),
                    flush=True,
                )
        receipt["source_unchanged"] = (
            hashlib.sha256(options.wav.read_bytes()).hexdigest() == source_hash
        )
        receipts = receipt["trials"]
        receipt["same_pcm_format"] = (
            len(
                {
                    (r["pcm_sha256"], r["sample_rate"], r["channels"], r["frames"])
                    for r in receipts
                }
            )
            == 1
        )
        receipt["all_completed"] = all(r["completed"] for r in receipts)
        return (
            0
            if all(
                receipt[k]
                for k in ("source_unchanged", "same_pcm_format", "all_completed")
            )
            else 1
        )
    finally:
        options.output.write_text(json.dumps(receipt, indent=2) + "\n")
        from vntts.system_audio import get_system_audio_output

        get_system_audio_output().module._terminate()


if __name__ == "__main__":
    raise SystemExit(main())
