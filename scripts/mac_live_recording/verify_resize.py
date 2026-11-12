"""Verify the short native resize fixture, saving decoded frames and pixel evidence."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

if __package__:
    from .verify import validate_events
else:
    from verify import validate_events


def validate_resize_events(
    metadata: dict, records: list[dict], fixture: list[dict]
) -> tuple[int, int]:
    validate_events(metadata, records, minimum_seconds=30)
    if metadata.get("target_guard_failure") or metadata.get("video_drops", 0):
        raise ValueError("Target identity failed or video frames were dropped")
    if metadata.get("video_resize_policy") != "aspect-fit-black":
        raise ValueError("Missing aspect-fit-black resize policy")
    extent = metadata.get("video_size")
    if (
        not isinstance(extent, list)
        or len(extent) != 2
        or any(type(value) is not int or value <= 0 for value in extent)
    ):
        raise ValueError("Invalid fixed video extent")
    frames = [r for r in records if r.get("kind") == "video" and r.get("written")]
    if any(
        [r.get("width"), r.get("height")] != extent
        or not math.isfinite(r["pts_s"])
        or r.get("dropped_since_previous", 0)
        for r in frames
    ) or any(r.get("kind") == "invalid_sample" for r in records):
        raise ValueError("Invalid video sample or changing encoded extent")
    if not fixture or any(r.get("window_id") != metadata["window_id"] for r in fixture):
        raise ValueError("Fixture selected-window identity changed or is missing")
    sizes = {(r["width"], r["height"]) for r in fixture}
    initial = metadata["bounds"]
    if not (
        min(width for width, _ in sizes) < initial["width"]
        and max(width for width, _ in sizes) > initial["width"]
        and any(width / height < 0.8 for width, height in sizes)
        and any(width / height > 2 for width, height in sizes)
        and any(r.get("fullscreen") is True for r in fixture)
        and any(r["second"] >= 29 and r.get("fullscreen") is False for r in fixture)
    ):
        raise ValueError(
            "Fixture did not demonstrate shrink, grow, aspect change and fullscreen/restore"
        )
    return tuple(extent)


def inspect_frame(rgb: np.ndarray, *, bars: str | None = None) -> dict:
    """Require the complete fixture border and reject the fixture's forbidden desktop color."""
    if rgb.ndim != 3 or rgb.shape[2] != 3 or min(rgb.shape[:2]) < 8:
        raise ValueError("Invalid decoded RGB frame")
    red, green, blue = (rgb[:, :, channel] for channel in range(3))
    magenta = (red > 150) & (blue > 150) & (green < 80)
    if magenta.any():
        raise ValueError(
            "Forbidden magenta desktop/occluder pixels leaked into selected-window capture"
        )
    yellow = (red > 160) & (green > 150) & (blue < 100)
    ys, xs = np.nonzero(yellow)
    if not len(xs):
        raise ValueError("Fixture yellow border is missing")
    left, right, top, bottom = (
        int(xs.min()),
        int(xs.max()),
        int(ys.min()),
        int(ys.max()),
    )
    border = yellow[top : bottom + 1, left : right + 1]
    thickness = max(2, round(min(border.shape) * 0.02))
    edges = (
        border[:thickness].any(axis=0),
        border[-thickness:].any(axis=0),
        border[:, :thickness].any(axis=1),
        border[:, -thickness:].any(axis=1),
    )
    if min(float(edge.mean()) for edge in edges) < 0.8:
        raise ValueError("Fixture is cropped or a yellow border edge is missing")
    fixture_green = (green > 180) & (red < 160) & (blue < 160)
    if fixture_green[top : bottom + 1, left : right + 1].mean() < 0.4:
        raise ValueError("Expected green fixture interior is missing")
    black = (rgb < 16).all(axis=2)
    if bars == "portrait" and not (black[:, 0].all() and black[:, -1].all()):
        raise ValueError("Portrait frame is missing black side bars")
    if bars == "wide" and not (black[0].all() and black[-1].all()):
        raise ValueError("Wide frame is missing black top/bottom bars")
    aspect = (right - left + 1) / (bottom - top + 1)
    if (bars == "portrait" and aspect >= 0.8) or (bars == "wide" and aspect <= 2):
        raise ValueError("Decoded fixture did not change to the expected aspect ratio")
    return {
        "yellow_bounds": [left, top, right, bottom],
        "green_pixels": int(fixture_green.sum()),
        "magenta_pixels": 0,
        "black_bar_check": bars,
    }


def verify(root: Path) -> dict:
    metadata = json.loads((root / "session.json").read_text())
    records = [
        json.loads(line) for line in (root / "game.jsonl").read_text().splitlines()
    ]
    fixture = [
        json.loads(line) for line in (root / "fixture.jsonl").read_text().splitlines()
    ]
    extent = validate_resize_events(metadata, records, fixture)
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
                "-show_frames",
                "-show_entries",
                "stream=width,height,duration:frame=width,height",
                "-of",
                "json",
                str(movie),
            ],
            text=True,
            timeout=60,
        )
    )
    accepted = [r for r in records if r.get("kind") == "video" and r.get("written")]
    decoded = probe.get("frames", [])
    if len(decoded) != len(accepted) or any(
        (r["width"], r["height"]) != extent for r in decoded
    ):
        raise ValueError(
            "Decoded frame count or extent differs from accepted writer samples"
        )
    duration = float(probe["streams"][0]["duration"])
    if not math.isfinite(duration) or duration < 31:
        raise ValueError("Decoded video ended before final verification frame")
    samples = []
    for second in (2, 5, 9, 13, 18, 22, 27, 31):
        screenshot = root / f"verified-resize-{second:02}.png"
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-ss",
                str(second),
                "-i",
                str(movie),
                "-frames:v",
                "1",
                "-y",
                str(screenshot),
            ],
            check=True,
            timeout=20,
        )
        with Image.open(screenshot) as image:
            rgb = np.asarray(image.convert("RGB"))
        if (rgb.shape[1], rgb.shape[0]) != extent:
            raise ValueError(
                "Decoded screenshot extent differs from fixed video extent"
            )
        bars = "portrait" if second == 13 else "wide" if second == 18 else None
        samples.append(
            {
                "second": second,
                "screenshot": screenshot.name,
                **inspect_frame(rgb, bars=bars),
            }
        )
    evidence = {
        "status": "verified",
        "video_size": list(extent),
        "decoded_frames": len(decoded),
        "timeline_span_s": accepted[-1]["pts_s"] - accepted[0]["pts_s"],
        "samples": samples,
    }
    (root / "resize-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.output)))


if __name__ == "__main__":
    main()
