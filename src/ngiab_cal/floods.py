"""The floods the flood objectives score.

A flood is an independent peak of the gauge's hourly flow above its 2-year flood, scored over the
seven days either side of its peak (Alam et al. 2026, JAWRA 62(1) e70093). Two peaks are separate
floods when they are at least 5 days plus ln(drainage area in mi²) apart and the flow between them
falls below three-quarters of the smaller (USWRC 1976, as Lang et al. 1999 give it). A flood
counts only where its peak hour and the hour either side are observed and none is estimated, so a
peak that may lie in a gap of the record is never scored.
"""

import bisect
import math
import urllib.request

import numpy as np
import pandas as pd

PEAKS_URL = "https://nwis.waterdata.usgs.gov/nwis/peak?site_no={gage}&agency_cd=USGS&format=rdb"
# Left out of the 2-year flood: historic peaks (7), and peaks affected by regulation or diversion
# (6) or by urbanization, mining, channelization or other changes (C)
EXCLUDED_PEAK_CODES = frozenset({"6", "7", "C"})
CMS_PER_CFS = 0.028316847
KM2_PER_MI2 = 2.589988110336


def read_annual_peaks(rdb: str) -> pd.DataFrame:
    """Each annual peak's discharge (ft³/s) and qualification codes, from an NWIS peak file."""
    lines = [line for line in rdb.splitlines() if line and not line.startswith("#")]
    rows = []
    if lines:
        header = lines[0].split("\t")
        # the second line gives each column's format
        for line in lines[2:]:
            row = dict(zip(header, line.split("\t")))
            value = row.get("peak_va", "").strip()
            if value:
                codes = frozenset(c.strip() for c in row.get("peak_cd", "").split(",") if c.strip())
                rows.append({"peak_cfs": float(value), "codes": codes})
    return pd.DataFrame(rows, columns=["peak_cfs", "codes"])


def download_annual_peaks(gage_id: str) -> pd.DataFrame:
    with urllib.request.urlopen(PEAKS_URL.format(gage=gage_id), timeout=120) as response:
        return read_annual_peaks(response.read().decode())


def two_year_flood_cfs(peaks: pd.DataFrame) -> float:
    """The median of the gauge's systematic annual peaks, which estimates the flow exceeded in half
    of all years without fitting a distribution."""
    kept = peaks[[not (codes & EXCLUDED_PEAK_CODES) for codes in peaks["codes"]]]
    if kept.empty:
        raise ValueError("the gauge has no annual peak to take the 2-year flood from")
    return float(kept["peak_cfs"].median())


def separation_hours(area_mi2: float) -> int:
    """The least separation of two floods, 5 days plus ln(area in mi²) days, in whole hours."""
    return math.ceil(24 * (5 + math.log(area_mi2)))


def independent_peaks(flow: np.ndarray, threshold: float, separation: int) -> np.ndarray:
    """Local maxima at or above ``threshold``, taken largest first, each kept when at least
    ``separation`` hours from its kept neighbours and the flow between them falls below 3/4 of the
    smaller. Comparing only the nearest kept peak on each side suffices: farther ones are farther
    and the trough between is no higher."""
    x = np.where(np.isfinite(flow), flow, -np.inf)
    left, right = np.r_[-np.inf, x[:-1]], np.r_[x[1:], -np.inf]
    candidates = np.flatnonzero((x >= threshold) & (x >= left) & (x > right))
    kept: list[int] = []
    for c in candidates[np.argsort(-x[candidates], kind="stable")]:
        i = bisect.bisect(kept, c)
        if all(
            abs(c - k) >= separation and np.nanmin(flow[min(c, k) : max(c, k) + 1]) < 0.75 * x[c]
            for k in kept[max(i - 1, 0) : i + 1]
        ):
            bisect.insort(kept, c)
    return np.array(kept, dtype=int)


def on_the_hour(series: pd.Series) -> pd.Series:
    """The values stamped on the hour, the ones ngen-cal pairs with the hourly simulation, on an
    unbroken hourly index, NaN where the gauge has none."""
    series = series[~series.index.duplicated(keep="first")]
    series = series[series.index == series.index.floor("h")]
    hours = pd.date_range(series.index.min(), series.index.max(), freq="h")
    return series.replace([np.inf, -np.inf], np.nan).reindex(hours)


def find_floods(
    flow: pd.Series, estimated: pd.Series, threshold_cms: float, area_mi2: float
) -> pd.DataFrame:
    """The floods of ``flow`` (m³/s) that are scored, each by its peak hour and observed peak."""
    q = on_the_hour(flow.astype(float))
    est = estimated[~estimated.index.duplicated(keep="first")]
    est = est.reindex(q.index, fill_value=False).to_numpy(bool)
    x = q.to_numpy(float)
    rows = []
    for i in independent_peaks(x, threshold_cms, separation_hours(area_mi2)):
        near = slice(max(i - 1, 0), i + 2)
        if x[near].size == 3 and np.isfinite(x[near]).all() and not est[near].any():
            rows.append({"peak_time": q.index[i], "obs_peak_cms": float(x[i])})
    return pd.DataFrame(rows, columns=["peak_time", "obs_peak_cms"])
