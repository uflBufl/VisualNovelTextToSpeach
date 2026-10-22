"""Validate native recorder evidence and extract real decoded beginning/middle/end frames."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path


def _validate_capture_metadata(metadata: dict, minimum_seconds: float) -> None:
    if metadata.get("status") != "succeeded" or metadata.get("errors"):
        raise ValueError("Capture failed or did not finish")
    if (
        not metadata.get("window_id")
        or not metadata.get("pid")
        or not metadata.get("bounds")
    ):
        raise ValueError("Explicit window provenance missing")
    if metadata.get("mechanism") != "ScreenCaptureKit desktopIndependentWindow":
        raise ValueError("Unexpected capture mechanism")
    if metadata.get("requested_seconds", 0) <= minimum_seconds:
        raise ValueError("Acceptance requires a recording longer than 60 seconds")
    if metadata.get("stop_requested"):
        raise ValueError("Acceptance capture stopped early")


def validate_events(
    metadata: dict, records: list[dict], minimum_seconds: float = 60
) -> list[float]:
    """Return three movie times; incomplete/frozen capture never passes on frame count alone."""
    _validate_capture_metadata(metadata, minimum_seconds)
    if any(r.get("kind") in {"failed", "error"} for r in records):
        raise ValueError("Stream reported failure")
    finished = [r for r in records if r.get("kind") == "finished"]
    if not finished or finished[-1].get("video_drops") != 0:
        raise ValueError("Missing finalization or encoder dropped frames")
    frames = [r for r in records if r.get("kind") == "video" and r.get("written")]
    if len(frames) < 3:
        raise ValueError("Insufficient actual video frames")
    pts = [r["pts_s"] for r in frames]
    if any(a >= b for a, b in pairwise(pts)):
        raise ValueError("Video PTS is not strictly increasing")
    span = pts[-1] - pts[0]
    if span < minimum_seconds or span < metadata["requested_seconds"] - 5:
        raise ValueError("Video ended early, including the four-frame stall")
    # A changing fixture produces one complete frame per second. Long gaps invalidate it.
    if any(b - a > 3 for a, b in pairwise(pts)):
        raise ValueError("Changing-window frames have a gap longer than three seconds")
    return [2, span / 2, span - 2]


def verify(root: Path) -> None:
    metadata = json.loads((root / "session.json").read_text())
    records = [
        json.loads(line) for line in (root / "game.jsonl").read_text().splitlines()
    ]
    times = validate_events(metadata, records)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("ffmpeg and ffprobe are required for real decode verification")
    movie = root / "window.mov"
    probe = json.loads(
        subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=nb_read_frames,duration",
                "-of",
                "json",
                str(movie),
            ],
            text=True,
        )
    )
    stream = probe["streams"][0]
    logged = sum(r.get("kind") == "video" and bool(r.get("written")) for r in records)
    if int(stream["nb_read_frames"]) != logged:
        raise ValueError("Decoded frame count differs from accepted writer samples")
    if float(stream["duration"]) < times[-1]:
        raise ValueError("Decoded video ended early")
    for index, (label, second) in enumerate(
        zip(("begin", "middle", "end"), times, strict=True)
    ):
        command = [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            str(second),
            "-i",
            str(movie),
            "-frames:v",
            "1",
        ]
        rgb = subprocess.check_output(
            command + ["-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        )
        if len(rgb) != 3 or max(range(3), key=rgb.__getitem__) != index:
            raise ValueError(
                f"Fixture {label} scene did not change to the expected RGB phase"
            )
        subprocess.run(
            command + ["-y", str(root / f"verified-{label}.png")], check=True
        )
    print(
        f"Verified >60s of real changing-window video: {logged} decoded frames; inspect verified-begin/middle/end.png text"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    verify(args.output)


if __name__ == "__main__":
    main()
