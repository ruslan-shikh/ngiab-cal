from argparse import ArgumentParser, Namespace, RawTextHelpFormatter
from pathlib import Path

ITERATIONS_DEFAULT = 100
WARMUP_DEFAULT = 365
CALIBRATION_VALIDATION_RATIO = 0.5
FLOOD_WEIGHT_DEFAULT = 0.5
# Medians over seven study basins of how far the MRMS product furthest from AORC at the flood
# storms, usually radar-only, departed from AORC: its mean departure for the robust objective, its
# largest for the stress test. They measure how much two rain products disagree, not AORC's error
STORM_DEPTH_DEFAULT = 1.4
STORM_CONCENTRATION_DEFAULT = 1.2
STRESS_DEPTH_DEFAULT = 1.6
STRESS_CONCENTRATION_DEFAULT = 1.5


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
        choices=("kge", "kge-flood", "kge-flood-robust"),
        default="kge",
        help="Default:kge\nWhat the calibration minimizes:\n"
        "  kge               1 - KGE\n"
        "  kge-flood         (1 - w)(1 - KGE) + w * the mean peak error of the calibration floods\n"
        "  kge-flood-robust  as kge-flood, but each flood's error is its largest |ln(sim/obs)|\n"
        "                    over three runs: the forcing as it is, a drier and a wetter copy\n"
        "A flood is an independent peak above the 2-year flood. Its peak error is\n"
        "|sim - obs| / obs, where sim is the largest simulated flow within 7 days either\n"
        "side of the observed peak\n\n",
    )
    parser.add_argument(
        "--flood-weight",
        type=float,
        default=None,
        help=f"Default:{FLOOD_WEIGHT_DEFAULT}\nThe weight w of the flood term, from 0 to 1\n\n",
    )
    parser.add_argument(
        "--q2-cfs",
        type=float,
        help="The gage's 2-year flood in ft3/s, e.g. a Bulletin 17C estimate from PeakFQ.\n"
        "Default: the median of the gage's annual peaks from USGS\n\n",
    )
    parser.add_argument(
        "--storm-depth",
        type=float,
        default=None,
        help=f"Default:{STORM_DEPTH_DEFAULT}\n"
        "d >= 1, for kge-flood-robust. The drier copy scales the rain of each calibration\n"
        "flood's storm to 1/d, the wetter one to d times\n\n",
    )
    parser.add_argument(
        "--storm-concentration",
        type=float,
        default=None,
        help=f"Default:{STORM_CONCENTRATION_DEFAULT}\n"
        "g >= 1, for kge-flood-robust. Changes how a storm's rain is spread over its hours,\n"
        "keeping its total. The drier copy raises each hour's rain to the power 1/g, which\n"
        "spreads the storm out, and the wetter one to the power g, which packs it into the\n"
        "heaviest hours. A dry hour stays dry\n\n",
    )
    parser.add_argument(
        "--stress-test",
        action="store_true",
        help="After calibrating, rerun the best parameters with the storms of the validation\n"
        "period's floods changed, and report each flood's peak error under every change:\n"
        "depth in ten equal log steps down to 1/D and ten up to D, and concentration at\n"
        "1/G and G, alone and with each end of depth (up to 26 runs). The calibration\n"
        "itself does not change\n\n",
    )
    parser.add_argument(
        "--stress-depth",
        type=float,
        default=None,
        help=f"Default:{STRESS_DEPTH_DEFAULT}\nD >= 1, the largest depth change in the stress test\n\n",
    )
    parser.add_argument(
        "--stress-concentration",
        type=float,
        default=None,
        help=f"Default:{STRESS_CONCENTRATION_DEFAULT}\n"
        "G >= 1, the largest concentration change in the stress test, applied as for\n"
        "--storm-concentration\n\n",
    )

    return parser


def check_objective_arguments(parser: ArgumentParser, args: Namespace) -> None:
    """Exit with a usage message if a flood or stress test option is out of range or unused."""
    for name in ("storm_depth", "storm_concentration", "stress_depth", "stress_concentration"):
        value = getattr(args, name)
        if value is not None and not value >= 1:
            parser.error(f"--{name.replace('_', '-')} must be >= 1")
    if args.flood_weight is not None and not 0 <= args.flood_weight <= 1:
        parser.error("--flood-weight must be between 0 and 1")
    if args.q2_cfs is not None and not args.q2_cfs > 0:
        parser.error("--q2-cfs must be > 0")
    if args.objective == "kge-flood-robust" and args.storm_depth == args.storm_concentration == 1:
        parser.error("kge-flood-robust needs --storm-depth or --storm-concentration > 1")
    if args.stress_test:
        if args.stress_depth == args.stress_concentration == 1:
            parser.error("--stress-test needs --stress-depth or --stress-concentration > 1")
        # the stress test needs a validation period with floods in it
        if not 0 < args.calibration_ratio < 1:
            parser.error("--calibration_ratio must be between 0 and 1 for --stress-test")
    if args.objective == "kge" and args.flood_weight is not None:
        parser.error("--flood-weight needs a flood objective")
    if args.objective == "kge" and not args.stress_test and args.q2_cfs is not None:
        parser.error("--q2-cfs needs a flood objective or --stress-test")
    if args.objective != "kge-flood-robust" and (args.storm_depth or args.storm_concentration):
        parser.error("--storm-depth and --storm-concentration need --objective kge-flood-robust")
    if not args.stress_test and (args.stress_depth or args.stress_concentration):
        parser.error("--stress-depth and --stress-concentration need --stress-test")
