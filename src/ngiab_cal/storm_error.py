"""How far the MRMS precipitation products depart from a package's forcing at its flood storms:
the d and g the robust objectives take, and the stress test's ends.

Each MRMS 1-hour QPE product is fetched for the storms' hours from the Iowa Environmental
Mesonet's archive, decoded, and averaged over each catchment as the NGIAB preprocessor averages
its forcing: each grid cell weighted by the fraction of it the catchment's divide covers
(exactextract's coverage), a catchment missing wherever one of its cells is. Each value is
labelled with the hour its accumulation ends, as MRMS names its files and as AORC labels its
hours. Nothing is kept on disk. GaugeCorr is MRMS's gauge-corrected estimate before its v12.0
upgrade of October 2020 and MultiSensor Pass 2 after it; the archive's daily listing says which it
holds, so each is absent outside its own era. The departures themselves are ``storms``'s.

Needs the ``storms`` extra: ``pip install 'ngiab-cal[storms]'``.
"""

import gzip
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import xarray as xr

from ngiab_cal import storms

ARCHIVE = "https://mtarchive.geol.iastate.edu"
PRODUCTS = ("GaugeCorr_QPE_01H", "MultiSensor_QPE_01H_Pass2", "RadarOnly_QPE_01H")
# Files fetched and decoded at once; each decoded CONUS field holds about 400 MB while it is read
WORKERS = 4
GZIP_MAGIC = b"\x1f\x8b"


class ArchiveError(RuntimeError):
    """An archive request that kept failing."""


def get(url: str, attempts: int = 6) -> bytes | None:
    """The response body, or None where the archive holds no such file (HTTP 403 or 404). Any
    other failure is retried and then raised, so an outage never reads as an absent hour."""
    error: object = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=180) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            error = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            error = e
        time.sleep(min(60, 2**attempt))
    raise ArchiveError(f"{url}: {error}")


def directory(product: str, day: pd.Timestamp) -> str:
    return f"{ARCHIVE}/{day:%Y/%m/%d}/mrms/ncep/{product}/"


def file_url(product: str, hour: pd.Timestamp) -> str:
    return directory(product, hour) + f"{product}_00.00_{hour:%Y%m%d-%H}0000.grib2.gz"


def listed_hours(html: str, product: str) -> set[pd.Timestamp]:
    """The hours an archive directory listing holds a file of ``product`` for."""
    pattern = re.compile(re.escape(product) + r"_00\.00_(\d{8})-(\d{2})0000\.grib2\.gz")
    return {
        pd.to_datetime(m.group(1) + m.group(2), format="%Y%m%d%H") for m in pattern.finditer(html)
    }


@dataclass(frozen=True)
class Grid:
    """A regular latitude-longitude grid: its cell-centre longitudes and latitudes, ascending."""

    x: np.ndarray
    y: np.ndarray

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.y), len(self.x)

    def edges(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) of the cells' outer edges."""
        dx, dy = self.x[1] - self.x[0], self.y[1] - self.y[0]
        return self.x[0] - dx / 2, self.y[0] - dy / 2, self.x[-1] + dx / 2, self.y[-1] + dy / 2


def decode(raw: bytes) -> tuple[Grid, np.ndarray]:
    """One MRMS GRIB2 message: its grid, and its field with row 0 at the north edge, NaN wherever
    MRMS flags a cell as missing or outside its coverage (by a negative value)."""
    import pygrib

    msg = pygrib.fromstring(raw)
    if msg["gridType"] != "regular_ll" or msg["iScansNegatively"]:
        raise ValueError(f"unsupported MRMS grid {msg['gridType']}")
    south_first = bool(msg["jScansPositively"])
    lon0 = msg["longitudeOfFirstGridPointInDegrees"]
    lon0 = lon0 - 360.0 if lon0 > 180.0 else lon0
    lat0 = msg["latitudeOfFirstGridPointInDegrees"]
    dlon, dlat = msg["iDirectionIncrementInDegrees"], msg["jDirectionIncrementInDegrees"]
    lats = lat0 + (dlat if south_first else -dlat) * np.arange(msg["Nj"])
    grid = Grid(x=lon0 + dlon * np.arange(msg["Ni"]), y=np.sort(lats))
    field = np.ma.filled(np.ma.asarray(msg.values, dtype=np.float64), np.nan)
    field[field < 0] = np.nan
    return grid, field[::-1] if south_first else field


@dataclass(frozen=True)
class CatchmentWeights:
    """Sparse weights over the window ``rows`` × ``cols`` of a north-first field: entry k gives
    catchment ``catchment[k]`` the window's flat cell ``cell[k]`` at ``coverage[k]``."""

    n: int
    rows: slice
    cols: slice
    cell: np.ndarray
    catchment: np.ndarray
    coverage: np.ndarray


