"""Find the floods that the kge-flood objective scores.

A flood is a peak of the gage's hourly flow above its 2-year flood (Alam et al. 2026, JAWRA
62(1) e70093). Two peaks count as separate floods when they are at least 5 + ln(drainage area in
mi2) days apart and the flow between them drops below 3/4 of the smaller peak (USWRC 1976, as
given by Lang et al. 1999). A flood is left out if its peak hour or the hour on either side is
missing or estimated, because the true peak may then be in the gap.
"""

import io
import logging
import math
import urllib.request

import numpy as np
import pandas as pd

PEAKS_URL = (
    "https://api.waterdata.usgs.gov/ogcapi/v1/collections/peaks/items"
    "?monitoring_location_id=USGS-{gage}&parameter_code=00060&f=csv&skipGeometry=true"
    "&limit=10000&properties=water_year,value,qualifier"
)
# USGS peak qualifiers (legacy peak_cd codes in brackets). Regulated [6] and urban [C] peaks
# come from a changed flow regime, historic [7] peaks lie outside the systematic record, a
# daily mean [1] is not an instantaneous peak and a dam failure [3] is not a natural flood.
AFFECTED_QUALIFIERS = frozenset({"REGULATED", "URBAN"})
LEFT_OUT_QUALIFIERS = AFFECTED_QUALIFIERS | {"HISTORIC", "MAXDAILYMEAN", "DAMFAILURE"}
UNKNOWN_REGULATION = "UNKNOWNREGULATION"  # [5] regulated or diverted to an unknown degree
CMS_PER_CFS = 0.028316847
KM2_PER_MI2 = 2.589988110336


def water_year(t: pd.Timestamp) -> int:
    """Return the water year (October to September) that ``t`` falls in."""
    return t.year + (t.month >= 10)


def read_annual_peaks(text: str) -> pd.DataFrame:
    """Read the CSV from the USGS peaks API into water_year, peak_cfs and codes columns."""
    columns = ["water_year", "peak_cfs", "codes"]
    try:
        table = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:  # USGS answers a gage without peaks with an empty body
        return pd.DataFrame(columns=columns)
    table = table[table["value"].str.strip() != ""]
    peaks = pd.DataFrame(
        {
            "water_year": table["water_year"].astype(int),
            "peak_cfs": table["value"].astype(float),
            "codes": table["qualifier"].map(lambda text: frozenset(text.split(",")) - {""}),
        }
    )
    return peaks.sort_values("water_year").reset_index(drop=True)


def download_annual_peaks(gage_id: str) -> pd.DataFrame:
    """Download the gage's annual peaks from USGS."""
    try:
        with urllib.request.urlopen(PEAKS_URL.format(gage=gage_id), timeout=120) as response:
            return read_annual_peaks(response.read().decode())
    except OSError as e:  # URLError, HTTPError and timeouts
        raise ValueError(
            f"Unable to download the annual peaks of {gage_id} from USGS ({e}); "
            "give the 2-year flood with --q2-cfs"
        ) from e


def two_year_flood_cfs(peaks: pd.DataFrame, record_years: range) -> float:
    """Return the median annual peak, which is exceeded in half of all years.

    Historic peaks, daily means, dam failures and peaks affected by regulation or urbanization
    are left out. If peaks in ``record_years`` (the years being calibrated) are affected, the
    median describes a different flow regime from the one being calibrated, so this logs a
    warning, as it does when peaks regulated to an unknown degree are used.
    """
    codes = peaks["codes"]
    usable = peaks[~codes.map(lambda c: bool(c & LEFT_OUT_QUALIFIERS)).astype(bool)]
    if usable.empty:
        raise ValueError(
            "The gage has no usable annual peaks (none, or all historic, daily means, dam "
            "failures or affected by regulation or urbanization); give the 2-year flood with "
            "--q2-cfs"
        )
    affected = codes.map(lambda c: bool(c & AFFECTED_QUALIFIERS)).astype(bool)
    affected_in_record = affected & peaks["water_year"].isin(record_years)
    if affected_in_record.any():
        logging.warning(
            f"{affected_in_record.sum()} annual peaks in the calibrated years are affected by "
            "regulation or urbanization, so the 2-year flood from the other years may not fit "
            "them; consider giving it with --q2-cfs"
        )
    unknown = usable["codes"].map(lambda c: UNKNOWN_REGULATION in c).astype(bool)
    if unknown.any():
        logging.warning(
            f"{unknown.sum()} of the annual peaks used are affected to an unknown degree by "
            "regulation or diversion; if the gage is regulated, give the 2-year flood with "
            "--q2-cfs"
        )
    return float(usable["peak_cfs"].median())


