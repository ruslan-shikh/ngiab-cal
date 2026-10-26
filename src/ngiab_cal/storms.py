"""The changed storm forcings: the drier and the wetter one the robust objectives run beside the
unchanged one, and those the stress test reruns the calibrated parameters under.

A storm is the seven days either side of a flood's peak, the calibration period's floods for the
robust objectives and the validation period's for the stress test, and storms that overlap are
one. Each forcing changes the precipitation of every storm and nothing else: P′ = f·s·P^γ, with
the depth factor f, the concentration exponent γ, and s set for each catchment and storm so that γ
alone leaves the catchment's storm total unchanged. γ above 1 packs a storm's rain into its heavier
hours, γ below 1 spreads it out, and a dry hour stays dry. The drier forcing takes f = 1/d and
γ = 1/g, the wetter f = d and γ = g, so the two bracket a simulated peak, which rises with both.
"""

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import xarray as xr

PRECIP = "precip_rate"  # mm s⁻¹
APCP = "APCP_surface"  # kg m⁻² over the hour, precip_rate × 3600
STORM_HOURS = 7 * 24
SECONDS_PER_HOUR = 3600
# Steps of depth to each end of the stress test, the grid of Yang et al. (2026, Fig. 7)
STRESS_STEPS = 10


def forcing_hours(forcing: xr.Dataset) -> pd.DatetimeIndex:
    """The hour of each step of an NGIAB NetCDF forcing, from its ``Time`` variable."""
    time = forcing["Time"]
    if time.attrs.get("units") != "s":
        raise ValueError(f"forcing Time units {time.attrs.get('units')!r}, expected seconds")
    epoch = pd.to_datetime(time.attrs["epoch_start"], format="%m/%d/%Y %H:%M:%S")
    if "catchment-id" in time.dims:
        time = time.isel({"catchment-id": 0})
    return pd.DatetimeIndex(epoch + pd.to_timedelta(time.to_numpy(), unit="s"))


def storm_steps(hours: pd.DatetimeIndex, peaks: Iterable[pd.Timestamp]) -> list[np.ndarray]:
    """The positions on ``hours`` of each storm, in time order, storms that overlap or touch
    joined into one."""
    window = pd.Timedelta(hours=STORM_HOURS)
    inside = np.zeros(len(hours), dtype=bool)
    for peak in map(pd.Timestamp, peaks):
        storm = (hours >= peak - window) & (hours <= peak + window)
        if not storm.any():
            raise ValueError(f"the storm of the flood peaking at {peak} is outside the forcing")
        inside |= storm
    at = np.flatnonzero(inside)
    return np.split(at, np.flatnonzero(np.diff(at) > 1) + 1) if at.size else []


def change_storms(
    rain: np.ndarray, storms: list[np.ndarray], depth: float, exponent: float
) -> np.ndarray:
    """``rain`` (catchments × hours) with every storm's depth scaled by ``depth`` and its hourly
    rain raised to ``exponent``, each catchment's storm total held before the scaling."""
    out = rain.astype(float)
    for storm in storms:
        p = out[:, storm]
        if not np.isfinite(p).all():
            raise ValueError("the forcing's precipitation has a missing value inside a storm")
        wet = p > 0
        powered = np.power(p, exponent, where=wet, out=np.zeros_like(p))
        total, powered_total = p.sum(axis=1), powered.sum(axis=1)
        s = np.divide(total, powered_total, out=np.ones_like(total), where=powered_total > 0)
        out[:, storm] = depth * s[:, None] * powered
    return out


def with_rain(forcing: xr.Dataset, rate: np.ndarray) -> xr.Dataset:
    """``forcing`` with ``rate`` (catchments × hours) as its precipitation rate, and
    ``APCP_surface`` rewritten to match wherever the rate changed."""
    out = forcing.copy()
    old = forcing[PRECIP].transpose("catchment-id", "time")
    new = rate.astype(old.dtype)
    out[PRECIP] = old.copy(data=new).transpose(*forcing[PRECIP].dims)
    if APCP in forcing:
        apcp = forcing[APCP].transpose("catchment-id", "time")
        changed = new != old.to_numpy()
        values = apcp.to_numpy().copy()
        values[changed] = new[changed] * new.dtype.type(SECONDS_PER_HOUR)
        out[APCP] = apcp.copy(data=values).transpose(*forcing[APCP].dims)
    return out


def write_changed_forcings(
    forcing_file: Path,
    peaks: Iterable[pd.Timestamp],
    changes: Iterable[tuple[float, float]],
    paths: Iterable[Path],
) -> None:
    """Write a forcing for each change (f, γ) to the path beside it, changing the storms of the
    floods peaking at ``peaks``."""
    with xr.open_dataset(forcing_file) as ds:
        forcing = ds.load()
    storms = storm_steps(forcing_hours(forcing), list(peaks))
    rain = forcing[PRECIP].transpose("catchment-id", "time").to_numpy()
    for (depth, exponent), path in zip(changes, paths, strict=True):
        with_rain(forcing, change_storms(rain, storms, depth, exponent)).to_netcdf(path)


def write_storm_forcings(
    forcing_file: Path,
    peaks: Iterable[pd.Timestamp],
    depth: float,
    concentration: float,
    drier_file: Path,
    wetter_file: Path,
) -> None:
    """Write the drier forcing, at 1/``depth`` and 1/``concentration``, and the wetter one, at
    ``depth`` and ``concentration``, changing the storms of the floods peaking at ``peaks``."""
    changes = [(depth**way, concentration**way) for way in (-1, 1)]
    write_changed_forcings(forcing_file, peaks, changes, [drier_file, wetter_file])


