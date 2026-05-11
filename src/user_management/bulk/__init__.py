"""CSV/XLSX-driven bulk management of Devin enterprise users and orgs.

The CSV is the source of truth. Each run fetches the current API state,
computes a diff, and applies only the necessary changes. Re-running with
the same CSV is a no-op.

See :mod:`user_management.bulk.sync` for the orchestration class and
:mod:`user_management.bulk.spreadsheet` for CSV/XLSX I/O.
"""

from user_management.bulk.spreadsheet import (
    OrgRow,
    SpreadsheetValidationError,
    UserRow,
    parse_orgs_file,
    parse_users_file,
    write_orgs_csv,
    write_users_csv,
)
from user_management.bulk.sync import (
    BulkManager,
    OrgDiff,
    SyncValidator,
    UserDiff,
    ValidationResult,
    is_managed_org,
)

__all__ = [
    "BulkManager",
    "OrgDiff",
    "UserDiff",
    "OrgRow",
    "UserRow",
    "SpreadsheetValidationError",
    "SyncValidator",
    "ValidationResult",
    "is_managed_org",
    "parse_orgs_file",
    "parse_users_file",
    "write_orgs_csv",
    "write_users_csv",
]
