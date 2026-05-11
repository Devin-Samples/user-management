"""``user-management doctor ...`` CLI — environment diagnostics."""

from __future__ import annotations

import argparse
import sys

from user_management.core.config import load_env
from user_management.doctor.checks import (
    check_devin_auth,
    run_all,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="user-management doctor",
        description="Run diagnostic checks for Devin credentials.",
    )
    parser.add_argument(
        "--check",
        choices=[
            "devin-auth",
            "all",
        ],
        default="all",
        help="Which check to run (default: all)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env()

    if args.check == "devin-auth":
        ok = check_devin_auth()
    else:
        ok = run_all()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
