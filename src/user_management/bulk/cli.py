"""``user-management bulk ...`` CLI.

Subcommands:

- ``apply``  — read CSVs, diff, and apply (or preview with ``--dry-run``)
- ``pull``   — export current API state into sync-format CSVs
- ``gen-deepwiki`` — generate a DeepWiki-only users CSV from an emails file
- ``verify`` — print enterprise state (alias for the top-level ``verify``)
"""

from __future__ import annotations

import argparse
import sys

from user_management.bulk.deepwiki import gen_deepwiki
from user_management.bulk.spreadsheet import (
    OrgRow,
    UserRow,
    parse_orgs_file,
    parse_users_file,
)
from user_management.bulk.sync import BulkManager, SyncValidator
from user_management.core.client import DevinAPIClient
from user_management.core.config import (
    get_devin_api_base_url,
    get_devin_api_key,
    load_env,
)


def run_verify(client: DevinAPIClient) -> None:
    """``verify`` mode: test credentials and print discovered resources."""
    print("Verifying credentials...")
    creds = client.verify_credentials()
    print(
        f"  Service user: {creds.get('service_user_name')} "
        f"(id={creds.get('service_user_id')})"
    )

    print("\nDiscovering roles...")
    roles = client.list_roles()
    for r in roles:
        print(
            f"  - {r['role_name']} (id={r['role_id']}, "
            f"type={r.get('role_type', 'N/A')})"
        )

    print("\nDiscovering organizations...")
    orgs = client.list_organizations()
    print(f"  Found {len(orgs)} organization(s)")
    for o in orgs:
        print(f"  - {o['name']} (id={o['org_id']})")

    print("\nDiscovering git connections...")
    conns = client.list_git_connections()
    for c in conns:
        print(
            f"  - {c.get('name', 'unnamed')} (id={c['git_connection_id']}, "
            f"type={c['git_provider_type']}, host={c.get('host', 'N/A')})"
        )

    try:
        gh_id = client.get_github_connection_id()
        print(f"\n  GitHub connection ID: {gh_id}")
    except Exception:
        print("\n  WARNING: No GitHub connection found")


def _build_client() -> DevinAPIClient:
    load_env()
    api_key = get_devin_api_key()
    base_url = get_devin_api_base_url()
    return DevinAPIClient(api_key=api_key, base_url=base_url)


def _cmd_apply(args: argparse.Namespace) -> int:
    client = _build_client()

    user_rows: list[UserRow] = []
    org_rows: list[OrgRow] = []

    if args.users_file:
        print(f"Parsing users file: {args.users_file}")
        user_rows = parse_users_file(args.users_file)
        print(f"  Found {len(user_rows)} user row(s)")

    if args.orgs_file:
        print(f"Parsing orgs file: {args.orgs_file}")
        org_rows = parse_orgs_file(args.orgs_file)
        print(f"  Found {len(org_rows)} org row(s)")

    if not user_rows and not org_rows:
        print(
            "ERROR: At least one of --users-file or --orgs-file is required.",
            file=sys.stderr,
        )
        return 1

    is_sync = any(r.action == "sync" for r in user_rows + org_rows)

    mgr = BulkManager(client=client, dry_run=args.dry_run)
    print("\nInitializing (verifying credentials, discovering resources)...")
    mgr.initialize()
    print(f"  GitHub connection: {mgr.github_connection_id or '(none)'}")
    print(f"  Known orgs: {len(mgr.org_map)}")
    print(f"  Known users: {len(mgr.user_map)}")

    if is_sync:
        print("\n*** SYNC MODE — CSV is source of truth ***")

        validator = SyncValidator()
        user_validation = validator.validate_users(user_rows)
        org_validation = validator.validate_orgs(org_rows)
        repo_validation = validator.validate_repos(
            org_rows, mgr.github_connection_id, client
        )

        all_errors = (
            user_validation.errors + org_validation.errors + repo_validation.errors
        )
        all_warnings = (
            user_validation.warnings
            + org_validation.warnings
            + repo_validation.warnings
        )

        if all_warnings:
            print("\nValidation warnings:")
            for w in all_warnings:
                print(f"  ! {w}")

        if all_errors:
            print("\nValidation ERRORS (aborting):")
            for e in all_errors:
                print(f"  X {e}")
            return 1
        print("\n  Validation passed.")

        org_diff = mgr.compute_org_diff(org_rows)
        user_diff = mgr.compute_user_diff(user_rows)
        mgr.print_diff(org_diff, user_diff)

        if args.dry_run:
            print("\n*** DRY RUN — no mutations will be made ***")
        else:
            print("\nApplying changes...")
        mgr.execute_sync(org_diff, user_diff)

    else:
        # Legacy action-based mode
        if args.dry_run:
            print("\n*** DRY RUN MODE — no mutations will be made ***\n")

        org_adds = [r for r in org_rows if r.action == "add"]
        org_updates = [r for r in org_rows if r.action == "update"]
        org_removes = [r for r in org_rows if r.action == "remove"]
        user_adds = [r for r in user_rows if r.action == "add"]
        user_removes = [r for r in user_rows if r.action == "remove"]

        if org_adds:
            print(f"\nProcessing {len(org_adds)} org addition(s)...")
            mgr.process_org_additions(org_adds)
        if org_updates:
            print(f"\nProcessing {len(org_updates)} org update(s)...")
            mgr.process_org_updates(org_updates)
        if user_adds:
            print(f"\nProcessing {len(user_adds)} user addition(s)...")
            mgr.process_user_additions(user_adds)
        if user_removes:
            print(f"\nProcessing {len(user_removes)} user removal(s)...")
            mgr.process_user_removals(user_removes)
        if org_removes:
            print(f"\nProcessing {len(org_removes)} org removal(s)...")
            mgr.process_org_removals(org_removes)

    mgr.print_summary()

    if args.output:
        with open(args.output, "w", newline="") as f:
            f.write(mgr.get_results_csv())
        print(f"\nDetailed results written to: {args.output}")

    return 0 if mgr.get_summary()["failed"] == 0 else 1


