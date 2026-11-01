import numpy as np
import pandas as pd
import pytest

from ngiab_cal import floods

# as the USGS peaks API returns them
PEAK_FILE = """x,y,water_year,value,qualifier
,,2003,200,ESTIMATED
,,1936,9000,HISTORIC
,,2001,100,
,,2002,300,
,,2004,5000,"REGULATED,URBAN"
,,2005,,
,,2006,7000,MAXDAILYMEAN
,,2007,6000,DAMFAILURE
"""

HOURS = pd.date_range("2020-01-01", periods=24 * 60, freq="h")


def hydrograph(*peaks: tuple[int, float], base: float = 1.0) -> pd.Series:
    """Return an hourly flow with a triangular flood, 24 hours either side, at each (hour, peak)."""
    q = np.full(len(HOURS), base)
    for at, height in peaks:
        for k in range(-24, 25):
            q[at + k] = max(q[at + k], base + (height - base) * (1 - abs(k) / 24))
    return pd.Series(q, index=HOURS)


def find(flow: pd.Series, threshold: float, estimated: pd.Series | None = None) -> pd.DataFrame:
    """Find floods in a 1 mi2 basin, where floods must be 5 days apart."""
    if estimated is None:
        estimated = pd.Series(False, index=flow.index)
    return floods.find_floods(flow, estimated, threshold, area_mi2=1.0)


def test_annual_peaks_are_read_with_water_years_and_codes():
    peaks = floods.read_annual_peaks(PEAK_FILE)
    # the 2005 row has no discharge
    assert peaks["water_year"].tolist() == [1936, 2001, 2002, 2003, 2004, 2006, 2007]
    assert peaks["peak_cfs"].tolist() == [9000, 100, 300, 200, 5000, 7000, 6000]
    assert peaks["codes"].tolist()[:5] == [
        {"HISTORIC"},
        set(),
        set(),
        {"ESTIMATED"},
        {"REGULATED", "URBAN"},
    ]


def test_two_year_flood_leaves_out_peaks_that_are_not_natural_instantaneous_floods(caplog):
    peaks = floods.read_annual_peaks(PEAK_FILE)
    # median of 100, 300 and 200; the historic, affected, daily-mean and dam-failure peaks are out
    assert floods.two_year_flood_cfs(peaks, range(2001, 2004)) == 200
    assert "affected" not in caplog.text
    # the calibrated years include the affected 2004 peak
    floods.two_year_flood_cfs(peaks, range(2003, 2006))
    assert "1 annual peaks in the calibrated years are affected" in caplog.text
    peaks.loc[len(peaks)] = [2008, 150.0, frozenset({"UNKNOWNREGULATION"})]
    assert floods.two_year_flood_cfs(peaks, range(2001, 2004)) == 175
    assert "affected to an unknown degree" in caplog.text


def test_a_gage_without_peaks_asks_for_the_2_year_flood():
    # USGS answers with an empty body
    with pytest.raises(ValueError, match="no usable annual peaks.*--q2-cfs"):
        floods.two_year_flood_cfs(floods.read_annual_peaks(""), range(2001, 2004))


def test_floods_are_separate_peaks_above_the_threshold():
    # 300 is within 5 days of the larger 200; 700 is below the threshold
    flow = hydrograph((200, 50.0), (300, 40.0), (500, 40.0), (700, 9.0))
    found = find(flow, threshold=10.0)
    assert found["peak_time"].tolist() == [HOURS[200], HOURS[500]]
    assert found["obs_peak_cms"].tolist() == [50.0, 40.0]


def test_peaks_without_a_deep_enough_trough_are_one_flood():
    # 200 hours apart, but the flow between stays above 3/4 of the smaller peak
    flow = hydrograph((200, 50.0), (400, 40.0), base=31.0)
    assert find(flow, threshold=35.0)["peak_time"].tolist() == [HOURS[200]]


def test_the_usgs_missing_value_code_is_not_a_trough():
    flow = hydrograph((200, 50.0), (400, 40.0), base=31.0)
    flow[HOURS[300]] = -999999.0
    assert find(flow, threshold=35.0)["peak_time"].tolist() == [HOURS[200]]


def test_only_values_on_the_hour_count():
    flow = hydrograph((200, 50.0))
    between = pd.Series([80.0], index=[HOURS[200] + pd.Timedelta(minutes=15)])
    flow = pd.concat([flow, between]).sort_index()
    assert find(flow, threshold=10.0)["obs_peak_cms"].tolist() == [50.0]


def test_a_peak_next_to_an_estimated_or_missing_hour_is_left_out(caplog):
    flow = hydrograph((200, 50.0), (400, 40.0))
    estimated = pd.Series(False, index=flow.index)
    estimated[HOURS[201]] = True
    assert find(flow.drop(HOURS[399]), threshold=10.0, estimated=estimated).empty
    assert caplog.text.count("Leaving out the flood peak") == 2
