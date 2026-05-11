"""``user-management doctor ...`` CLI — environment diagnostics."""

from __future__ import annotations

import argparse
import sys

from user_management.core.config import load_env
from user_management.doctor.checks import (
    check_devin_auth,
    check_email_visibility,
    check_github_app,
    check_github_token,
    run_all,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="user-management doctor",
        description="Run diagnostic checks for Devin + GitHub credentials.",
    )
    parser.add_argument(
        "--check",
        choices=[
            "devin-auth",
            "github-token",
            "github-app",
            "email-visibility",
            "all",
        ],
        default="all",
        help="Which check to run (default: all)",
    )
    parser.add_argument(
        "--github-org",
        help="GitHub org slug (required for --check email-visibility)",
    )
    parser.add_argument(
        "--team-slug",
        help="GitHub team slug (required for --check email-visibility)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env()

    if args.check == "devin-auth":
        ok = check_devin_auth()
    elif args.check == "github-token":
        ok = check_github_token()
    elif args.check == "github-app":
        ok = check_github_app()
    elif args.check == "email-visibility":
        if not args.github_org or not args.team_slug:
            print(
                "ERROR: --github-org and --team-slug are required for "
                "--check email-visibility",
                file=sys.stderr,
            )
            return 2
        ok = check_email_visibility(
            github_org=args.github_org, team_slug=args.team_slug,
        )
    else:
        ok = run_all(
            github_org=args.github_org, team_slug=args.team_slug,
        )

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
