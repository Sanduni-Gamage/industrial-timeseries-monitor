"""Analytics tests.

Everything here is a pure function tested without a database. The emphasis is on the
decisions that would be quietly wrong rather than loudly broken: a state boundary off by
one, a persistence rule that lets chatter through, a z-score that divides by zero and
returns infinity instead of admitting it does not know.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analytics import forecasting as fc
from analytics import operating_state as ost
from analytics.anomaly_detection import _apply_persistence
from analytics.failure_analysis import WindowStat
from analytics.statistics import BaselineRow

# ---------------------------------------------------------------------------------
# Operating state
# ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "current, expected",
    [
        (0.0, ost.STATE_OFF),
        (0.04, ost.STATE_OFF),          # the measured OFF median
        (0.999, ost.STATE_OFF),
        (1.0, ost.STATE_OFFLOADED),     # boundary is inclusive at the floor
        (4.0, ost.STATE_OFFLOADED),     # documented nominal for OFFLOADED
        (4.999, ost.STATE_OFFLOADED),
        (5.0, ost.STATE_LOADED),
        (7.0, ost.STATE_LOADED),        # documented nominal for LOADED
        (7.999, ost.STATE_LOADED),
        (8.0, ost.STATE_STARTING),
        (9.0, ost.STATE_STARTING),      # documented nominal for STARTING
        (30.0, ost.STATE_STARTING),
    ],
)
def test_state_boundaries(current, expected):
    """The four documented nominal currents must land in their own states.

    If a boundary drifts, every baseline and every anomaly downstream shifts with it, so
    this pins the documented values themselves rather than only the edges.
    """
    assert ost.classify(current) == expected


def test_python_and_sql_classifiers_use_the_same_boundaries():
    """The SQL CASE expression and the Python function are two copies of one rule.

    They cannot be executed together here, so the test asserts that the numbers embedded
    in the SQL are the same constants the Python branch uses. A silent divergence would
    make ScanState and any ad-hoc query disagree about what the machine was doing.
    """
    expression = ost.classify_expression("MotorCurrent")
    assert f"< {ost.OFFLOADED_FLOOR}" in expression
    assert f"< {ost.LOADED_FLOOR}" in expression
    assert f"< {ost.STARTING_FLOOR}" in expression
    for state in ost.ALL_STATES:
        assert f"'{state}'" in expression


def test_starting_is_excluded_from_baselines():
    """Only 44 scans across seven months (0.003%) fall in STARTING. That cannot support
    a standard deviation, let alone a control limit."""
    assert ost.STATE_STARTING not in ost.BASELINE_STATES
    assert set(ost.BASELINE_STATES) == {ost.STATE_OFF, ost.STATE_OFFLOADED, ost.STATE_LOADED}


# ---------------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------------

def make_baseline(**overrides) -> BaselineRow:
    values = {"sensor_id": 1, "sensor_code": "TP2", "operating_state": "LOADED",
              "sample_count": 10_000, "mean": 9.2, "stddev": 1.1,
              "p01": 7.0, "p25": 8.8, "p50": 9.2, "p75": 9.9, "p99": 10.5}
    values.update(overrides)
    return BaselineRow(**values)


def test_iqr_fences_use_tukeys_constant():
    b = make_baseline(p25=8.0, p75=10.0)
    assert b.iqr == pytest.approx(2.0)
    assert b.lower_fence == pytest.approx(8.0 - 1.5 * 2.0)
    assert b.upper_fence == pytest.approx(10.0 + 1.5 * 2.0)


def test_flat_signal_is_flagged_degenerate():
    """A digital tag that is always 1 in a given state has zero IQR. Its fences collapse
    onto a single value, so every reading that is not exactly that value becomes an
    outlier. Detectors must skip these rather than emit a million alerts."""
    assert make_baseline(p25=1.0, p75=1.0, stddev=0.0).is_degenerate


def test_zero_stddev_alone_is_degenerate():
    assert make_baseline(p25=1.0, p75=2.0, stddev=0.0).is_degenerate


def test_normal_baseline_is_not_degenerate():
    assert not make_baseline().is_degenerate


def test_the_measured_motor_current_off_baseline_is_reproduced():
    """Regression guard on the finding in ANALYTICS_FINDINGS.md §2: this baseline's fence
    is narrower than the instrument's own scatter, which is why per-reading IQR detection
    was rejected. If the numbers ever stop looking like this, the reasoning needs
    revisiting."""
    b = make_baseline(sensor_code="MOTOR_CURRENT", operating_state="OFF",
                      p25=0.0350, p75=0.0375, stddev=0.0088, mean=0.0373)
    fence_width = b.upper_fence - b.lower_fence
    assert fence_width == pytest.approx(0.01, abs=1e-6)
    assert fence_width < b.stddev * 1.5, "fence is tighter than the sensor's own noise"


# ---------------------------------------------------------------------------------
# Persistence - the ISA-18.2 on-delay
# ---------------------------------------------------------------------------------

def test_persistence_drops_isolated_flags():
    """A single bucket over a limit is a fluctuation, not an event. This one rule is the
    difference between 12 alarms a day and an alarm flood."""
    flags = np.array([False, True, False, True, False])
    assert not _apply_persistence(flags, 3).any()


def test_persistence_keeps_a_long_enough_run():
    flags = np.array([False, True, True, True, False])
    kept = _apply_persistence(flags, 3)
    assert kept.tolist() == [False, True, True, True, False]


def test_persistence_boundary_is_exact():
    """A run of exactly the required length survives; one shorter does not."""
    assert _apply_persistence(np.array([True, True, True]), 3).all()
    assert not _apply_persistence(np.array([True, True, False]), 3).any()


def test_persistence_handles_a_run_touching_the_end():
    """A condition still active at the end of the series must not be silently discarded
    just because it has not finished yet - that is the alarm most worth seeing."""
    flags = np.array([False, False, True, True, True])
    assert _apply_persistence(flags, 3).tolist() == [False, False, True, True, True]


def test_persistence_handles_a_run_touching_the_start():
    flags = np.array([True, True, True, False, False])
    assert _apply_persistence(flags, 3).tolist() == [True, True, True, False, False]


def test_persistence_of_one_is_a_passthrough():
    flags = np.array([True, False, True])
    assert _apply_persistence(flags, 1).tolist() == flags.tolist()


def test_persistence_on_empty_input():
    assert _apply_persistence(np.array([], dtype=bool), 3).size == 0


# ---------------------------------------------------------------------------------
# Failure-window usability gating
# ---------------------------------------------------------------------------------

def make_window(**overrides) -> WindowStat:
    values = {"source_reference": "#4", "hours": 24, "sensor_code": "TP2",
              "operating_state": "OFF", "total_scans": 1000, "usable_scans": 1000,
              "mean_value": 1.0, "baseline_mean": 0.5, "baseline_sd": 0.25}
    values.update(overrides)
    return WindowStat(**values)


def test_window_with_mostly_held_data_is_not_usable():
    """Event #1's 24-hour lead-up is 69.5% held data. Averaging it would describe a
    frozen logger and present the result next to three genuine measurements."""
    window = make_window(total_scans=6301, usable_scans=1923)
    assert window.usable_fraction == pytest.approx(0.305, abs=0.001)
    assert not window.is_usable
    assert window.z_vs_baseline is None


def test_window_with_no_usable_data_is_not_usable():
    """The 12-, 6- and 1-hour lead-ups to event #1 are 100% held."""
    window = make_window(total_scans=3581, usable_scans=0, mean_value=None)
    assert not window.is_usable
    assert window.z_vs_baseline is None


