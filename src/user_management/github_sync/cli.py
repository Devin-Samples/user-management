"""``user-management github-sync`` CLI."""

from __future__ import annotations

import argparse
import sys

from user_management.github_sync.sync import run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="user-management github-sync",
        description=(
            "Sync GitHub Team membership and repo access "
            "to Devin organizations."
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would change without making any API calls",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    args = parser.parse_args(argv)

    return run_sync(
        config_path=args.config,
        dry_run_flag=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
