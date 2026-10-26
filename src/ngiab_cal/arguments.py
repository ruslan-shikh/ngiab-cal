from argparse import ArgumentParser, Namespace, RawTextHelpFormatter
from pathlib import Path

ITERATIONS_DEFAULT = 100
WARMUP_DEFAULT = 365
CALIBRATION_VALIDATION_RATIO = 0.5
FLOOD_WEIGHT_DEFAULT = 0.5

# --objective choices and the ngen-cal objective each one configures
OBJECTIVES = {
    "kge": "kge",
    "kge-flood-mean": "kge_flood_mean",
    "kge-flood-robust": "kge_flood_robust",
    "kge-robust": "kge_robust",
}
ROBUST_OBJECTIVES = ("kge-flood-robust", "kge-robust")


def get_arg_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Create a calibration config for ngen-cal",
        epilog="""
                 Default calibration settings on a 5 year period
        |   year 1   |   year 2   |   year 3   |   year 4   |   year 5   |
        |<- warmup ->|<-     calibration     ->|      no simulation      |
        |<-             warmup               ->|<-      validation     ->|
        """,
        formatter_class=RawTextHelpFormatter,
    )
    parser.add_argument(
        "data_folder",
        type=Path,
        help="Path to the folder you wish to calibrate",
    )
    parser.add_argument("-g", "--gage", type=str, help="Gage ID to use for calibration")
    parser.add_argument(
        "-f", "--force", help="Overwrite existing configuration", action="store_true"
    )
    parser.add_argument(
        "--run",
        help="Try to automatically run the calibration, this may be unstable",
        action="store_true",
    )
    parser.add_argument(
        "-i",
        "--iterations",
        help=f"Default:{ITERATIONS_DEFAULT} number of iterations to calibrate for\n\n",
        type=int,
        default=ITERATIONS_DEFAULT,
    )
    parser.add_argument("--debug", help="enable debug logging", action="store_true")
    parser.add_argument(
        "-w",
        "--warmup",
        help=f"Default:{WARMUP_DEFAULT}\nNumber of days at the beginning of the simulation\n to exclude from calibration objective metric calculation",
        type=int,
        default=WARMUP_DEFAULT,
    )
    parser.add_argument(
        "--calibration_ratio",
        "--cr",
        type=float,
        help=f"Default:{CALIBRATION_VALIDATION_RATIO}\nHow to split time after warmup into calibration and validation.\n1 == 100%% calibration, 0 == 100%% validation, 0.8 == 80%% calibration 20%% validation",
        default=CALIBRATION_VALIDATION_RATIO,
    )
    parser.add_argument(
        "--objective",
        choices=OBJECTIVES,
        default="kge",
        help="Default:kge\nWhat the calibration minimizes:\n"
        "  kge               1 - KGE\n"
        "  kge-flood-mean    (1 - w)(1 - KGE) + w * mean peak error of the calibration floods\n"
        "  kge-flood-robust  kge-flood-mean, each flood scored by its largest |ln(sim/obs peak)|\n"
        "                    among runs under the unchanged, a drier and a wetter forcing\n"
        "  kge-robust        1 - the lowest KGE among those three runs\n"
        "A flood is an independent peak above the 2-year flood; its peak error is\n"
        "|sim - obs| / obs, sim the largest simulated flow within 7 days of the observed peak\n\n",
    )
    parser.add_argument(
        "--flood-weight",
        type=float,
        default=FLOOD_WEIGHT_DEFAULT,
        help=f"Default:{FLOOD_WEIGHT_DEFAULT}\nThe weight w of the flood term, from 0 to 1\n\n",
    )
    parser.add_argument(
        "--q2-cfs",
        type=float,
        help="The gage's 2-year flood in ft3/s, e.g. a Bulletin 17C estimate from PeakFQ.\n"
        "Default: the median of the gage's annual peaks from USGS\n\n",
    )
    parser.add_argument(
        "--storm-error",
        action="store_true",
        help="Instead of writing a config, compare the forcing's rain with three MRMS\n"
        "precipitation products at the storms of the calibration floods, and write\n"
        "calibration/storm_error.csv. The robust objectives and --stress-test then take\n"
        "d, g and the stress test's ends from it. Needs pip install 'ngiab-cal[storms]'\n\n",
    )
    parser.add_argument(
        "--storm-depth",
        type=float,
        help="d >= 1 of the robust objectives: the drier forcing scales the calibration\n"
        "floods' storms to 1/d of their depth, the wetter one to d times.\n"
        "Default: the mean depth departure in calibration/storm_error.csv\n\n",
    )
    parser.add_argument(
        "--storm-concentration",
        type=float,
        help="g >= 1 of the robust objectives: the drier forcing raises the storms' hourly\n"
        "rain to 1/g, spreading it out, the wetter one to g, packing it into the heavier\n"
        "hours; each storm's total is kept before the depth change.\n"
        "Default: the mean concentration departure in calibration/storm_error.csv\n\n",
    )
    parser.add_argument(
        "--stress-test",
        action="store_true",
        help="After calibrating, rerun the best parameters with the storms of the validation\n"
        "period's floods changed, and report each flood's peak error under every change.\n"
        "Depth runs in ten equal steps of ln f to 1/d and to d; concentration takes 1/g\n"
        "and g, alone and with each end of depth: 26 runs beside the unchanged one.\n"
        "The calibration itself is unchanged\n\n",
    )
    parser.add_argument(
        "--stress-depth",
        type=float,
        help="d >= 1, the stress test's largest change of depth. Default: the largest\n"
        "depth departure in calibration/storm_error.csv, else --storm-depth\n\n",
    )
    parser.add_argument(
        "--stress-concentration",
        type=float,
        help="g >= 1, the stress test's largest change of concentration. Default: the largest\n"
        "concentration departure in calibration/storm_error.csv, else --storm-concentration\n\n",
    )

    return parser


