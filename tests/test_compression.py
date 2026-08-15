"""Swinging-door compression tests.

The property under test is a *guarantee*, not a behaviour: every discarded reading must be
within the stated deviation of the reconstructed line. That is checkable exhaustively, so
it is checked exhaustively - including by fuzzing, because a hand-picked set of shapes
will not find the case where the algorithm is subtly wrong.
"""

from __future__ import annotations

import numpy as np
import pytest

from analytics.compression import (
    deviation_for_span,
    reconstruct,
    reconstruction_error,
    swinging_door,
)


def compress(times, values, deviation, **kwargs):
    return swinging_door(np.asarray(times, dtype=float),
                         np.asarray(values, dtype=float), deviation, **kwargs)


# ---------------------------------------------------------------------------------
# Cases with a hand-known answer
# ---------------------------------------------------------------------------------

def test_a_straight_line_needs_only_its_endpoints():
    """100 collinear points carry two points of information."""
    result = compress(range(100), [i * 0.5 for i in range(100)], 0.01)
    assert result.kept_count == 2
    assert result.kept_indices.tolist() == [0, 99]


def test_a_constant_signal_needs_only_its_endpoints():
    result = compress(range(100), [7.0] * 100, 0.01)
    assert result.kept_count == 2


def test_a_step_keeps_the_points_either_side():
    """The transition is the information. Everything flat around it is not."""
    result = compress(range(10), [0.0] * 5 + [10.0] * 5, 0.01)
    assert 4 in result.kept_indices, "the last point before the step"
    assert 5 in result.kept_indices, "the first point after it"
    assert result.kept_count < 10


def test_noise_below_the_deadband_is_absorbed():
    rng = np.random.default_rng(0)
    times = np.arange(500.0)
    values = times * 0.1 + rng.uniform(-0.04, 0.04, 500)
    result = compress(times, values, 0.05)
    assert result.ratio > 2.0


def test_noise_above_the_deadband_is_not_compressible():
    """Compression is not magic. When every sample genuinely differs by more than the
    tolerance, almost every sample has to be kept - and the algorithm must not pretend
    otherwise."""
    rng = np.random.default_rng(1)
    times = np.arange(500.0)
    result = compress(times, rng.uniform(0, 10, 500), 0.05)
    assert result.retained_pct > 90


def test_a_sawtooth_is_the_worst_case():
    """Every point is a turning point, so nothing can be discarded."""
    result = compress(np.arange(500.0), np.tile([0.0, 5.0], 250), 0.05)
    assert result.kept_count == 500


# ---------------------------------------------------------------------------------
# The guarantee
# ---------------------------------------------------------------------------------

@pytest.mark.parametrize("deviation", [0.001, 0.01, 0.05, 0.2, 1.0])
def test_error_bound_holds_across_deviations(deviation):
    rng = np.random.default_rng(7)
    times = np.cumsum(rng.uniform(9, 13, 2000))
    values = np.cumsum(rng.normal(0, 0.2, 2000))

    result = compress(times, values, deviation)
    error = reconstruction_error(times, values, result)

    assert error["within_bound"], (
        f"max error {error['max_error']:.6g} exceeds deviation {deviation}"
    )


