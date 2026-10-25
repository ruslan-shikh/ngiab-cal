import numpy as np
import pandas as pd
import pytest
import xarray as xr

from ngiab_cal import storms

HOURS = pd.date_range("2020-01-01", periods=24 * 40, freq="h")
PEAK = HOURS[400]


def forcing(rain: np.ndarray) -> xr.Dataset:
    """An NGIAB NetCDF forcing of two catchments over HOURS with ``rain`` in mm per hour."""
    seconds = ((HOURS - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy()
    rate = (rain / 3600).astype(np.float32)
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


def rain() -> np.ndarray:
    rng = np.random.default_rng(1)
    r = rng.gamma(0.5, 2.0, size=(2, len(HOURS)))
    r[r < 1.0] = 0.0
    return r


def test_the_forcing_hours_are_read_from_its_time_variable():
    assert storms.forcing_hours(forcing(rain())).equals(HOURS)


def test_storms_that_overlap_are_one_and_a_peak_outside_the_forcing_is_refused():
    one = storms.storm_steps(HOURS, [HOURS[400], HOURS[500]])
    assert len(one) == 1 and one[0][0] == 400 - 168 and one[0][-1] == 500 + 168
    assert len(storms.storm_steps(HOURS, [HOURS[200], HOURS[600]])) == 2
    with pytest.raises(ValueError):
        storms.storm_steps(HOURS, [HOURS[-1] + pd.Timedelta(days=30)])


def test_concentration_holds_each_catchments_storm_total_and_keeps_dry_hours_dry():
    r = rain()
    steps = storms.storm_steps(HOURS, [PEAK])
    out = storms.change_storms(r, steps, depth=1.0, exponent=1.5)
    storm = steps[0]
    assert out[:, storm].sum(axis=1) == pytest.approx(r[:, storm].sum(axis=1))
    assert ((out[:, storm] == 0) == (r[:, storm] == 0)).all()
    # the heaviest hour takes a larger share
    assert (out[:, storm].max(axis=1) > r[:, storm].max(axis=1)).all()


def test_depth_scales_the_storm_and_leaves_every_other_hour():
    r = rain()
    steps = storms.storm_steps(HOURS, [PEAK])
    out = storms.change_storms(r, steps, depth=1.5, exponent=1.0)
    storm = steps[0]
    outside = np.setdiff1d(np.arange(len(HOURS)), storm)
    assert out[:, storm] == pytest.approx(1.5 * r[:, storm])
    assert (out[:, outside] == r[:, outside]).all()


def test_the_drier_and_wetter_forcings_change_only_the_storms_rain(tmp_path):
    source = tmp_path / "forcings.nc"
    forcing(rain()).to_netcdf(source)
    dry, wet = tmp_path / "storm_dry.nc", tmp_path / "storm_wet.nc"
    storms.write_storm_forcings(source, [PEAK], 1.6, 1.2, dry, wet)
    with xr.open_dataset(source) as a, xr.open_dataset(dry) as d, xr.open_dataset(wet) as w:
        storm = storms.storm_steps(HOURS, [PEAK])[0]
        total = a["precip_rate"][:, storm].sum("time")
        assert d["precip_rate"][:, storm].sum("time").values == pytest.approx(total / 1.6, rel=1e-5)
        assert w["precip_rate"][:, storm].sum("time").values == pytest.approx(total * 1.6, rel=1e-5)
        for changed in (d, w):
            assert changed["precip_rate"].dtype == np.float32
            assert changed["APCP_surface"].values == pytest.approx(
                changed["precip_rate"].values * 3600, rel=1e-6
            )
            assert changed["TMP_2maboveground"].equals(a["TMP_2maboveground"])
            assert changed["Time"].equals(a["Time"]) and changed["ids"].equals(a["ids"])
        # the wetter storm's heaviest hour is packed beyond the depth change alone
        assert (
            w["precip_rate"][:, storm].max("time") > 1.6 * a["precip_rate"][:, storm].max("time")
        ).all()