def separation_hours(area_mi2: float) -> int:
    """Return the minimum time between two separate floods: 5 + ln(area in mi2) days, in hours."""
    return math.ceil(24 * (5 + math.log(area_mi2)))


def are_separate(flow: np.ndarray, a: int, b: int, separation: int) -> bool:
    """Return whether the peaks at hours ``a`` and ``b`` are separate floods."""
    first, last = min(a, b), max(a, b)
    if last - first < separation:
        return False
    lowest_between = np.nanmin(flow[first : last + 1])
    return lowest_between < 0.75 * min(flow[a], flow[b])


def independent_peaks(flow: np.ndarray, threshold: float, separation: int) -> list[int]:
    """Return the hours of the separate peaks of ``flow`` at or above ``threshold``.

    Peaks are taken from the largest down. Each is kept if it is separate from every peak
    already kept, so of two peaks that are not separate, the larger one is the flood.
    """
    q = np.where(np.isnan(flow), -np.inf, flow)  # a missing hour is never a peak
    before = np.concatenate(([-np.inf], q[:-1]))
    after = np.concatenate((q[1:], [-np.inf]))
    # on a flat top, the peak is the last hour
    candidates = np.flatnonzero((q >= threshold) & (q >= before) & (q > after))

    kept: list[int] = []
    for hour in sorted(candidates, key=lambda i: q[i], reverse=True):
        # check the nearest kept peaks first, since they are the likeliest to rule it out
        nearest_first = sorted(kept, key=lambda k: abs(k - hour))
        if all(are_separate(flow, hour, k, separation) for k in nearest_first):
            kept.append(hour)
    return sorted(kept)


def on_the_hour(series: pd.Series) -> pd.Series:
    """Return the values stamped on the hour, on a complete hourly index with NaN for gaps.

    ngen-cal compares the hourly simulation with these values only.
    """
    series = series[~series.index.duplicated(keep="first")]
    series = series[series.index == series.index.floor("h")]
    hours = pd.date_range(series.index.min(), series.index.max(), freq="h")
    return series.replace([np.inf, -np.inf], np.nan).reindex(hours)


def find_floods(
    flow: pd.Series, estimated: pd.Series, threshold_cms: float, area_mi2: float
) -> pd.DataFrame:
    """Return the floods in ``flow`` (m3/s) as peak_time and obs_peak_cms columns.

    ``estimated`` is True where USGS marks a value as estimated.
    """
    q = on_the_hour(flow.astype(float))
    q = q.where(q >= 0)  # USGS marks some missing values -999999
    estimated = estimated[~estimated.index.duplicated(keep="first")]
    estimated = estimated.reindex(q.index, fill_value=False).astype(bool)

    rows = []
    for i in independent_peaks(q.to_numpy(), threshold_cms, separation_hours(area_mi2)):
        around = slice(i - 1, i + 2)  # the peak hour and the hour on either side
        complete = 0 < i < len(q) - 1 and q.iloc[around].notna().all()
        if complete and not estimated.iloc[around].any():
            rows.append({"peak_time": q.index[i], "obs_peak_cms": float(q.iloc[i])})
        else:
            logging.warning(
                f"Leaving out the flood peak of {q.iloc[i]:.1f} m3/s at {q.index[i]}: "
                "its hour or the hour on either side is missing or estimated"
            )
    return pd.DataFrame(rows, columns=["peak_time", "obs_peak_cms"])
