import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from aiohttp.client_exceptions import ContentTypeError
from hydrotools.nwis_client import IVDataService
from rich.prompt import Prompt

from ngiab_cal import floods, storms
from ngiab_cal.arguments import (
    CALIBRATION_VALIDATION_RATIO,
    FLOOD_WEIGHT_DEFAULT,
    ITERATIONS_DEFAULT,
    OBJECTIVES,
    ROBUST_OBJECTIVES,
    WARMUP_DEFAULT,
    check_objective_arguments,
    get_arg_parser,
    stress_ends,
)
from ngiab_cal.custom_logging import set_log_level, setup_logging
from ngiab_cal.file_paths import FilePaths, validate_calibration_files, validate_run_files

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DOCKER_IMAGE_NAME = "awiciroh/ngiab-cal:latest"
# the calibration folder as the container sees it
CONTAINER_CALIBRATION_FOLDER = "/ngen/ngen/data/calibration"


# hide IVDataService warning so we can show our own
warnings.filterwarnings("ignore", message="No data was returned by the request.")


def create_crosswalk_json(hydrofabric: Path, gage_id: str, output_file: Path) -> None:
    """Create a crosswalk JSON file for a given gage ID."""
    with sqlite3.connect(hydrofabric) as con:
        sql_query = f"SELECT id FROM 'flowpath-attributes' WHERE gage = '{gage_id}'"
        result = con.execute(sql_query).fetchone()
        wb_id = result[0]
        cat_id = wb_id.replace("wb", "cat")

    data = {cat_id: {"Gage_no": gage_id}}
    with open(output_file, "w") as f:
        f.write(json.dumps(data))


def copy_and_convert_paths_to_absolute(source_file: Path, dest_file: Path) -> None:
    # a bit dodgy but removeable once ngiab-cal is updated
    with open(source_file, "r") as f:
        with open(dest_file, "w") as out:
            for line in f:
                line = line.replace("./", "/ngen/ngen/data/")
                line = line.replace("/ngen/ngen/data/outputs/ngen/", ".")
                line = line.replace("/ngen/ngen/data/outputs/ngen", ".")
                line = line.replace("outputs/troute/", ".")
                # ngiab-cal takes troute yaml as an input but doesn't replace this value
                line = line.replace(
                    "/ngen/ngen/data/config/troute.yaml", "/ngen/ngen/data/calibration/troute.yaml"
                )
                if "lakeout_output" in line:
                    continue
                if "lite_restart" in line:
                    continue
                out.write(line)


def get_start_end_times(realization_path: Path) -> tuple[datetime, datetime]:
    """Get the start and end times from a realization file."""
    with open(realization_path, "r") as f:
        realization = json.loads(f.read())
    start = realization["time"]["start_time"]
    end = realization["time"]["end_time"]

    start = datetime.strptime(start, TIME_FORMAT)
    end = datetime.strptime(end, TIME_FORMAT)
    total_range = end - start
    # 2 year minimum suggested to allow for a 12 month warm up
    if total_range.days < 730:
        logging.warning(
            "Time range is less than 2 years, this may not be enough data for calibration"
        )
    return start, end


def write_usgs_data_to_csv(
    start: datetime, end: datetime, gage_id: str, output_file: Path
) -> pd.DataFrame:
    """Downloads the usgs observed data to csv for a given gage and time range, and returns it
    with whether USGS qualifies each value as estimated"""
    logging.info(f"Downloading USGS data for {gage_id} between {start} and {end}")
    data = pd.DataFrame()
    try:
        with IVDataService(cache_filename=FilePaths.hydrotools_cache) as service:
            data = service.get(sites=gage_id, startDT=start, endDT=end)
    except ContentTypeError:
        pass

    if data.empty:
        raise ValueError(f"Unable to find usgs observation for {gage_id} between {start} and {end}")

    data["estimated"] = data["qualifiers"].astype(str).str.contains("'e'", regex=False)
    data = data.filter(["value_time", "value", "estimated"])
    data.columns = ["value_date", "obs_flow", "estimated"]
    data["obs_flow"] = data["obs_flow"].astype(float)
    data["obs_flow"] = data["obs_flow"] * floods.CMS_PER_CFS
    data[["value_date", "obs_flow"]].to_csv(output_file, index=False)
    return data


