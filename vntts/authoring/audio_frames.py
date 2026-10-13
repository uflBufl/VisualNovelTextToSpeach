"""Shared frame measurements for admitted mono floating-point PCM."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def frame_rms[Float: (np.float32, np.float64)](
    samples: NDArray[Float], frame_samples: int
) -> NDArray[Float]:
    """Keep input precision and measure a final partial frame without padding."""
    full_frames, tail_size = divmod(samples.size, frame_samples)
    full_end = full_frames * frame_samples
    # Row-wise means retain per-frame rounding; reduceat sums can differ.
    rms = np.sqrt(
        np.mean((samples[:full_end] ** 2).reshape(full_frames, frame_samples), axis=1)
    )
    if tail_size:
        rms = np.append(rms, np.sqrt(np.mean(samples[full_end:] ** 2)))
    return rms
