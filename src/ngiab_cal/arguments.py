from argparse import ArgumentParser, Namespace, RawTextHelpFormatter
from pathlib import Path

ITERATIONS_DEFAULT = 100
WARMUP_DEFAULT = 365
CALIBRATION_VALIDATION_RATIO = 0.5
FLOOD_WEIGHT_DEFAULT = 0.5


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
        choices=("kge", "kge-flood"),
        default="kge",
        help="Default:kge\nWhat the calibration minimizes:\n"
        "  kge        1 - KGE\n"
        "  kge-flood  (1 - w)(1 - KGE) + w * the mean peak error of the calibration floods\n"
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

    return parser


def check_objective_arguments(parser: ArgumentParser, args: Namespace) -> None:
    """Exit with a usage message when a flood objective setting is out of range or unused."""
    if args.flood_weight is not None and not 0 <= args.flood_weight <= 1:
        parser.error("--flood-weight must be between 0 and 1")
    if args.q2_cfs is not None and not args.q2_cfs > 0:
        parser.error("--q2-cfs must be > 0")
    if args.objective == "kge":
        for name in ("flood_weight", "q2_cfs"):
            if getattr(args, name) is not None:
                parser.error(f"--{name.replace('_', '-')} needs --objective kge-flood")