def calibration_period(
    start: datetime, end: datetime, warm_up: int, calibration_ratio: float
) -> tuple[datetime, datetime]:
    """The start and end of the period the calibration is scored over"""
    total_range = abs(start - end)
    # warm up is half the range, capped at 365 days
    warm_up_period = timedelta(days=warm_up)
    if warm_up_period > total_range:
        logging.error(
            f"Warm up period {warm_up_period} days is longer that the total range to be simulated {total_range} "
        )
    # Validation not currently working so just set the values the same as eval
    # round to the nearest day so we don't get strange intervals ngen isn't expecting
    calibration_days = ((total_range - warm_up_period) * calibration_ratio).days
    calibration_td = timedelta(days=calibration_days)
    evaluation_start = start + warm_up_period
    # ends after half the remaining time
    if calibration_ratio == 0:
        evaluation_end = end
    else:
        evaluation_end = evaluation_start + calibration_td
    return evaluation_start, evaluation_end


def write_ngen_cal_config(
    data_folder: FilePaths,
    gage_id: str,
    start: datetime,
    end: datetime,
    iterations: int,
    warm_up: int,
    calibration_ratio: float,
    objective: str = "kge",
    objective_params: str = "",
) -> None:
    logging.info("Writing ngiab-cal configuration")
    total_range = abs(start - end)
    evaluation_start, evaluation_end = calibration_period(start, end, warm_up, calibration_ratio)
    calibration_days = (evaluation_end - evaluation_start).days
    # validation starts at the end of the evaluation period
    validation_start = evaluation_end
    validation_end = end

    # debug all the times
    logging.debug("start {}".format(start))
    logging.debug("end {}".format(end))
    logging.debug("Total range: {}".format(total_range))
    logging.debug("Warm up: {}".format(warm_up))
    logging.debug("Calibration Days: {}".format(calibration_days))
    logging.debug("Validation Days: {}".format(abs(validation_start - validation_end).days))
    logging.debug("Evaluation start: {}".format(evaluation_start))
    logging.debug("Evaluation end: {}".format(evaluation_end))
    logging.debug("Validation start: {}".format(validation_start))
    logging.debug("Validation end: {}".format(validation_end))

    plot_frequency = min(50, iterations)

    with open(FilePaths.template_ngiab_cal_conf, "r") as f:
        template = f.read()

    with open(data_folder.calibration_config, "w") as file:
        file.write(
            template.format(
                iterations=iterations,
                plot_frequency=plot_frequency,
                subset_hydrofabric=data_folder.geopackage_path.name,
                evaluation_start=evaluation_start.strftime(TIME_FORMAT),
                evaluation_stop=evaluation_end.strftime(TIME_FORMAT),
                valid_start_time=start.strftime(TIME_FORMAT),
                valid_end_time=end.strftime(TIME_FORMAT),
                valid_eval_start_time=validation_start.strftime(TIME_FORMAT),
                valid_eval_end_time=validation_end.strftime(TIME_FORMAT),
                full_eval_start_time=start.strftime(TIME_FORMAT),
                full_eval_end_time=end.strftime(TIME_FORMAT),
                gage_id=gage_id,
                objective=OBJECTIVES[objective],
                objective_params=objective_params,
            )
        )


def gage_drainage_area_km2(hydrofabric: Path, gage_id: str) -> float:
    """The drainage area of the gage's flowpath in the hydrofabric"""
    with sqlite3.connect(hydrofabric) as con:
        sql_query = (
            "SELECT f.tot_drainage_areasqkm FROM flowpaths f "
            "JOIN 'flowpath-attributes' a ON a.id = f.id WHERE a.gage = ?"
        )
        return float(con.execute(sql_query, (gage_id,)).fetchone()[0])