def _cmd_pull(args: argparse.Namespace) -> int:
    client = _build_client()
    mgr = BulkManager(client=client, dry_run=False)
    print("Initializing (verifying credentials, discovering resources)...")
    mgr.initialize()
    print(f"  GitHub connection: {mgr.github_connection_id or '(none)'}")
    print(f"  Known orgs: {len(mgr.org_map)}")
    print(f"  Known users: {len(mgr.user_map)}")

    print("\nPulling current enterprise state...")
    num_orgs, num_users = mgr.write_exported_state(
        orgs_out=args.orgs_out,
        users_out=args.users_out,
        include_unmanaged_orgs=args.include_unmanaged_orgs,
    )
    print(f"  Wrote {num_orgs} org row(s) → {args.orgs_out}")
    print(f"  Wrote {num_users} user row(s) → {args.users_out}")
    print(
        "\nEdit these CSVs locally, then re-run with:\n"
        f"  user-management bulk apply --orgs-file {args.orgs_out} "
        f"--users-file {args.users_out} --dry-run"
    )
    return 0


def _cmd_verify(_args: argparse.Namespace) -> int:
    client = _build_client()
    run_verify(client)
    return 0


def _cmd_gen_deepwiki(args: argparse.Namespace) -> int:
    load_env()
    return gen_deepwiki(
        emails_file=args.emails,
        output=args.output,
        org=args.org,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="user-management bulk",
        description="CSV/XLSX-driven bulk enterprise management.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # apply
    p_apply = sub.add_parser("apply", help="Apply CSV changes to the enterprise.")
    p_apply.add_argument("--users-file", help="Path to users CSV/XLSX file")
    p_apply.add_argument("--orgs-file", help="Path to orgs CSV/XLSX file")
    p_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show planned operations without executing",
    )
    p_apply.add_argument("--output", help="Path to write detailed results CSV")
    p_apply.set_defaults(func=_cmd_apply)

    # pull
    p_pull = sub.add_parser(
        "pull",
        help="Export current enterprise state to sync-format CSVs.",
    )
    p_pull.add_argument(
        "--orgs-out",
        default="orgs-current.csv",
        help="Output path for orgs CSV (default: orgs-current.csv)",
    )
    p_pull.add_argument(
        "--users-out",
        default="users-current.csv",
        help="Output path for users CSV (default: users-current.csv)",
    )
    p_pull.add_argument(
        "--include-unmanaged-orgs",
        action="store_true",
        help=(
            "Also include non-convention orgs (those without '/' in their name). "
            "Off by default so re-importing the export does not try to modify "
            "the enterprise's default/unmanaged orgs."
        ),
    )
    p_pull.set_defaults(func=_cmd_pull)

    # verify
    p_verify = sub.add_parser(
        "verify",
        help="Test credentials and print discovered enterprise resources.",
    )
    p_verify.set_defaults(func=_cmd_verify)

    # gen-deepwiki
    p_dw = sub.add_parser(
        "gen-deepwiki",
        help="Generate a users CSV for bulk DeepWiki-only org access.",
    )
    p_dw.add_argument(
        "--emails",
        help="Path to a file with one email per line (reads stdin if omitted)",
    )
    p_dw.add_argument(
        "--org",
        help="Org name (defaults to ORG_NAME env var)",
    )
    p_dw.add_argument(
        "--output",
        default="users.csv",
        help="Output CSV path (default: users.csv)",
    )
    p_dw.set_defaults(func=_cmd_gen_deepwiki)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
