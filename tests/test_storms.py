import netCDF4
import numpy as np
import pandas as pd
import pytest
import xarray as xr

from ngiab_cal import storms


def test_forcing_hours_are_read_from_time(forcing_file, hours):
    with netCDF4.Dataset(forcing_file) as forcing:
        assert storms.forcing_hours(forcing).equals(hours)


def test_overlapping_storms_join_and_a_peak_outside_is_refused(hours):
    one = storms.storm_steps(hours, [hours[400], hours[500]])
    assert len(one) == 1 and one[0][0] == 400 - 168 and one[0][-1] == 500 + 168
    assert len(storms.storm_steps(hours, [hours[200], hours[600]])) == 2
    with pytest.raises(ValueError):
        storms.storm_steps(hours, [hours[-1] + pd.Timedelta(days=30)])


def test_concentration_keeps_storm_totals_and_dry_hours(forcing, hours):
    storm = storms.storm_steps(hours, [hours[400]])[0]
    rain = forcing["precip_rate"].values[:, storm].astype(float)
    out = storms.change_storm(rain, depth=1.0, exponent=1.5)
    assert out.sum(axis=1) == pytest.approx(rain.sum(axis=1))
    assert ((out == 0) == (rain == 0)).all()
    # the heaviest hour takes a larger share
    assert (out.max(axis=1) > rain.max(axis=1)).all()


def test_drier_and_wetter_forcings_change_only_storm_rain(tmp_path, forcing_file, hours):
    dry, wet = tmp_path / "storm_dry.nc", tmp_path / "storm_wet.nc"
    changes = [(1 / 1.6, 1 / 1.2), (1.6, 1.2)]
    storms.write_changed_forcings(forcing_file, [hours[400]], changes, [dry, wet])
    storm = storms.storm_steps(hours, [hours[400]])[0]
    outside = np.setdiff1d(np.arange(len(hours)), storm)
    with xr.open_dataset(forcing_file) as a, xr.open_dataset(dry) as d, xr.open_dataset(wet) as w:
        total = a["precip_rate"][:, storm].sum("time")
        assert d["precip_rate"][:, storm].sum("time").values == pytest.approx(total / 1.6, rel=1e-5)
        assert w["precip_rate"][:, storm].sum("time").values == pytest.approx(total * 1.6, rel=1e-5)
        for changed in (d, w):
            assert changed["precip_rate"].dtype == np.float32
            assert changed["precip_rate"][:, outside].equals(a["precip_rate"][:, outside])
            assert changed["APCP_surface"].values == pytest.approx(
                changed["precip_rate"].values * 3600, rel=1e-6
            )
            for name in ("TMP_2maboveground", "Time", "ids"):
                assert changed[name].equals(a[name])
        # the wetter storm's heaviest hour is packed beyond the depth change alone
        assert (
            w["precip_rate"][:, storm].max("time") > 1.6 * a["precip_rate"][:, storm].max("time")
        ).all()


def test_the_stress_test_steps_depth_in_log_and_crosses_concentration():
    changes = storms.stress_changes(1.6, 1.2)
    assert len(changes) == 26
    # ten equal steps of ln f to each end, without the unchanged storm
    sweep = [f for f, g in changes if g == 1.0]
    steps = [k / 10 for k in range(-10, 11) if k]
    assert np.log(sweep) == pytest.approx(np.log(1.6) * np.array(steps))
    assert np.array([c for c in changes if c[1] != 1.0]) == pytest.approx(
        np.array([(f, g) for f in (1.0, 1 / 1.6, 1.6) for g in (1 / 1.2, 1.2)])
    )
    # without a depth or concentration change, its runs are left out rather than repeated
    assert storms.stress_changes(1.6, 1.0) == [c for c in changes if c[1] == 1.0]
    assert np.array(storms.stress_changes(1.0, 1.2)) == pytest.approx(
        np.array([(1.0, 1 / 1.2), (1.0, 1.2)])
    )