def netcdf_forcing(files: FilePaths) -> Path:
    """The NetCDF forcing file the realization reads"""
    with open(files.template_realization) as f:
        forcing = json.load(f)["global"]["forcing"]
    if forcing.get("provider") != "NetCDF":
        raise ValueError(
            f"the robust objectives change NetCDF forcing, and this realization's forcing "
            f"provider is {forcing.get('provider')}"
        )
    return (files.data_folder / forcing["path"]).resolve()


def container_path(files: FilePaths, path: Path) -> str:
    """A file of the calibration folder as the container sees it"""
    return f"{CONTAINER_CALIBRATION_FOLDER}/{path.relative_to(files.calibration_folder).as_posix()}"


def objective_params_yaml(
    flood_weight: float, storm_forcings: list[Path], stress_changes: str | None = None
) -> str:
    """The objective_params block of the ngen-cal config, under eval_params"""
    text = (
        f"\n    objective_params:"
        f"\n      floods: {CONTAINER_CALIBRATION_FOLDER}/floods.csv"
        f"\n      flood_weight: {flood_weight}"
    )
    if storm_forcings:
        text += "\n      storm_forcings:"
        for path in storm_forcings:
            text += f"\n        - {CONTAINER_CALIBRATION_FOLDER}/{path.name}"
    if stress_changes:
        text += f"\n      stress_test: {stress_changes}"
    return text


def write_flood_objective_files(
    files: FilePaths,
    gage_id: str,
    observed: pd.DataFrame,
    period: tuple[datetime, datetime],
    objective: str,
    flood_weight: float,
    q2_cfs: float | None,
    storm_depth: float | None,
    storm_concentration: float | None,
    stress: tuple[float, float] | None = None,
    validation_period: tuple[datetime, datetime] | None = None,
) -> str:
    """Write the flood list, the robust objectives' drier and wetter forcings, and with ``stress``,
    the stress test's d and g, its changed forcings of the floods of ``validation_period``;
    returns the config's objective_params block"""
    if q2_cfs is None:
        logging.info(f"Downloading the annual peaks of {gage_id}")
        q2_cfs = floods.two_year_flood_cfs(floods.download_annual_peaks(gage_id))
        logging.info(f"2-year flood {q2_cfs:.0f} ft3/s, the median of the annual peaks")
    area_mi2 = gage_drainage_area_km2(files.geopackage_path, gage_id) / floods.KM2_PER_MI2
    flow = observed.set_index("value_date")
    found = floods.find_floods(
        flow["obs_flow"], flow["estimated"], q2_cfs * floods.CMS_PER_CFS, area_mi2
    )
    peaks = found["peak_time"]
    in_period = found[(peaks >= period[0]) & (peaks <= period[1])]
    if objective != "kge" and in_period.empty:
        raise ValueError(
            f"No flood above the 2-year flood ({q2_cfs:.0f} ft3/s) is scored in the calibration "
            f"period {period[0]} to {period[1]}"
        )
    logging.info(f"{len(in_period)} floods in the calibration period, {len(found)} in the record")
    found.to_csv(files.floods, index=False, date_format=TIME_FORMAT)

    storm_forcings = []
    if objective in ROBUST_OBJECTIVES:
        logging.info("Writing the drier and wetter forcings")
        storm_forcings = [files.drier_forcing, files.wetter_forcing]
        storms.write_storm_forcings(
            netcdf_forcing(files),
            in_period["peak_time"],
            storm_depth,
            storm_concentration,
            *storm_forcings,
        )

    stress_changes = None
    if stress:
        held_out = found[(peaks >= validation_period[0]) & (peaks <= validation_period[1])]
        if held_out.empty:
            raise ValueError(
                f"No flood above the 2-year flood ({q2_cfs:.0f} ft3/s) is scored in the validation "
                f"period {validation_period[0]} to {validation_period[1]}, so the stress test has "
                f"none to read"
            )
        logging.info(f"Writing the stress test's forcings, changing {len(held_out)} floods' storms")
        shutil.rmtree(files.stress_folder, ignore_errors=True)
        files.stress_folder.mkdir()
        changes = storms.write_stress_forcings(
            netcdf_forcing(files), held_out["peak_time"], *stress, files.stress_folder
        )
        changes["forcing"] = [
            container_path(files, files.stress_folder / f) for f in changes["forcing"]
        ]
        changes.to_csv(files.stress_changes, index=False)
        stress_changes = container_path(files, files.stress_changes)
    return objective_params_yaml(flood_weight, storm_forcings, stress_changes)


