import sqlite3
from datetime import datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
import yaml

from ngiab_cal import __main__ as cli
from ngiab_cal.arguments import check_objective_arguments, get_arg_parser
from ngiab_cal.file_paths import FilePaths

START, END = datetime(2020, 1, 1), datetime(2020, 2, 9, 23)
CALIBRATION_PERIOD = (datetime(2020, 1, 11), datetime(2020, 1, 26))
GAGE = "01234567"


@pytest.fixture
def files(tmp_path) -> FilePaths:
    """Create a package whose hydrofabric places the gage on a 1 mi2 flowpath."""
    (tmp_path / "config").mkdir()
    (tmp_path / "calibration").mkdir()
    with sqlite3.connect(tmp_path / "config" / f"{tmp_path.name}_subset.gpkg") as con:
        con.execute("CREATE TABLE flowpaths (id TEXT, tot_drainage_areasqkm REAL)")
        con.execute("CREATE TABLE 'flowpath-attributes' (id TEXT, gage TEXT)")
        con.execute("INSERT INTO flowpaths VALUES ('wb-1', 2.589988110336)")
        con.execute(f"INSERT INTO 'flowpath-attributes' VALUES ('wb-1', '{GAGE}')")
    return FilePaths(tmp_path)


def observed() -> pd.DataFrame:
    """Return 15-minute flows with one flood before, one in and one after the calibration period."""
    times = pd.date_range(START, END, freq="15min")
    flow = np.ones(len(times))
    for peak in ("2020-01-05 06:00", "2020-01-18 12:00", "2020-02-03 00:00"):
        hours = np.abs((times - pd.Timestamp(peak)) / pd.Timedelta(hours=1))
        flow = np.maximum(flow, 40.0 * np.clip(1 - hours / 24, 0, None))
    return pd.DataFrame({"value_date": times, "obs_flow": flow, "estimated": False})


def write_floods(files: FilePaths, period=CALIBRATION_PERIOD) -> str:
    # a 2-year flood of 353 ft3/s is 10 m3/s, below all three floods
    return cli.write_flood_objective_files(
        files, GAGE, observed(), period=period, flood_weight=0.25, q2_cfs=353.0
    )


def eval_params(files: FilePaths, objective: str = "kge", flood_settings: str = "") -> dict:
    cli.write_ngen_cal_config(
        files, GAGE, START, END, 10, 10, CALIBRATION_PERIOD, objective, flood_settings
    )
    return yaml.safe_load(files.calibration_config.read_text())["model"]["eval_params"]


def test_the_default_config_has_no_flood_settings(files):
    params = eval_params(files)
    assert params["objective"] == "kge"
    assert "floods" not in params and "flood_weight" not in params


def test_kge_flood_writes_the_flood_list_and_points_ngen_cal_at_it(files, caplog):
    params = eval_params(files, "kge-flood", write_floods(files))
    assert "Fewer than 3 floods in the calibration period" in caplog.text
    listed = pd.read_csv(files.floods)
    assert listed["peak_time"].tolist() == [
        "2020-01-05 06:00:00",
        "2020-01-18 12:00:00",
        "2020-02-03 00:00:00",
    ]
    assert params["objective"] == "kge_flood"
    assert params["floods"] == "/ngen/ngen/data/calibration/floods.csv"
    assert params["flood_weight"] == 0.25


def test_kge_flood_stops_if_the_calibration_period_has_no_flood(files):
    with pytest.raises(ValueError, match="calibration period"):
        write_floods(files, period=(datetime(2020, 1, 20), datetime(2020, 1, 30)))


def test_estimated_values_are_flagged_but_not_written(tmp_path, monkeypatch):
    usgs = pd.DataFrame(
        {
            "value_time": pd.date_range(START, periods=3, freq="h"),
            "value": [10.0, 20.0, 30.0],
            "qualifiers": pd.Categorical(["['A']", "['A', 'e']", "['P', 'Ice']"]),
        }
    )
    service = MagicMock()
    service.return_value.__enter__.return_value.get.return_value = usgs
    monkeypatch.setattr(cli, "IVDataService", service)
    data = cli.write_usgs_data_to_csv(START, END, GAGE, tmp_path / "obs.csv")
    assert data["estimated"].tolist() == [False, True, False]
    assert pd.read_csv(tmp_path / "obs.csv").columns.tolist() == ["value_date", "obs_flow"]


def test_flood_arguments_are_checked():
    parser = get_arg_parser()

    def check(*argv):
        check_objective_arguments(parser, parser.parse_args(["folder", *argv]))

    check("--objective", "kge-flood", "--flood-weight", "0.3", "--q2-cfs", "300")
    for wrong in (
        ["--objective", "kge-flood", "--flood-weight", "1.5"],
        ["--objective", "kge-flood", "--q2-cfs", "0"],
        ["--flood-weight", "0.3"],  # only kge-flood uses it
    ):
        with pytest.raises(SystemExit):
            check(*wrong)
