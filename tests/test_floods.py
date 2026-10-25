import math

import numpy as np
import pandas as pd
import pytest

from ngiab_cal import floods

PEAK_FILE = """# US Geological Survey
# retrieved: 2026-10-24
agency_cd\tsite_no\tpeak_dt\tpeak_tm\tpeak_va\tpeak_cd\tgage_ht
5s\t15s\t10d\t6s\t8s\t33s\t8s
USGS\t01234567\t1936-03-18\t\t9000\t7\t
USGS\t01234567\t2001-04-02\t\t100\t\t3.1
USGS\t01234567\t2002-11-20\t\t300\t\t
USGS\t01234567\t2003-05-07\t\t200\t2\t
USGS\t01234567\t2004-09-18\t\t5000\t6,C\t
USGS\t01234567\t2005-01-01\t\t\t\t2.0
"""

HOURS = pd.date_range("2020-01-01", periods=24 * 60, freq="h")


def hydrograph(*peaks: tuple[int, float], base: float = 1.0) -> pd.Series:
    """Triangular floods of 24 hours either side on a steady base flow, at hours of HOURS."""
    q = np.full(len(HOURS), base)
    for at, height in peaks:
        for k in range(-24, 25):
            q[at + k] = max(q[at + k], base + (height - base) * (1 - abs(k) / 24))
    return pd.Series(q, index=HOURS)


def none_estimated(flow: pd.Series) -> pd.Series:
    return pd.Series(False, index=flow.index)


def test_annual_peaks_are_read_with_their_codes():
    peaks = floods.read_annual_peaks(PEAK_FILE)
    assert peaks["peak_cfs"].tolist() == [9000, 100, 300, 200, 5000]
    assert peaks["codes"].tolist() == [{"7"}, set(), set(), {"2"}, {"6", "C"}]


def test_the_two_year_flood_is_the_median_of_the_systematic_peaks():
    # the historic peak (7) and the regulated, urbanized one (6, C) are left out
    assert floods.two_year_flood_cfs(floods.read_annual_peaks(PEAK_FILE)) == 200


def test_a_gage_without_systematic_peaks_has_no_two_year_flood():
    with pytest.raises(ValueError):
        floods.two_year_flood_cfs(floods.read_annual_peaks(PEAK_FILE.split("USGS\t")[0]))


def test_floods_are_separated_by_five_days_and_the_log_of_the_area():
    assert floods.separation_hours(1.0) == 120
    assert floods.separation_hours(math.e) == 144


def test_a_smaller_peak_within_the_separation_is_not_a_flood():
    flow = hydrograph((200, 50.0), (300, 40.0))
    found = floods.find_floods(flow, none_estimated(flow), 10.0, area_mi2=1.0)
    assert found["peak_time"].tolist() == [HOURS[200]]


def test_a_smaller_peak_without_a_deep_enough_trough_is_not_a_flood():
    # 200 hours apart, but the flow between stays above 3/4 of the smaller peak
    flow = hydrograph((200, 50.0), (400, 40.0), base=31.0)
    found = floods.find_floods(flow, none_estimated(flow), 35.0, area_mi2=1.0)
    assert found["peak_time"].tolist() == [HOURS[200]]


def test_separate_peaks_above_the_threshold_are_floods():
    flow = hydrograph((200, 50.0), (400, 40.0), (600, 9.0))
    found = floods.find_floods(flow, none_estimated(flow), 10.0, area_mi2=1.0)
    assert found["peak_time"].tolist() == [HOURS[200], HOURS[400]]
    assert found["obs_peak_cms"].tolist() == [50.0, 40.0]


def test_only_values_on_the_hour_count():
    flow = hydrograph((200, 50.0))
    between = pd.Series([80.0], index=[HOURS[200] + pd.Timedelta(minutes=15)])
    flow = pd.concat([flow, between]).sort_index()
    found = floods.find_floods(flow, none_estimated(flow), 10.0, area_mi2=1.0)
    assert found["obs_peak_cms"].tolist() == [50.0]


def test_a_peak_beside_an_estimated_or_missing_hour_is_not_scored():
    flow = hydrograph((200, 50.0), (400, 40.0))
    estimated = none_estimated(flow)
    estimated[HOURS[201]] = True
    found = floods.find_floods(flow.drop(HOURS[399]), estimated, 10.0, area_mi2=1.0)
    assert found.empty