@pytest.mark.parametrize("seed", range(40))
def test_error_bound_survives_fuzzing(seed):
    """Four signal shapes across forty seeds.

    Hand-picked cases test what the author already thought of. This is what found the
    real defect: the swinging door guarantees a feasible line *exists*, but reconstruction
    uses the chord between archived endpoints, which is a different line and can deviate
    up to 2x the tolerance. See DEV_LOG DL-043.
    """
    rng = np.random.default_rng(seed)
    count = int(rng.integers(3, 400))
    times = np.cumsum(rng.uniform(5, 15, count))

    shape = seed % 4
    if shape == 0:
        values = rng.uniform(-1, 12, count)                      # pure noise
    elif shape == 1:
        values = np.cumsum(rng.normal(0, 0.3, count))            # random walk
    elif shape == 2:
        values = np.sin(times / 50) * 5 + rng.normal(0, 0.02, count)  # smooth + noise
    else:
        values = np.repeat(rng.uniform(0, 10, count // 20 + 1), 20)[:count]  # staircase

    deviation = float(rng.choice([0.001, 0.01, 0.05, 0.2, 1.0]))
    result = compress(times, values, deviation)

    assert reconstruction_error(times, values, result)["within_bound"]


def test_reconstruction_is_exact_at_retained_points():
    """A retained point must come back as itself. If it does not, the interpolation is
    reading the wrong index."""
    rng = np.random.default_rng(3)
    times = np.cumsum(rng.uniform(9, 11, 300))
    values = np.cumsum(rng.normal(0, 0.5, 300))

    result = compress(times, values, 0.1)
    rebuilt = reconstruct(times, times[result.kept_indices], values[result.kept_indices])

    assert np.allclose(rebuilt[result.kept_indices], values[result.kept_indices])


# ---------------------------------------------------------------------------------
# The two deliberate departures from textbook SDT
# ---------------------------------------------------------------------------------

def test_a_gap_forces_the_door_shut():
    """A straight line across a 4,970-second hole would invent a measurement. Every other
    part of this project refuses to interpolate across a gap, and so does this."""
    times = np.array([0.0, 10, 20, 30, 5000, 5010, 5020])
    values = np.ones(7)

    result = compress(times, values, 0.01)

    assert 3 in result.kept_indices, "the last point before the gap"
    assert 4 in result.kept_indices, "the first point after it"


def test_a_quality_change_forces_the_door_shut():
    """Held (frozen) data must not be spanned by a line drawn from a genuine reading -
    the reconstruction would look like real measurement."""
    times = np.arange(20.0)
    values = np.full(20, 5.0)
    quality = np.array([192] * 10 + [65] * 10)      # GOOD then UNCERTAIN_STALE

    result = compress(times, values, 0.01, quality=quality)

    assert 9 in result.kept_indices
    assert 10 in result.kept_indices


def test_without_a_quality_change_the_same_series_collapses():
    """The contrast that proves the previous test is measuring what it claims."""
    times = np.arange(20.0)
    values = np.full(20, 5.0)
    assert compress(times, values, 0.01).kept_count == 2


# ---------------------------------------------------------------------------------
# Edges and inputs
# ---------------------------------------------------------------------------------

def test_empty_series():
    result = compress([], [], 0.1)
    assert result.kept_count == 0
    assert result.ratio == 0.0


@pytest.mark.parametrize("count", [1, 2])
def test_series_too_short_to_compress(count):
    result = compress(range(count), [1.0] * count, 0.1)
    assert result.kept_count == count


def test_zero_deviation_keeps_everything():
    """A tolerance of zero means no reading may be approximated at all."""
    result = compress(range(50), np.arange(50.0) * 0.1, 0.0)
    assert result.kept_count == 50


def test_endpoints_are_always_retained():
    """Without the final point the reconstruction stops short of the data it represents."""
    rng = np.random.default_rng(11)
    times = np.arange(200.0)
    result = compress(times, rng.normal(5, 0.001, 200), 1.0)
    assert result.kept_indices[0] == 0
    assert result.kept_indices[-1] == 199


def test_mismatched_input_lengths_are_rejected_at_the_boundary():
    """An IndexError three loops deep is a poor way to learn two arrays came from
    different queries."""
    with pytest.raises(ValueError, match="same length"):
        swinging_door(np.arange(5.0), np.arange(3.0), 0.1)


def test_mismatched_quality_length_is_rejected():
    with pytest.raises(ValueError, match="quality must match"):
        swinging_door(np.arange(5.0), np.arange(5.0), 0.1, quality=np.arange(3))


# ---------------------------------------------------------------------------------
# Percent-of-span configuration
# ---------------------------------------------------------------------------------

def test_deviation_from_span():
    """Percent of span is the unit an engineer actually configures - 'compression
    deviation = 0.1% of range', not an absolute number per tag."""
    assert deviation_for_span(0.0, 10.0, 0.1) == pytest.approx(0.01)
    assert deviation_for_span(-1.0, 9.0, 1.0) == pytest.approx(0.1)


def test_zero_span_yields_zero_deviation():
    """A tag that never moved has no span to take a percentage of. Returning zero keeps
    every reading, which is the safe answer."""
    assert deviation_for_span(5.0, 5.0, 0.1) == 0.0


def test_measured_archive_ratios_are_reproduced():
    """Regression guard on the headline numbers.

    Measured over the real archive at 0.1% of span: DV_PRESSURE compresses 24.6x because
    it sits flat near -0.02 bar for most of its life, while OIL_TEMPERATURE manages only
    2.0x because its noise is comparable to the deadband. The ordering is a property of
    the signals, and a change in it means something changed in the algorithm.
    """
    rng = np.random.default_rng(21)
    times = np.arange(20_000.0) * 10

    # A mostly-flat signal with occasional excursions, like DV_PRESSURE.
    flat = np.full(20_000, -0.02) + rng.normal(0, 0.0005, 20_000)
    flat[5000:5100] = 2.4

    # A continuously noisy signal, like OIL_TEMPERATURE.
    noisy = 60 + np.cumsum(rng.normal(0, 0.05, 20_000)) * 0.01 + rng.normal(0, 0.05, 20_000)

    flat_result = compress(times, flat, deviation_for_span(-0.03, 9.8, 0.1))
    noisy_result = compress(times, noisy, deviation_for_span(15.4, 89.0, 0.1))

    assert flat_result.ratio > noisy_result.ratio, (
        "a flat signal must compress better than a noisy one"
    )
    assert reconstruction_error(times, flat, flat_result)["within_bound"]
    assert reconstruction_error(times, noisy, noisy_result)["within_bound"]