def get_gages_from_hydrofabric(hydrofabric: Path) -> list[str]:
    with sqlite3.connect(hydrofabric) as conn:
        sql = "select gage from 'flowpath-attributes' where gage is not NULL"
        return [row[0] for row in conn.execute(sql).fetchall()]


def pick_gage_to_calibrate(hydrofabric: Path) -> str:
    gages = get_gages_from_hydrofabric(hydrofabric)
    if len(gages) == 1:
        return gages[0]
    else:
        return input(f"Select a gage to calibrate from {gages}: ")


def create_calibration_config(
    data_folder: Path,
    gage_id: str,
    iterations: int = ITERATIONS_DEFAULT,
    warmup_days: int = WARMUP_DEFAULT,
    calib_ratio: float = CALIBRATION_VALIDATION_RATIO,
    objective: str = "kge",
    flood_weight: float = FLOOD_WEIGHT_DEFAULT,
    q2_cfs: float | None = None,
    storm_depth: float | None = None,
    storm_concentration: float | None = None,
    stress: tuple[float, float] | None = None,
) -> None:
    # first pass at this so I'm probably not using ngen-cal properly
    # for now keep it simple and only allow single gage lumped calibration

    logging.info("Validating input files")
    # This initialization also checks all the files we need exist
    files = FilePaths(data_folder)

    files.calibration_folder.mkdir(exist_ok=True)

    if not gage_id:
        gage_id = pick_gage_to_calibrate(files.geopackage_path)

    all_gages = get_gages_from_hydrofabric(files.geopackage_path)
    if gage_id not in all_gages:
        raise ValueError(
            f"Gage {gage_id} not in {files.geopackage_path}, avaiable options are {all_gages}"
        )

    start, end = get_start_end_times(files.template_realization)

    observed = write_usgs_data_to_csv(start, end, gage_id, files.observed_discharge)
    create_crosswalk_json(files.geopackage_path, gage_id, files.crosswalk)
    # copy the ngen realization and troute config files into the calibration folder
    # convert the relative paths to absolute for ngiab_cal compatibility
    copy_and_convert_paths_to_absolute(files.template_realization, files.calibration_realization)
    copy_and_convert_paths_to_absolute(files.template_troute, files.calibration_troute)

    objective_params = ""
    if objective != "kge" or stress:
        period = calibration_period(start, end, warmup_days, calib_ratio)
        objective_params = write_flood_objective_files(
            files,
            gage_id,
            observed,
            period,
            objective,
            flood_weight,
            q2_cfs,
            storm_depth,
            storm_concentration,
            stress,
            # validation starts at the end of the calibration period
            (period[1], end),
        )

    # create the dates for the ngen-cal config
    write_ngen_cal_config(
        files,
        gage_id,
        start,
        end,
        iterations,
        warmup_days,
        calib_ratio,
        objective,
        objective_params,
    )


def print_run_command(folder_to_run: Path) -> None:
    logging.info("This is still experimental, run the following command to start calibration:")
    logging.info(
        f'docker run -it -v "{folder_to_run.resolve()}:/ngen/ngen/data" --user $(id -u):$(id -g) {DOCKER_IMAGE_NAME}'
    )
    logging.info("FOR PODMAN:")
    logging.info(
        f'podman run -it -v "{folder_to_run.resolve()}:/ngen/ngen/data" docker://{DOCKER_IMAGE_NAME}'
    )


