"""Swinging-door compression, the mechanism that makes a historian a historian.

A historian stores only readings a straight line cannot reconstruct within a stated
tolerance. From an anchor point, track the range of slopes that keep every subsequent
reading within +/-E; when that range closes, archive the previous point.

Two departures from the textbook: a gap closes the door, and so does a quality change.
Neither may be spanned by a line, because the reconstruction would invent a measurement.

The bound is verified rather than asserted. See docs/HISTORIAN_CONCEPTS.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

#: Compression deviation as a fraction of each sensor's observed span.
#:
#: Expressed as a percentage of span because that is how the setting is configured in
#: practice - an engineer sets "compression deviation = 0.1% of range", not an absolute
#: number per tag. 0.1% is a common starting point for a well-behaved analogue signal.
DEFAULT_DEVIATION_PCT = 0.1

#: Deviations to evaluate when characterising the trade-off curve.
SWEEP_DEVIATION_PCT = (0.02, 0.05, 0.1, 0.25, 0.5, 1.0)

#: Seconds after which a step counts as a gap and the door is forced shut.
#: Matches the pipeline's measured gap threshold: 3x the 10 s modal interval.
GAP_SECONDS = 30.0


@dataclass
class CompressionResult:
    """What compressing one series produced."""

    kept_indices: np.ndarray
    original_count: int
    deviation: float

    @property
    def kept_count(self) -> int:
        return int(self.kept_indices.size)

    @property
    def ratio(self) -> float:
        """Original samples per retained sample. 20.0 means a 20x reduction."""
        return self.original_count / self.kept_count if self.kept_count else 0.0

    @property
    def retained_pct(self) -> float:
        return 100.0 * self.kept_count / self.original_count if self.original_count else 0.0


def swinging_door(
    seconds: np.ndarray,
    values: np.ndarray,
    deviation: float,
    *,
    quality: np.ndarray | None = None,
    gap_seconds: float = GAP_SECONDS,
) -> CompressionResult:
    """Compress one series, returning the indices worth keeping.

    ``deviation`` is the half-width of the error corridor in the sensor's own units, and
    every discarded point is guaranteed within it. A quality change or a step longer than
    ``gap_seconds`` closes the door, so no line spans held data or a gap. First and last
    points are always retained.
    """
    if seconds.size != values.size:
        # Fail at the boundary, not three loops deep.
        raise ValueError(
            f"seconds and values must be the same length: {seconds.size} vs {values.size}"
        )
    if quality is not None and quality.size != seconds.size:
        raise ValueError(
            f"quality must match seconds length: {quality.size} vs {seconds.size}"
        )

    count = seconds.size
    if count == 0:
        return CompressionResult(np.array([], dtype=np.int64), 0, deviation)
    if count <= 2 or deviation <= 0:
        return CompressionResult(np.arange(count, dtype=np.int64), count, deviation)

    kept = [0]
    anchor = 0
    # The feasible slope corridor from the current anchor. Starts unbounded.
    slope_low = -np.inf
    slope_high = np.inf

    for i in range(1, count):
        elapsed = seconds[i] - seconds[anchor]

        # Two named conditions rather than one combined expression, which would rely on
        # `and` binding tighter than `or` and get misread in a later edit.
        crosses_gap = seconds[i] - seconds[i - 1] > gap_seconds
        changes_quality = quality is not None and quality[i] != quality[i - 1]

        if crosses_gap or changes_quality:
            # Close the previous segment, then re-anchor after the break so no line
            # spans it.
            if kept[-1] != i - 1:
                kept.append(i - 1)
            kept.append(i)
            anchor = i
            slope_low, slope_high = -np.inf, np.inf
            continue

        if elapsed <= 0:
            continue

        lower = (values[i] - deviation - values[anchor]) / elapsed
        upper = (values[i] + deviation - values[anchor]) / elapsed

        new_low = max(slope_low, lower)
        new_high = min(slope_high, upper)

        if new_low > new_high:
            # No straight line from the anchor can cover every point up to here. The
            # previous point becomes the new anchor, and this point is reconsidered
            # against it.
            anchor = i - 1
            kept.append(anchor)
            elapsed = seconds[i] - seconds[anchor]
            if elapsed > 0:
                slope_low = (values[i] - deviation - values[anchor]) / elapsed
                slope_high = (values[i] + deviation - values[anchor]) / elapsed
            else:
                slope_low, slope_high = -np.inf, np.inf
        else:
            slope_low, slope_high = new_low, new_high

    if kept[-1] != count - 1:
        kept.append(count - 1)

    indices = np.array(sorted(set(kept)), dtype=np.int64)
    indices = _enforce_error_bound(seconds, values, indices, deviation)
    return CompressionResult(indices, count, deviation)


def _enforce_error_bound(
    seconds: np.ndarray,
    values: np.ndarray,
    indices: np.ndarray,
    deviation: float,
) -> np.ndarray:
    """Split any segment whose reconstruction exceeds the deviation.

    Not in the textbook algorithm. The swinging door guarantees some line from the anchor
    stays within E; reconstruction draws the chord between the archived endpoints, which
    is a different line and can deviate up to 2E. A streaming historian cannot revisit
    that decision. Compressing in batch can. See DEV_LOG DL-043.
    """
    if indices.size < 2 or deviation <= 0:
        return indices

    kept = {int(i) for i in indices}
    # Each entry is a segment [start, end] still to verify.
    stack: list[tuple[int, int]] = [
        (int(indices[i]), int(indices[i + 1])) for i in range(indices.size - 1)
    ]

    # A guard against pathological input: worst case every point is retained, at which
    # point there is nothing left to split.
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue

        span = seconds[end] - seconds[start]
        if span <= 0:
            continue

        inner = np.arange(start + 1, end)
        slope = (values[end] - values[start]) / span
        chord = values[start] + slope * (seconds[inner] - seconds[start])
        errors = np.abs(values[inner] - chord)

        worst = int(errors.argmax())
        if errors[worst] <= deviation:
            continue

        split = int(inner[worst])
        kept.add(split)
        stack.append((start, split))
        stack.append((split, end))

    return np.array(sorted(kept), dtype=np.int64)


def reconstruct(
    seconds: np.ndarray,
    kept_seconds: np.ndarray,
    kept_values: np.ndarray,
) -> np.ndarray:
    """Rebuild the full series from retained points by linear interpolation.

    This is what a historian does when asked for a value it did not store: it draws the
    line between the two archived points that bracket the request.
    """
    return np.interp(seconds, kept_seconds, kept_values)


def reconstruction_error(
    seconds: np.ndarray,
    values: np.ndarray,
    result: CompressionResult,
) -> dict[str, float]:
    """Verify the error bound rather than trusting it.

    The whole promise of compression is "no discarded reading is more than E away from
    what you get back". That is checkable, so it is checked: every run reports the worst
    deviation, and a violation means the implementation is wrong, not that the data was
    unusual.
    """
    if result.kept_count == 0:
        return {"max_error": 0.0, "mean_error": 0.0, "rmse": 0.0, "within_bound": True}

    rebuilt = reconstruct(seconds, seconds[result.kept_indices], values[result.kept_indices])
    errors = np.abs(values - rebuilt)

    max_error = float(errors.max())
    # A small tolerance for floating-point accumulation in the slope arithmetic; the
    # bound is a guarantee about the algorithm, not about IEEE 754.
    tolerance = max(result.deviation * 1e-6, 1e-9)

    return {
        "max_error": max_error,
        "mean_error": float(errors.mean()),
        "rmse": float(np.sqrt((errors ** 2).mean())),
        "within_bound": bool(max_error <= result.deviation + tolerance),
    }


def deviation_for_span(minimum: float, maximum: float, percent: float) -> float:
    """Convert a percent-of-span setting into an absolute deviation.

    Percent of span is the unit an engineer actually configures. A tag reading 0-10 bar
    with a 0.1% setting gets a 0.01 bar corridor.
    """
    span = abs(maximum - minimum)
    if span <= 0:
        return 0.0
    return span * percent / 100.0