def stress_ends(args: Namespace) -> tuple[float | None, float | None] | None:
    """The stress test's d and g, its own or else the robust objectives'; None without the test."""
    if not args.stress_test:
        return None
    depth = args.stress_depth if args.stress_depth is not None else args.storm_depth
    concentration = (
        args.stress_concentration
        if args.stress_concentration is not None
        else args.storm_concentration
    )
    return depth, concentration


def check_objective_arguments(parser: ArgumentParser, args: Namespace) -> None:
    """Exit with a usage message when the objective's settings don't fit together."""
    robust = args.objective in ROBUST_OBJECTIVES
    storm = (args.storm_depth, args.storm_concentration)
    if robust and None in storm:
        parser.error(
            f"{args.objective} needs --storm-depth and --storm-concentration, or the "
            "calibration/storm_error.csv --storm-error writes"
        )
    if not robust and storm != (None, None):
        parser.error("--storm-depth and --storm-concentration apply to the robust objectives")
    if robust and min(storm) < 1:
        parser.error("--storm-depth and --storm-concentration are 1 or more")
    if not 0 <= args.flood_weight <= 1:
        parser.error("--flood-weight is between 0 and 1")
    if args.q2_cfs is not None and args.q2_cfs <= 0:
        parser.error("--q2-cfs is a flow above 0")
    if not args.stress_test and (args.stress_depth, args.stress_concentration) != (None, None):
        parser.error("--stress-depth and --stress-concentration apply to --stress-test")
    ends = stress_ends(args)
    if ends is not None:
        if None in ends:
            parser.error(
                "--stress-test needs --stress-depth and --stress-concentration, the "
                "calibration/storm_error.csv --storm-error writes, or a robust objective's "
                "--storm-depth and --storm-concentration"
            )
        if min(ends) < 1:
            parser.error("--stress-depth and --stress-concentration are 1 or more")
        if max(ends) == 1:
            parser.error("--stress-test changes nothing when d and g are both 1")
        if args.calibration_ratio >= 1:
            parser.error(
                "--stress-test reads the validation period's floods, and --calibration_ratio 1 "
                "leaves no validation period"
            )
