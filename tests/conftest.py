import numpy as np
import pandas as pd
import pytest
import xarray as xr


@pytest.fixture
def hours() -> pd.DatetimeIndex:
    return pd.date_range("2020-01-01", periods=24 * 40, freq="h")


@pytest.fixture
def forcing(hours) -> xr.Dataset:
    """Create an NGIAB NetCDF forcing of two catchments over ``hours`` with random rain."""
    rain = np.random.default_rng(1).gamma(0.5, 2.0, size=(2, len(hours)))  # mm per hour
    rain[rain < 1.0] = 0.0
    rate = (rain / 3600).astype(np.float32)
    seconds = ((hours - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy()
    dims = ("catchment-id", "time")
    return xr.Dataset(
        {
            "precip_rate": (dims, rate),
            "APCP_surface": (dims, rate * np.float32(3600)),
            "TMP_2maboveground": (dims, np.full(rain.shape, 280.0, dtype=np.float32)),
            "ids": (("catchment-id",), np.array(["cat-1", "cat-2"])),
            "Time": (
                dims,
                np.tile(seconds.astype(np.int32), (2, 1)),
                {"units": "s", "epoch_start": "01/01/1970 00:00:00"},
            ),
        }
    )


@pytest.fixture
def forcing_file(tmp_path, forcing):
    path = tmp_path / "forcings" / "forcings.nc"
    path.parent.mkdir()
    forcing.to_netcdf(path)
    return path