def stress_changes(
    depth: float, concentration: float, steps: int = STRESS_STEPS
) -> list[tuple[float, float]]:
    """The stress test's changes (f, γ): depth in ``steps`` equal steps of ln f to 1/``depth``
    and to ``depth``, then the concentration's two ends, 1/``concentration`` and
    ``concentration``, alone and with each end of depth. A change that repeats another, or the
    unchanged storm, is left out."""
    factors = [depth ** (k / steps) for k in range(-steps, steps + 1)]
    sweep = [(f, 1.0) for f in factors]
    ends = [(f, concentration**way) for f in (1.0, factors[0], factors[-1]) for way in (-1, 1)]
    return [change for change in dict.fromkeys(sweep + ends) if change != (1.0, 1.0)]


def write_stress_forcings(
    forcing_file: Path,
    peaks: Iterable[pd.Timestamp],
    depth: float,
    concentration: float,
    folder: Path,
) -> pd.DataFrame:
    """Write the stress test's changed forcings (``stress_changes``) to ``folder``, as
    stress_01.nc onwards, changing the storms of the floods peaking at ``peaks``; returns the
    depth, concentration and file name of each."""
    changes = stress_changes(depth, concentration)
    names = [f"stress_{k:02d}.nc" for k in range(1, len(changes) + 1)]
    write_changed_forcings(forcing_file, peaks, changes, [folder / name for name in names])
    return pd.DataFrame(changes, columns=["depth", "concentration"]).assign(forcing=names)


# The exponent search looks between a thousandth and a thousand, a bracket for the bisection and
# not a bound on the result: a Gini index no exponent in it gives is left unmatched.
LOG_EXPONENT_BRACKET = float(np.log(1000.0))


def gini(values: np.ndarray) -> float:
    """The Gini index of ``values``: 0 when all are equal, 1 − 1/n when one holds everything."""
    x = np.sort(np.asarray(values, dtype=float))
    n = x.size
    return float((2 * np.arange(1, n + 1) - n - 1) @ x / (n * x.sum()))


def matching_exponent(hourly: np.ndarray, target: float) -> float:
    """The exponent that gives ``hourly`` the Gini index ``target``, by bisection, since the index
    grows with the exponent; NaN where no exponent in the bracket gives it."""
    x = hourly / hourly.max()
    lo, hi = -LOG_EXPONENT_BRACKET, LOG_EXPONENT_BRACKET
    if not gini(x ** np.exp(lo)) <= target <= gini(x ** np.exp(hi)):
        return float("nan")
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if gini(x ** np.exp(mid)) < target else (lo, mid)
    return float(np.exp((lo + hi) / 2))


def storm_departures(
    rain: np.ndarray,
    products: dict[str, np.ndarray],
    storms: list[np.ndarray],
    hours: pd.DatetimeIndex,
) -> pd.DataFrame:
    """How far each product departs from ``rain`` at each storm it holds an hour of and both rain
    in: its depth ratio, and the exponent that gives the storm of ``rain`` the product's Gini
    index of hourly rain, both over the hours the product holds at every catchment, with the rain
    pooled over the catchments. ``rain`` and each product are catchments × ``hours``, in the same
    units, a product NaN wherever it holds nothing."""
    rows = []
    for name, other in products.items():
        for storm in storms:
            held = np.isfinite(other[:, storm]).all(axis=0)
            a, x = rain[:, storm][:, held], other[:, storm][:, held]
            if a.sum() > 0 and x.sum() > 0:
                rows.append(
                    {
                        "product": name,
                        "storm_start": hours[storm[0]],
                        "storm_end": hours[storm[-1]],
                        "hours": int(held.sum()),
                        "depth": float(x.sum() / a.sum()),
                        "exponent": matching_exponent(a.mean(axis=0), gini(x.mean(axis=0))),
                    }
                )
    columns = ["product", "storm_start", "storm_end", "hours", "depth", "exponent"]
    return pd.DataFrame(rows, columns=columns)


def _largest_mean(departures: pd.DataFrame, column: str) -> float:
    """The largest of the products' mean |ln| of ``column``, as a factor of 1 or more."""
    logs = np.abs(np.log(departures[column])).groupby(departures["product"]).mean().dropna()
    if logs.empty:
        raise ValueError("no product departs at any storm, so there is nothing to size from")
    return float(np.exp(logs.max()))


def mean_departures(departures: pd.DataFrame) -> tuple[float, float]:
    """d and g of the robust objectives: the mean departure in depth and in concentration of the
    product that departs most, each product's mean |ln| over its storms."""
    return _largest_mean(departures, "depth"), _largest_mean(departures, "exponent")


def largest_departures(departures: pd.DataFrame) -> tuple[float, float]:
    """The stress test's ends: the largest departure in depth and in concentration at any storm,
    taken either way, so 0.5 counts as 2."""
    if departures.empty or departures["exponent"].isna().all():
        raise ValueError("no product departs at any storm, so there is nothing to size from")
    return (
        float(np.exp(np.abs(np.log(departures["depth"])).max())),
        float(np.exp(np.abs(np.log(departures["exponent"])).max())),
    )
