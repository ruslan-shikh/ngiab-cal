import gzip

import numpy as np
import pandas as pd
import pytest

from ngiab_cal import storm_error

gpd = pytest.importorskip("geopandas")
pytest.importorskip("exactextract")
from shapely.geometry import box  # noqa: E402

# A 0.01° grid whose cell edges fall on whole hundredths
GRID = storm_error.Grid(x=-100.0 + 0.01 * np.arange(50), y=40.0 + 0.01 * np.arange(40))


def edge(axis: np.ndarray, k: float) -> float:
    """The edge k cells from the first cell's outer edge."""
    return float(axis[0] - 0.005 + 0.01 * k)


def divides() -> "gpd.GeoDataFrame":
    """cat-1 covers four whole cells; cat-2 one whole cell and half the next to the east."""
    shapes = {
        "cat-1": box(edge(GRID.x, 10), edge(GRID.y, 5), edge(GRID.x, 12), edge(GRID.y, 7)),
        "cat-2": box(edge(GRID.x, 20), edge(GRID.y, 10), edge(GRID.x, 21.5), edge(GRID.y, 11)),
    }
    return gpd.GeoDataFrame(
        {"divide_id": list(shapes)}, geometry=list(shapes.values()), crs="EPSG:4326"
    )


def field(value=lambda col, row: col + 100.0 * row) -> np.ndarray:
    """A north-first field of ``value`` at each column and south-first row."""
    cols, rows = np.meshgrid(np.arange(len(GRID.x)), np.arange(len(GRID.y)))
    return value(cols, rows).astype(float)[::-1]


def test_each_catchment_averages_its_cells_by_the_fraction_of_each_it_covers():
    weights = storm_error.catchment_weights(GRID, divides(), ["cat-2", "cat-1"])
    means = storm_error.catchment_means(weights, field())
    cat1 = np.mean([c + 100.0 * r for c in (10, 11) for r in (5, 6)])
    cat2 = (20 + 1000.0 + 0.5 * (21 + 1000.0)) / 1.5
    assert means == pytest.approx([cat2, cat1])


def test_a_missing_cell_leaves_only_the_catchment_it_lies_in_missing():
    weights = storm_error.catchment_weights(GRID, divides(), ["cat-1", "cat-2"])
    values = field()
    values[len(GRID.y) - 1 - 10, 21] = np.nan
    means = storm_error.catchment_means(weights, values)
    assert np.isfinite(means[0]) and np.isnan(means[1])


def test_the_hours_an_archive_listing_holds_are_read_from_its_file_names():
    html = (
        '<a href="RadarOnly_QPE_01H_00.00_20170110-230000.grib2.gz">x</a>'
        '<a href="RadarOnly_QPE_01H_00.00_20170111-000000.grib2.gz">x</a>'
        '<a href="GaugeCorr_QPE_01H_00.00_20170110-220000.grib2.gz">x</a>'
    )
    assert storm_error.listed_hours(html, "RadarOnly_QPE_01H") == {
        pd.Timestamp("2017-01-10 23:00"),
        pd.Timestamp("2017-01-11 00:00"),
    }


def test_storm_rain_reads_each_hour_a_product_holds_and_leaves_the_rest_missing(monkeypatch):
    hours = pd.date_range("2017-01-10 20:00", periods=6, freq="h")
    held = {"GaugeCorr_QPE_01H": hours[:4], "RadarOnly_QPE_01H": hours[2:]}
    # the archive serves a saved 404 page, with HTTP 200, for one hour it lists
    broken = ("RadarOnly_QPE_01H", hours[5])

    def get(url):
        for product in storm_error.PRODUCTS:
            for day in hours.normalize().unique():
                if url == storm_error.directory(product, day):
                    names = [
                        storm_error.file_url(product, h).rsplit("/", 1)[1]
                        for h in held.get(product, [])
                        if h.normalize() == day
                    ]
                    return " ".join(names).encode() or None
            for h in held.get(product, []):
                if url == storm_error.file_url(product, h):
                    if (product, h) == broken:
                        return b"<html>404 Not Found</html>"
                    return gzip.compress(f"{product}|{h:%H}".encode())
        raise AssertionError(url)

    def decode(raw):
        product, hour = raw.decode().split("|")
        scale = 2.0 if product.startswith("Radar") else 1.0
        return GRID, field(lambda col, row: np.full(col.shape, scale * int(hour)))

    monkeypatch.setattr(storm_error, "get", get)
    monkeypatch.setattr(storm_error, "decode", decode)
    rain = storm_error.storm_rain(hours, [np.arange(6)], divides(), ["cat-1", "cat-2"])
    expected = np.array([20, 21, 22, 23, np.nan, np.nan])
    assert rain["GaugeCorr_QPE_01H"][0] == pytest.approx(expected, nan_ok=True)
    radar = np.array([np.nan, np.nan, 44, 46, 0, np.nan])
    assert rain["RadarOnly_QPE_01H"][1] == pytest.approx(radar, nan_ok=True)
    assert np.isnan(rain["MultiSensor_QPE_01H_Pass2"]).all()
