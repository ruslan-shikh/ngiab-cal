"""Write copies of a NetCDF forcing with the storms changed: the drier and wetter copies for
kge-flood-robust, and the copies the stress test reruns the calibrated parameters with.

A storm is the seven days either side of a flood's peak, and storms that overlap are joined. A
change (f, g) sets each storm's precipitation to P' = f * s * P^g, where s keeps each catchment's
storm total. So g changes only how the rain is spread over the storm's hours: g above 1 packs it
into the heaviest hours, g below 1 spreads it out, and a dry hour stays dry. f then scales the
total. Nothing else in the forcing changes.
"""

import shutil
from pathlib import Path
from typing import Iterable

import netCDF4
import numpy as np
import pandas as pd

PRECIP = "precip_rate"  # mm/s
APCP = "APCP_surface"  # kg/m2 over the hour, precip_rate * 3600
STORM_HOURS = 7 * 24
SECONDS_PER_HOUR = 3600
# number of depth steps from no change to each end of the stress test
STRESS_STEPS = 10


def forcing_hours(forcing: netCDF4.Dataset) -> pd.DatetimeIndex:
    """Return the hour of each time step of an NGIAB NetCDF forcing, from its Time variable."""
    time = forcing["Time"]
    units = getattr(time, "units", None)
    if units != "s":
        raise ValueError(f"forcing Time units {units!r}, expected seconds")
    epoch = pd.to_datetime(time.epoch_start, format="%m/%d/%Y %H:%M:%S")
    # NGIAB forcings repeat the same times for every catchment
    seconds = time[0] if "catchment-id" in time.dimensions else time[:]
    return pd.DatetimeIndex(epoch + pd.to_timedelta(np.ma.filled(seconds), unit="s"))


def storm_steps(hours: pd.DatetimeIndex, peaks: Iterable[pd.Timestamp]) -> list[np.ndarray]:
    """Return the positions in ``hours`` of each storm, in time order. Storms that overlap or
    touch are joined into one."""
    window = pd.Timedelta(hours=STORM_HOURS)
    in_a_storm = np.zeros(len(hours), dtype=bool)
    for peak in map(pd.Timestamp, peaks):
        storm = (hours >= peak - window) & (hours <= peak + window)
        if not storm.any():
            raise ValueError(f"the storm of the flood peaking at {peak} is outside the forcing")
        in_a_storm |= storm

    # group consecutive storm hours into one storm each
    storms: list[list[int]] = []
    for position in np.flatnonzero(in_a_storm):
        if storms and position == storms[-1][-1] + 1:
            storms[-1].append(position)
        else:
            storms.append([position])
    return [np.array(storm) for storm in storms]


def change_storm(rain: np.ndarray, depth: float, exponent: float) -> np.ndarray:
    """Change one storm's ``rain`` (catchments x hours).

    Raises each hour's rain to the power ``exponent``, rescales each catchment's rain so that its
    storm total is unchanged, then multiplies it by ``depth``. Dry hours stay dry.
    """
    rain = rain.astype(float)
    if not np.isfinite(rain).all():
        raise ValueError("the forcing's precipitation has a missing value inside a storm")
    wet = rain > 0
    reshaped = np.zeros_like(rain)
    reshaped[wet] = rain[wet] ** exponent

    totals = rain.sum(axis=1)
    reshaped_totals = reshaped.sum(axis=1)
    scale = np.ones(len(rain))  # one factor per catchment
    has_rain = reshaped_totals > 0
    scale[has_rain] = totals[has_rain] / reshaped_totals[has_rain]
    return depth * scale.reshape(-1, 1) * reshaped


def write_changed_forcings(
    forcing_file: Path,
    peaks: Iterable[pd.Timestamp],
    changes: Iterable[tuple[float, float]],
    paths: Iterable[Path],
) -> None:
    """Copy ``forcing_file`` to each of ``paths`` and apply the matching (f, g) of ``changes`` to
    the storms of the floods peaking at ``peaks``. Only the precipitation inside the storms is
    rewritten."""
    with netCDF4.Dataset(forcing_file) as forcing:
        storms = storm_steps(forcing_hours(forcing), list(peaks))
    for (depth, exponent), path in zip(changes, paths, strict=True):
        shutil.copyfile(forcing_file, path)
        with netCDF4.Dataset(path, "a") as nc:
            precip = nc[PRECIP]
            if precip.dimensions != ("catchment-id", "time"):
                raise ValueError(f"{PRECIP} has dimensions {precip.dimensions}")
            for storm in storms:
                hours = slice(storm[0], storm[-1] + 1)
                old = np.ma.filled(precip[:, hours], np.nan)
                new = change_storm(old, depth, exponent).astype(precip.dtype)
                precip[:, hours] = new
                if APCP in nc.variables:
                    # update APCP only where the rate changed, so it keeps its own rounding
                    # elsewhere
                    changed = new != old
                    apcp = nc[APCP][:, hours]
                    apcp[changed] = new[changed] * new.dtype.type(SECONDS_PER_HOUR)
                    nc[APCP][:, hours] = apcp


def stress_changes(
    depth: float, concentration: float, steps: int = STRESS_STEPS
) -> list[tuple[float, float]]:
    """Return the stress test's changes (f, g).

    Depth f goes from 1/``depth`` to ``depth`` in ``2 * steps`` equal steps of ln f. The
    concentration g is 1/``concentration`` and ``concentration``, each with no depth change and
    with each end of depth. Repeats and the unchanged storm (1, 1) are left out.
    """
    factors = [depth ** (k / steps) for k in range(-steps, steps + 1)]
    driest, wettest = factors[0], factors[-1]
    depth_sweep = [(f, 1.0) for f in factors]
    concentration_changes = [
        (f, g) for f in (1.0, driest, wettest) for g in (concentration**-1, concentration)
    ]
    changes = []
    for change in depth_sweep + concentration_changes:
        if change != (1.0, 1.0) and change not in changes:
            changes.append(change)
    return changes


def write_stress_forcings(
    forcing_file: Path,
    peaks: Iterable[pd.Timestamp],
    depth: float,
    concentration: float,
    folder: Path,
) -> pd.DataFrame:
    """Write the stress test's forcings to ``folder`` as stress_01.nc onwards, with the storms of
    the floods peaking at ``peaks`` changed. Return each file's depth, concentration and name."""
    changes = stress_changes(depth, concentration)
    names = [f"stress_{k:02d}.nc" for k in range(1, len(changes) + 1)]
    write_changed_forcings(forcing_file, peaks, changes, [folder / name for name in names])
    return pd.DataFrame(changes, columns=["depth", "concentration"]).assign(forcing=names)
