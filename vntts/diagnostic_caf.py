"""Validate and finalize a copy of interrupted native LPCM CAF recordings."""

import math
import os
import shutil
import struct
from pathlib import Path
from threading import Event
from time import monotonic

import soundfile as sf

from vntts.path_safety import open_regular_binary

MAX_CAPTURE_SECONDS = (
    3600 + 20
)  # Final callbacks may drain after the acquisition deadline.
MAX_CAF_BYTES = 192000 * 2 * 4 * MAX_CAPTURE_SECONDS + 1024**2


def _cancel(cancelled: Event | None, deadline: float | None = None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("CAF recovery cancelled")
    if deadline is not None and monotonic() > deadline:
        raise TimeoutError("CAF recovery reached the analysis budget")


def recover_caf_copy(
    source: Path,
    destination: Path,
    *,
    cancelled: Event | None = None,
    deadline: float | None = None,
) -> bool:
    """Return False for finalized CAF; otherwise verify/create a finalized copy.

    Existing copies must exactly match the patched source. The original is never changed.
    """
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("Diagnostic CAF must not be a symlink")
    _cancel(cancelled, deadline)
    with open_regular_binary(source) as original:
        identity = os.fstat(original.fileno())
        if source.lstat().st_ino != identity.st_ino or source.is_symlink():
            raise ValueError("CAF source changed while opening")
        size = identity.st_size
        if (
            not 8 <= size <= MAX_CAF_BYTES
            or original.read(8) != b"caff\x00\x01\x00\x00"
        ):
            raise ValueError("Invalid or oversized diagnostic CAF")
        description = None
        patch_at = None
        for _ in range(1024):
            _cancel(cancelled, deadline)
            chunk_at = original.tell()
            if chunk_at > 1024**2:
                raise ValueError("CAF header exceeds its recovery budget")
            header = original.read(12)
            if len(header) != 12:
                raise ValueError("Incomplete CAF chunk header")
            kind, count = struct.unpack(">4sq", header)
            payload = original.tell()
            if count < 0 and (kind != b"data" or count != -1):
                raise ValueError("Invalid CAF chunk size")
            if count >= 0 and payload + count > size:
                raise ValueError("Incomplete CAF chunk")
            if kind == b"desc":
                if description is not None or count != 32:
                    raise ValueError("Invalid CAF description")
                description = struct.unpack(">d4sIIIII", original.read(32))
            if kind == b"data":
                if description is None:
                    raise ValueError("CAF audio precedes its description")
                rate, codec, flags, packet_bytes, packet_frames, channels, bits = (
                    description
                )
                if (
                    not math.isfinite(rate)
                    or not 8000 <= rate <= 192000
                    or rate != int(rate)
                    or codec != b"lpcm"
                    or flags & ~3
                    or channels not in (1, 2)
                    or bits not in (8, 16, 24, 32)
                    or (flags & 1 and bits != 32)
                    or packet_frames != 1
                    or packet_bytes != channels * (bits // 8)
                ):
                    raise ValueError("Unsupported diagnostic CAF LPCM description")
                data_bytes = (size - payload if count == -1 else count) - 4
                if (
                    data_bytes <= 0
                    or data_bytes % packet_bytes
                    or data_bytes > MAX_CAPTURE_SECONDS * rate * packet_bytes
                ):
                    raise ValueError("Invalid or oversized CAF PCM frames")
                if count != -1:
                    return False
                patch_at, patched_size = chunk_at + 4, size - payload
                frames = data_bytes // packet_bytes
                break
            original.seek(payload + count)
        if patch_at is None:
            raise ValueError("Missing final CAF audio chunk")
        original.seek(0)
        created = False
        try:
            try:
                if (
                    not destination.exists()
                    and shutil.disk_usage(destination.parent).free
                    < size + 512 * 1024**2
                ):
                    raise OSError(
                        "Insufficient free space for CAF recovery and reserve"
                    )
                output = destination.open("xb")
                created = True
            except FileExistsError:
                output = None
            with open_regular_binary(destination) if output is None else output as copy:
                if (
                    destination.is_symlink()
                    or destination.lstat().st_ino != os.fstat(copy.fileno()).st_ino
                ):
                    raise ValueError("CAF destination changed while opening")
                position = 0
                while block := original.read(1024**2):
                    _cancel(cancelled, deadline)
                    begin, end = (
                        max(position, patch_at),
                        min(position + len(block), patch_at + 8),
                    )
                    if begin < end:
                        patch = struct.pack(">q", patched_size)
                        block = (
                            block[: begin - position]
                            + patch[begin - patch_at : end - patch_at]
                            + block[end - position :]
                        )
                    if created:
                        copy.write(block)
                    elif copy.read(len(block)) != block:
                        raise ValueError("Existing CAF recovery differs from original")
                    position += len(block)
                if not created and copy.read(1):
                    raise ValueError("Existing CAF recovery has trailing data")
                after = os.fstat(original.fileno())
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                    size,
                    identity.st_mtime_ns,
                    identity.st_ctime_ns,
                ):
                    raise ValueError("CAF changed during recovery")
            with (
                open_regular_binary(destination) as recovered,
                sf.SoundFile(recovered) as audio,
            ):
                if (
                    audio.frames != frames
                    or audio.samplerate != rate
                    or audio.channels != channels
                ):
                    raise ValueError("Recovered CAF decoder disagrees with LPCM frames")
            return True
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise


def diagnostic_audio_path(
    path: Path, *, cancelled: Event | None = None, deadline: float | None = None
) -> Path:
    """Resolve an interrupted CAF to its verified recovery copy, preserving its original."""
    if path.is_symlink():
        raise ValueError("Diagnostic audio must not be a symlink")
    try:
        _cancel(cancelled, deadline)
        with open_regular_binary(path) as source, sf.SoundFile(source) as audio:
            if (
                not 8000 <= audio.samplerate <= 192000
                or audio.channels not in (1, 2)
                or audio.frames > MAX_CAPTURE_SECONDS * audio.samplerate
                or os.fstat(source.fileno()).st_size > MAX_CAF_BYTES
            ):
                raise ValueError("Unsupported or oversized diagnostic CAF")
            return path
    except sf.LibsndfileError:
        recovered = path.with_name(path.stem + ".recovered.caf")
        if recover_caf_copy(path, recovered, cancelled=cancelled, deadline=deadline):
            return recovered
        raise