def test_fully_usable_window_yields_a_z_score():
    window = make_window(mean_value=1.0, baseline_mean=0.5, baseline_sd=0.25)
    assert window.is_usable
    assert window.z_vs_baseline == pytest.approx(2.0)


def test_zero_variance_baseline_gives_no_z_score():
    """A z-score against zero spread is not a large number, it is an undefined one.
    Returning infinity here would put a meaningless row at the top of every ranking."""
    assert make_window(baseline_sd=0.0).z_vs_baseline is None
    assert make_window(baseline_sd=None).z_vs_baseline is None


def test_usability_threshold_boundary():
    assert make_window(total_scans=100, usable_scans=50).is_usable
    assert not make_window(total_scans=100, usable_scans=49).is_usable


# ---------------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------------

def test_scoring_is_exact_on_known_errors():
    actual = np.array([10.0, 12.0, 14.0])
    predicted = np.array([11.0, 11.0, 15.0])       # errors: -1, +1, -1
    result = fc._score("t", actual, predicted)
    assert result.mae == pytest.approx(1.0)
    assert result.rmse == pytest.approx(1.0)
    assert result.n == 3


def test_mape_is_suppressed_near_zero():
    """Several signals here sit at roughly -0.012 bar. A percentage error against that is
    a number, and it is meaningless."""
    assert fc._score("t", np.array([1e-9, 1.0]), np.array([0.5, 1.0])).mape is None
    assert fc._score("t", np.array([10.0, 20.0]), np.array([9.0, 21.0])).mape is not None


def test_improvement_is_signed_correctly():
    naive = fc.ForecastResult("naive", mae=1.0, rmse=1.0, mape=None, n=10)
    better = fc.ForecastResult("ma", mae=0.8, rmse=0.9, mape=None, n=10)
    worse = fc.ForecastResult("bad", mae=1.5, rmse=1.6, mape=None, n=10)

    assert better.beats(naive)
    assert not worse.beats(naive)
    assert better.improvement_over(naive) == pytest.approx(20.0)
    assert worse.improvement_over(naive) == pytest.approx(-50.0)


