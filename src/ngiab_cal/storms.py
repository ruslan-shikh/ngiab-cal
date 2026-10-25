"""The drier and the wetter forcing the robust objectives run beside the unchanged one.

A storm is the seven days either side of a calibration-period flood's peak, and storms that overlap
are one. Each forcing changes the precipitation of every storm and nothing else: P′ = f·s·P^γ, with
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
    with xr.open_dataset(forcing_file) as ds:
        forcing = ds.load()
    storms = storm_steps(forcing_hours(forcing), list(peaks))
    rain = forcing[PRECIP].transpose("catchment-id", "time").to_numpy()
    for path, way in ((drier_file, -1), (wetter_file, 1)):
        rate = change_storms(rain, storms, depth**way, concentration**way)
        with_rain(forcing, rate).to_netcdf(path)
