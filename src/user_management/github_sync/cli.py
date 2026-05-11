"""``user-management github-sync ...`` CLI.

Subcommands:

- ``run``   — execute the sync
- ``check`` — validate the YAML config and resolve every team slug + org ID
"""

from __future__ import annotations

import argparse
import logging
import sys

from user_management.core.client import DevinAPIClient
from user_management.core.config import (
    get_devin_api_base_url,
    get_devin_api_key,
    get_github_token,
    load_env,
)
from user_management.core.models import SyncSummary
from user_management.github_sync.config import load_config
from user_management.github_sync.github_client import GitHubClient
from user_management.github_sync.sync import (
    print_summary,
    run_auto_sync,
    run_legacy_sync,
)

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _cmd_run(args: argparse.Namespace) -> int:
    _configure_logging(args.verbose)
    load_env()
    config = load_config(args.config)

    dry_run = args.dry_run or config.dry_run
    if dry_run:
        logger.info("Running in DRY RUN mode — no changes will be made")

    devin_client = DevinAPIClient(
        api_key=get_devin_api_key(),
        base_url=get_devin_api_base_url(),
    )

    all_summaries: list[SyncSummary] = []
    for org_config in config.github_orgs:
        logger.info("=" * 60)
        logger.info("Processing GitHub org: %s", org_config.github_org)
        logger.info("=" * 60)

        token_env_var = org_config.github_token_env_var or "GITHUB_TOKEN"
        github_client = GitHubClient(token=get_github_token(token_env_var))

        if org_config.is_auto_mode:
            logger.info("Running in AUTO mode (no team_mappings — discovering teams)")
            summary = run_auto_sync(
                org_config,
                devin_client=devin_client,
                github_client=github_client,
                default_member_role=config.default_member_role,
                dry_run=dry_run,
                config_path=args.config,
            )
        else:
            logger.info("Running in LEGACY mode (using explicit team_mappings)")
            summary = run_legacy_sync(
                org_config,
                devin_client=devin_client,
                github_client=github_client,
                default_member_role=config.default_member_role,
                dry_run=dry_run,
            )

        print_summary(summary)
        all_summaries.append(summary)

    total_errors = 0
    for summary in all_summaries:
        total_errors += (
            sum(len(r.errors) for r in summary.member_results)
            + sum(len(r.errors) for r in summary.repo_results)
            + sum(1 for r in summary.orgs_created if r.error)
        )
    return 1 if total_errors > 0 else 0


def _cmd_check(args: argparse.Namespace) -> int:
    """Validate the YAML config and resolve every team slug + devin org id."""
    _configure_logging(args.verbose)
    load_env()
    config = load_config(args.config)
    print(f"OK: {args.config} parses as valid SyncConfig.")
    print(f"  github_orgs: {len(config.github_orgs)}")
    print(f"  default_member_role: {config.default_member_role}")
    print(f"  dry_run (config-level): {config.dry_run}")

    devin_client = DevinAPIClient(
        api_key=get_devin_api_key(),
        base_url=get_devin_api_base_url(),
    )

    print("\nResolving Devin orgs referenced by config...")
    devin_orgs = {o["org_id"]: o.get("name", "") for o in devin_client.list_organizations()}
    print(f"  Enterprise has {len(devin_orgs)} org(s).")

    problems = 0
    for org_config in config.github_orgs:
        token_env_var = org_config.github_token_env_var or "GITHUB_TOKEN"
        token = get_github_token(token_env_var, required=False)
        if not token:
            print(
                f"  X github_org={org_config.github_org}: "
                f"{token_env_var} is not set."
            )
            problems += 1
            continue

        github_client = GitHubClient(token=token)

        try:
            teams = {
                t.slug: t for t in github_client.list_org_teams(org_config.github_org)
            }
        except Exception as exc:
            print(
                f"  X github_org={org_config.github_org}: "
                f"cannot list teams: {exc}"
            )
            problems += 1
            continue

        print(
            f"\n  github_org={org_config.github_org} "
            f"({len(teams)} teams visible, "
            f"{len(org_config.team_mappings)} mapping(s) configured)"
        )

        for mapping in org_config.team_mappings:
            team_ok = mapping.github_team_slug in teams
            org_ok = mapping.devin_org_id in devin_orgs
            status = "OK" if (team_ok and org_ok) else "X"
            details = []
            if not team_ok:
                details.append("team slug not found in GitHub")
            if not org_ok:
                details.append("devin_org_id not found in Devin enterprise")
            suffix = f" — {', '.join(details)}" if details else ""
            print(
                f"    {status} {mapping.github_team_slug} -> "
                f"{mapping.devin_org_id}{suffix}"
            )
            if not (team_ok and org_ok):
                problems += 1

    if problems:
        print(f"\n{problems} problem(s) found.")
        return 1
    print("\nAll checks passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="user-management github-sync",
        description="Sync GitHub Team membership and repo access to Devin orgs.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_run = sub.add_parser("run", help="Execute the sync.")
    p_run.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would change without making any API calls",
    )
    p_run.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    p_run.set_defaults(func=_cmd_run)

    p_check = sub.add_parser(
        "check",
        help="Validate config and resolve every team slug + devin org id.",
    )
    p_check.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    p_check.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    p_check.set_defaults(func=_cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