def run_calibration(folder_to_run: Path) -> None:
    try:
        subprocess.run(f"docker pull {DOCKER_IMAGE_NAME}", shell=True)
    except:
        logging.error("Docker is not running, please start Docker and try again.")
    logging.warning("Beginning calibration...")
    try:
        command = f'docker run --rm -it -v "{str(folder_to_run.resolve())}:/ngen/ngen/data" --user $(id -u):$(id -g) {DOCKER_IMAGE_NAME} /calibration/run.sh'
        subprocess.run(command, shell=True)
        logging.info("Calibration complete.")
    except:
        logging.error("Calibration failed.")


def copy_best_params(realization: Path, calibrated_realization: Path) -> None:
    if not realization.exists() or not calibrated_realization.exists():
        logging.error(
            f"Realization path {realization} or calibrated realization path {calibrated_realization} does not exist."
        )
    logging.debug(
        f"Extracting model parameters from calibrated realization: {calibrated_realization}"
    )
    with open(realization) as f:
        uncalibrated = json.load(f)
    with open(realization.with_suffix("._old"), "w") as f:
        json.dump(uncalibrated, f, indent=4)
    with open(calibrated_realization) as f:
        calibrated = json.load(f)

    # this assumes that the calibration was done in ngiab
    old_modules = uncalibrated["global"]["formulations"][0]["params"]["modules"]
    new_modules = calibrated["global"]["formulations"][0]["params"]["modules"]

    for old_module in old_modules:
        for new_module in new_modules:
            if old_module["params"]["model_type_name"] == new_module["params"]["model_type_name"]:
                old_module["params"]["model_params"] = new_module["params"]["model_params"]

    logging.info(
        f"Copying model parameters from calibrated realization {calibrated_realization} into {realization}"
    )
    with open(realization, "w") as f:
        json.dump(uncalibrated, f, indent=4)


def main():
    setup_logging()
    parser = get_arg_parser()
    args = parser.parse_args()
    check_objective_arguments(parser, args)

    if args.debug:
        set_log_level(logging.DEBUG)
    paths = FilePaths(args.data_folder)

    data_folder_valid = validate_run_files(paths, log_level=logging.ERROR)

    if not data_folder_valid:
        return

    logging.debug(f"Searching for calibration files in {paths.calibration_folder}")
    config_valid = validate_calibration_files(paths, log_level=logging.NOTSET)
    if not config_valid and paths.calibration_folder.exists():
        # e.g. if there are missing config files and this isn't the first run
        # warn that there are files missing
        validate_calibration_files(paths, log_level=logging.WARN)

    if config_valid and not args.force:
        logging.warning(
            "Existing calibration configuration found, use -f or --force to overwrite existing calibration settings"
        )
        if args.run:
            response = Prompt.ask(
                "Your current settings may not have been applied, do you still want to run?",
                default="n",
                choices=["y", "n"],
            )
            if response != "y":
                logging.info("Calibration run cancelled")
                return

    # drop the gage- syntax used in other tools
    if args.gage:
        args.gage = args.gage.split("-")[-1]
    if not config_valid or args.force:
        create_calibration_config(
            args.data_folder,
            args.gage,
            args.iterations,
            args.warmup,
            args.calibration_ratio,
            args.objective,
            args.flood_weight,
            args.q2_cfs,
            args.storm_depth,
            args.storm_concentration,
            stress_ends(args),
        )

    if args.run:
        logging.info(f"Starting calibration run at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        run_calibration(args.data_folder)

    config_valid = validate_calibration_files(paths, log_level=logging.NOTSET)
    if config_valid:
        print_run_command(args.data_folder)

    if paths.calibrated_realization.exists():
        logging.info(f"Calibrated realization found: {paths.calibrated_realization}")
        copy_best_params(paths.template_realization, paths.calibrated_realization)


if __name__ == "__main__":
    sys.exit(main())