def test_evaluation_is_walk_forward_not_fitted_on_the_holdout():
    """A perfectly linear ramp is exactly predictable by persistence-plus-trend, and a
    method that had seen the holdout would score zero error. Non-zero naive error proves
    the split is real."""
    series = pd.Series(np.arange(100, dtype=float),
                       index=pd.date_range("2020-02-01", periods=100, freq="D"))
    results = fc.evaluate(series, holdout_days=10, ma_window=7)
    naive = next(r for r in results if r.method == "naive_persistence")
    assert naive.mae == pytest.approx(1.0), "persistence lags a unit ramp by exactly 1"
    assert naive.n == 10


def test_short_series_is_refused_rather_than_guessed():
    series = pd.Series(np.arange(5, dtype=float),
                       index=pd.date_range("2020-02-01", periods=5, freq="D"))
    assert fc.evaluate(series, holdout_days=21) == []


def test_report_names_the_benchmark_when_nothing_beats_it():
    series = pd.Series(np.arange(60, dtype=float),
                       index=pd.date_range("2020-02-01", periods=60, freq="D"))
    naive = fc.ForecastResult("naive_persistence", mae=1.0, rmse=1.0, mape=None, n=10)
    worse = fc.ForecastResult("moving_average_7d", mae=3.0, rmse=3.2, mape=None, n=10)
    text = fc.report(series, [naive, worse])
    assert "WORSE" in text
    assert "Nothing beats persistence" in text


# ---------------------------------------------------------------------------------
# Statistics against hand-computed values
# ---------------------------------------------------------------------------------
# These fixtures are worked out by hand in the docstrings rather than generated from the
# implementation. A test that computes its own expectation the same way the code does
# proves only that the code is self-consistent.

def test_modified_z_score_matches_a_hand_computed_value():
    """Series [1, 2, 3, 4, 100].

        median            = 3
        absolute devs     = [2, 1, 0, 1, 97]
        MAD               = median of those = 1
        modified z of 100 = 0.6745 x (100 - 3) / 1 = 65.4265

    The point of the fixture is the contrast with a conventional z-score: the standard
    deviation of this series is about 43.2, so a plain z-score of the outlier is only
    ~2.2 - below a 3-sigma threshold. The single extreme value inflates the very statistic
    meant to detect it. The median and MAD barely move, which is why the detector uses
    them.
    """
    from analytics.anomaly_detection import MAD_SCALE

    values = np.array([1.0, 2.0, 3.0, 4.0, 100.0])
    median = np.median(values)
    mad = np.median(np.abs(values - median))

    assert median == pytest.approx(3.0)
    assert mad == pytest.approx(1.0)

    modified_z = MAD_SCALE * (100.0 - median) / mad
    assert modified_z == pytest.approx(65.4265, abs=1e-4)

    conventional_z = (100.0 - values.mean()) / values.std(ddof=1)
    assert conventional_z < 3.0, "a plain z-score misses this outlier entirely"


def test_iqr_fences_match_a_hand_computed_value():
    """P25 = 8.0, P75 = 10.0.

        IQR         = 2.0
        1.5 x IQR   = 3.0  ->  fences 5.0 and 13.0
        3.0 x IQR   = 6.0  ->  far-out bounds 2.0 and 16.0
    """
    from analytics.statistics import IQR_CRITICAL_MULTIPLIER, IQR_WARNING_MULTIPLIER

    p25, p75 = 8.0, 10.0
    iqr = p75 - p25

    assert p25 - IQR_WARNING_MULTIPLIER * iqr == pytest.approx(5.0)
    assert p75 + IQR_WARNING_MULTIPLIER * iqr == pytest.approx(13.0)
    assert p25 - IQR_CRITICAL_MULTIPLIER * iqr == pytest.approx(2.0)
    assert p75 + IQR_CRITICAL_MULTIPLIER * iqr == pytest.approx(16.0)

    baseline = make_baseline(p25=p25, p75=p75)
    assert baseline.lower_fence == pytest.approx(5.0)
    assert baseline.upper_fence == pytest.approx(13.0)


def test_measured_tp2_global_fences_would_exclude_normal_operation():
    """Regression guard on the finding that shaped the whole analytics design.

    Measured over February 2020 across all operating states, TP2 has P25 = -0.012 and
    P75 = -0.010, so the 1.5 x IQR fences land at -0.015 and -0.007 - while the sensor
    genuinely reaches 10.68 bar whenever the compressor runs. A global rule would flag
    every loaded scan.
    """
    baseline = make_baseline(sensor_code="TP2", operating_state="ALL",
                             p25=-0.012, p75=-0.010, stddev=3.251, mean=1.368)
    assert baseline.upper_fence == pytest.approx(-0.007)
    assert baseline.upper_fence < 10.68, (
        "the fence sits below the sensor's normal operating maximum, which is exactly "
        "why baselines are computed per operating state"
    )
