"""Top-level ``user-management`` CLI dispatcher.

Subcommands:

- ``bulk ...``         — CSV/XLSX-driven enterprise sync
- ``github-sync ...``  — GitHub Team → Devin org sync
- ``doctor ...``       — diagnostic checks
- ``verify``           — alias for ``bulk verify``: prints enterprise state

The dispatcher is intentionally hand-rolled rather than using argparse
subparsers because each module owns its own argparse and we don't want the
top-level parser to intercept ``--help`` / ``-h`` meant for a submodule.
"""

from __future__ import annotations

import sys

from user_management import __version__
from user_management.bulk import cli as bulk_cli
from user_management.doctor import cli as doctor_cli
from user_management.github_sync import cli as github_sync_cli


_USAGE = """\
usage: user-management <module> [<args>...]

Unified tool for managing Devin enterprise users and orgs.

Modules:
  bulk           CSV/XLSX is the source of truth.
                 See `user-management bulk --help`.
  github-sync    GitHub Teams are the source of truth.
                 See `user-management github-sync --help`.
  doctor         Run diagnostic checks (devin-auth, github-token, …).
                 See `user-management doctor --help`.
  verify         Alias for `user-management bulk verify` — print enterprise state.

Options:
  -h, --help     Show this message.
  --version      Show version.

Set DEVIN_API_KEY (and optionally GITHUB_TOKEN) in your environment or .env file.
"""


def _print_help() -> None:
    sys.stdout.write(_USAGE)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in {"-h", "--help"}:
        _print_help()
        return 0
    if argv[0] == "--version":
        print(f"user-management {__version__}")
        return 0

    module, rest = argv[0], argv[1:]

    if module == "bulk":
        return bulk_cli.main(rest)
    if module == "github-sync":
        return github_sync_cli.main(rest)
    if module == "doctor":
        return doctor_cli.main(rest)
    if module == "verify":
        return bulk_cli.main(["verify", *rest])

    sys.stderr.write(
        f"user-management: error: unknown module '{module}'\n\n{_USAGE}"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
