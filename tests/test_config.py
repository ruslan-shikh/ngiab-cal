import json
import sqlite3
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
import xarray as xr
import yaml

from ngiab_cal import __main__ as cli
from ngiab_cal.arguments import check_objective_arguments, get_arg_parser, stress_ends
from ngiab_cal.file_paths import FilePaths
from test_storms import HOURS, forcing, rain

START, END = datetime(2020, 1, 1), datetime(2020, 2, 9, 23)
PERIOD = (datetime(2020, 1, 11), datetime(2020, 1, 26))


def package(tmp_path) -> FilePaths:
    """A run package with a hydrofabric holding the gage, a NetCDF forcing over HOURS and a
    calibration folder."""
    (tmp_path / "config").mkdir()
    (tmp_path / "forcings").mkdir()
    (tmp_path / "calibration").mkdir()
    (tmp_path / "config" / "realization.json").write_text(
        json.dumps(
            {"global": {"forcing": {"path": "./forcings/forcings.nc", "provider": "NetCDF"}}}
        )
    )
    with sqlite3.connect(tmp_path / "config" / f"{tmp_path.name}_subset.gpkg") as con:
        con.execute("CREATE TABLE flowpaths (id TEXT, tot_drainage_areasqkm REAL)")
        con.execute("CREATE TABLE 'flowpath-attributes' (id TEXT, gage TEXT)")
        con.execute("INSERT INTO flowpaths VALUES ('wb-1', 2.589988110336)")  # 1 mi²
        con.execute("INSERT INTO 'flowpath-attributes' VALUES ('wb-1', '01234567')")
    forcing(rain()).to_netcdf(tmp_path / "forcings" / "forcings.nc")
    return FilePaths(tmp_path)


def observed() -> pd.DataFrame:
    """Floods peaking before, inside and after the calibration period, at every 15 minutes."""
    times = pd.date_range(HOURS[0], HOURS[-1], freq="15min")
    flow = np.ones(len(times))
    for peak in ("2020-01-05 06:00", "2020-01-18 12:00", "2020-02-03 00:00"):
        hours = np.abs((times - pd.Timestamp(peak)) / pd.Timedelta(hours=1))
        flow = np.maximum(flow, 40.0 * np.clip(1 - hours / 24, 0, None))
    return pd.DataFrame({"value_date": times, "obs_flow": flow, "estimated": False})


def config(files: FilePaths, objective: str, params: str = "") -> dict:
    cli.write_ngen_cal_config(files, "01234567", START, END, 10, 10, 0.4, objective, params)
    return yaml.safe_load(files.calibration_config.read_text())["model"]["eval_params"]


def test_the_default_config_scores_kge_without_objective_params(tmp_path):
    eval_params = config(package(tmp_path), "kge")
    assert eval_params["objective"] == "kge" and "objective_params" not in eval_params


def test_a_robust_objective_lists_every_flood_and_changes_the_calibration_storms(tmp_path):
    files = package(tmp_path)
    params = cli.write_flood_objective_files(
        files, "01234567", observed(), PERIOD, "kge-flood-robust", 0.25, 353.0, 1.6, 1.2
    )
    # the 2-year flood of 353 ft³/s is 10 m³/s, so all three floods count
    listed = pd.read_csv(files.floods, parse_dates=["peak_time"])
    assert listed["peak_time"].astype(str).tolist() == [
        "2020-01-05 06:00:00",
        "2020-01-18 12:00:00",
        "2020-02-03 00:00:00",
    ]
    eval_params = config(files, "kge-flood-robust", params)
    assert eval_params["objective"] == "kge_flood_robust"
    assert eval_params["objective_params"] == {
        "floods": "/ngen/ngen/data/calibration/floods.csv",
        "flood_weight": 0.25,
        "storm_forcings": [
            "/ngen/ngen/data/calibration/storm_dry.nc",
            "/ngen/ngen/data/calibration/storm_wet.nc",
        ],
    }
    # only the calibration period's flood has its storm changed
    with xr.open_dataset(cli.netcdf_forcing(files)) as a, xr.open_dataset(files.drier_forcing) as d:
        changed = (a["precip_rate"] != d["precip_rate"]).any("catchment-id").values
    storm = HOURS[changed]
    assert storm.min() >= pd.Timestamp("2020-01-11 12:00")
    assert storm.max() <= pd.Timestamp("2020-01-25 12:00")


def test_a_flood_objective_writes_no_forcing(tmp_path):
    files = package(tmp_path)
    params = cli.write_flood_objective_files(
        files, "01234567", observed(), PERIOD, "kge-flood-mean", 0.5, 353.0, None, None
    )
    assert "storm_forcings" not in params
    assert not files.drier_forcing.exists() and not files.wetter_forcing.exists()