def catchment_weights(grid: Grid, divides, ids: list[str]) -> CatchmentWeights:
    """Coverage weights of ``grid``'s cells for each divide in ``ids``, in that order, over the
    window of the grid that holds them. ``divides`` is a GeoDataFrame with a ``divide_id`` column
    in any CRS."""
    from exactextract import exact_extract
    from exactextract.raster import NumPyRasterSource
    from pyproj import CRS

    wanted = divides.set_index("divide_id")
    missing = [i for i in ids if i not in wanted.index]
    if missing:
        raise KeyError(f"no divide polygon in the hydrofabric for {missing}")
    gdf = wanted.loc[ids].reset_index().to_crs("EPSG:4326")
    west, south, east, north = gdf.total_bounds
    dx, dy = grid.x[1] - grid.x[0], grid.y[1] - grid.y[0]
    # a cell's margin either side, so every cell a divide touches lies inside the window
    c0 = max(int(np.floor((west - grid.x[0]) / dx)) - 1, 0)
    c1 = min(int(np.ceil((east - grid.x[0]) / dx)) + 2, len(grid.x))
    s0 = max(int(np.floor((south - grid.y[0]) / dy)) - 1, 0)
    s1 = min(int(np.ceil((north - grid.y[0]) / dy)) + 2, len(grid.y))
    window = Grid(x=grid.x[c0:c1], y=grid.y[s0:s1])
    xmin, ymin, xmax, ymax = window.edges()
    source = NumPyRasterSource(
        np.zeros(window.shape, dtype=np.float32),
        srs_wkt=CRS("EPSG:4326").to_wkt(),
        xmin=xmin,
        ymin=ymin,
        xmax=xmax,
        ymax=ymax,
    )
    out = exact_extract(
        source, gdf, ["cell_id", "coverage"], include_cols=["divide_id"], output="pandas"
    )
    position = {d: k for k, d in enumerate(ids)}
    cells, catchments, coverage = [], [], []
    for divide_id, cell_ids, cov in zip(out["divide_id"], out["cell_id"], out["coverage"]):
        cells.append(np.asarray(cell_ids, dtype=np.int64))
        coverage.append(np.asarray(cov, dtype=np.float64))
        catchments.append(np.full(len(cells[-1]), position[divide_id], dtype=np.int64))
    # the field is north-first, so the window's rows count down from its northern edge
    n_rows = len(grid.y)
    return CatchmentWeights(
        n=len(ids),
        rows=slice(n_rows - s1, n_rows - s0),
        cols=slice(c0, c1),
        cell=np.concatenate(cells),
        catchment=np.concatenate(catchments),
        coverage=np.concatenate(coverage),
    )


def catchment_means(weights: CatchmentWeights, field: np.ndarray) -> np.ndarray:
    """The coverage-weighted mean of a north-first ``field`` over each catchment, NaN where any
    of its cells is."""
    values = np.ascontiguousarray(field[weights.rows, weights.cols]).ravel()[weights.cell]
    n = weights.n
    missing = np.bincount(weights.catchment, weights=np.isnan(values), minlength=n) > 0
    total = np.bincount(
        weights.catchment, weights=weights.coverage * np.nan_to_num(values), minlength=n
    )
    covered = np.bincount(weights.catchment, weights=weights.coverage, minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        means = total / covered
    means[missing] = np.nan
    return means


def storm_rain(
    hours: pd.DatetimeIndex, storm_steps: list[np.ndarray], divides, ids: list[str]
) -> dict[str, np.ndarray]:
    """Each product's rain in mm over each catchment at the storms' hours: catchments ×
    ``hours``, NaN wherever the product holds nothing."""
    steps = np.concatenate(storm_steps)
    days = sorted({hours[k].normalize() for k in steps})
    tasks = []
    for product in PRODUCTS:
        held: set[pd.Timestamp] = set()
        for day in days:
            listing = get(directory(product, day))
            held |= listed_hours(listing.decode(errors="replace") if listing else "", product)
        tasks += [(product, k) for k in steps if hours[k] in held]
    logging.info(f"Fetching {len(tasks)} hours of MRMS precipitation over {len(days)} days")

    weights: dict[tuple, CatchmentWeights] = {}
    lock = threading.Lock()

    def means(task: tuple[str, int]) -> np.ndarray | None:
        product, k = task
        raw = get(file_url(product, hours[k]))
        if raw is None or not raw.startswith(GZIP_MAGIC):
            # the archive serves a saved "404 Not Found" page, with HTTP 200, for some hours
            return None
        grid, field = decode(gzip.decompress(raw))
        key = (grid.x[0], grid.y[0], grid.shape)
        with lock:
            if key not in weights:
                weights[key] = catchment_weights(grid, divides, ids)
        return catchment_means(weights[key], field)

    rain = {product: np.full((len(ids), len(hours)), np.nan) for product in PRODUCTS}
    with ThreadPoolExecutor(WORKERS) as pool:
        for done, ((product, k), values) in enumerate(zip(tasks, pool.map(means, tasks)), 1):
            if values is not None:
                rain[product][:, k] = values
            if done % 500 == 0:
                logging.info(f"{done} of {len(tasks)} hours read")
    return rain


def storm_error(
    forcing_file: Path, geopackage: Path, peaks: Iterable[pd.Timestamp]
) -> pd.DataFrame:
    """Each MRMS product's departure from the forcing at the storms of the floods peaking at
    ``peaks`` (``storms.storm_departures``)."""
    import geopandas as gpd

    with xr.open_dataset(forcing_file) as ds:
        forcing = ds[[storms.PRECIP, "ids", "Time"]].load()
    hours = storms.forcing_hours(forcing)
    found = storms.storm_steps(hours, list(peaks))
    ids = [str(i) for i in forcing["ids"].values]
    rate = forcing[storms.PRECIP].transpose("catchment-id", "time").to_numpy()
    rain = rate.astype(float) * storms.SECONDS_PER_HOUR
    divides = gpd.read_file(geopackage, layer="divides")[["divide_id", "geometry"]]
    return storms.storm_departures(rain, storm_rain(hours, found, divides, ids), found, hours)
