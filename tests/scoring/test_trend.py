"""Unit tests for the phenology-trend classifier (issue #301 redesign)."""

from __future__ import annotations

import pytest

from foray.scoring.trend import phenology_trend

# A stylized season: ramps up to a September peak, tails off through November.
SEASON = {6: 2, 7: 8, 8: 22, 9: 40, 10: 18, 11: 4}


@pytest.mark.parametrize(
    ("months", "expected"),
    [
        ([9], "peak"),  # the peak month itself
        ([8], "building"),  # shoulder, more activity ahead than behind
        ([10], "past-peak"),  # shoulder, more activity behind than ahead
        ([6], "off"),  # in-window but far below the peak month
        ([8, 9, 10], "peak"),  # multi-month window classified by its strongest month
    ],
)
def test_classifies_window_against_season(months: list[int], expected: str) -> None:
    assert phenology_trend(SEASON, months) == expected


def test_none_when_nothing_to_classify() -> None:
    assert phenology_trend({}, [9]) is None
    assert phenology_trend(SEASON, []) is None
    assert phenology_trend({9: 0}, [9]) is None


def test_january_window_wraps_to_december_shoulder() -> None:
    # Peak in January; December is the "behind" month, February the "ahead" one.
    winter = {12: 6, 1: 30, 2: 20}
    assert phenology_trend(winter, [1]) == "peak"
    assert phenology_trend(winter, [2]) == "past-peak"
    assert phenology_trend(winter, [12]) == "building"