def test_a_calibration_period_without_a_flood_is_refused(tmp_path):
    with pytest.raises(ValueError, match="No flood"):
        cli.write_flood_objective_files(
            package(tmp_path),
            "01234567",
            observed(),
            (datetime(2020, 1, 20), datetime(2020, 1, 30)),
            "kge-flood-mean",
            0.5,
            353.0,
            None,
            None,
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["--objective", "kge-robust"],
        ["--objective", "kge-flood-robust", "--storm-depth", "1.5"],
        ["--storm-depth", "1.5", "--storm-concentration", "1.2"],
        ["--objective", "kge-robust", "--storm-depth", "0.8", "--storm-concentration", "1.2"],
        ["--objective", "kge-flood-mean", "--flood-weight", "1.5"],
    ],
)
def test_settings_that_do_not_fit_the_objective_are_refused(argv):
    parser = get_arg_parser()
    with pytest.raises(SystemExit):
        check_objective_arguments(parser, parser.parse_args(["folder", *argv]))


def test_a_robust_objective_with_its_storm_settings_is_accepted():
    parser = get_arg_parser()
    argv = [
        "folder",
        "--objective",
        "kge-robust",
        "--storm-depth",
        "1.5",
        "--storm-concentration",
        "1.0",
    ]
    check_objective_arguments(parser, parser.parse_args(argv))


VALIDATION = (PERIOD[1], END)


def test_the_stress_test_changes_only_the_validation_floods_storms(tmp_path):
    files = package(tmp_path)
    params = cli.write_flood_objective_files(
        files, "01234567", observed(), PERIOD, "kge", 0.5, 353.0, None, None, (1.6, 1.2), VALIDATION
    )
    eval_params = config(files, "kge", params)
    assert eval_params["objective"] == "kge"
    assert eval_params["objective_params"] == {
        "floods": "/ngen/ngen/data/calibration/floods.csv",
        "flood_weight": 0.5,
        "stress_test": "/ngen/ngen/data/calibration/stress/changes.csv",
    }
    changes = pd.read_csv(files.stress_changes)
    assert len(changes) == 26
    assert changes["forcing"].iloc[0] == "/ngen/ngen/data/calibration/stress/stress_01.nc"
    assert len(list(files.stress_folder.glob("stress_*.nc"))) == 26
    # only the validation period's flood, peaking on 2020-02-03, has its storm changed
    with (
        xr.open_dataset(cli.netcdf_forcing(files)) as a,
        xr.open_dataset(files.stress_folder / "stress_20.nc") as s,
    ):
        changed = (a["precip_rate"] != s["precip_rate"]).any("catchment-id").values
    assert HOURS[changed].min() >= pd.Timestamp("2020-01-27 00:00")
    assert not files.drier_forcing.exists()


def test_a_kge_calibration_under_the_stress_test_needs_no_calibration_flood(tmp_path):
    files = package(tmp_path)
    no_flood = (datetime(2020, 1, 20), datetime(2020, 1, 30))
    params = cli.write_flood_objective_files(
        files,
        "01234567",
        observed(),
        no_flood,
        "kge",
        0.5,
        353.0,
        None,
        None,
        (1.6, 1.2),
        (no_flood[1], END),
    )
    assert "stress_test" in params


def test_the_stress_test_without_a_validation_flood_is_refused(tmp_path):
    with pytest.raises(ValueError, match="validation period"):
        cli.write_flood_objective_files(
            package(tmp_path),
            "01234567",
            observed(),
            PERIOD,
            "kge-flood-mean",
            0.5,
            353.0,
            None,
            None,
            (1.6, 1.2),
            (datetime(2020, 1, 26), datetime(2020, 1, 30)),
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["--stress-depth", "1.5", "--stress-concentration", "1.2"],
        ["--stress-test"],
        ["--stress-test", "--stress-depth", "1.5"],
        ["--stress-test", "--stress-depth", "1.5", "--stress-concentration", "0.9"],
        ["--stress-test", "--stress-depth", "1", "--stress-concentration", "1"],
        ["--stress-test", "--stress-depth", "1.5", "--stress-concentration", "1.2", "--cr", "1"],
    ],
)
def test_stress_settings_that_do_not_fit_are_refused(argv):
    parser = get_arg_parser()
    with pytest.raises(SystemExit):
        check_objective_arguments(parser, parser.parse_args(["folder", *argv]))


def test_the_stress_test_takes_its_own_ends_or_else_the_robust_objectives():
    parser = get_arg_parser()
    robust = [
        "folder",
        "--objective",
        "kge-robust",
        "--storm-depth",
        "1.5",
        "--storm-concentration",
        "1.1",
        "--stress-test",
    ]
    args = parser.parse_args(robust)
    check_objective_arguments(parser, args)
    assert stress_ends(args) == (1.5, 1.1)
    args = parser.parse_args([*robust, "--stress-depth", "2.0"])
    check_objective_arguments(parser, args)
    assert stress_ends(args) == (2.0, 1.1)
    assert stress_ends(parser.parse_args(["folder"])) is None
