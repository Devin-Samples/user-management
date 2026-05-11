"""Helper to generate a users CSV pre-populated with DeepWiki-only access.

Each user gets ``account_member`` enterprise role and ``org_deepwiki`` org
role in the chosen org. The org defaults to the ``ORG_NAME`` env variable.
"""

from __future__ import annotations

import csv
import os
import sys


def gen_deepwiki(
    emails_file: str | None,
    output: str,
    org: str | None,
) -> int:
    """Write a DeepWiki-only users CSV.

    Returns 0 on success, non-zero on error.
    """
    org_name = org or os.environ.get("ORG_NAME") or "YourOrgName"

    if emails_file:
        with open(emails_file) as f:
            lines = list(f)
    else:
        lines = list(sys.stdin)

    emails = [line.strip() for line in lines if line.strip() and "@" in line]

    if not emails:
        print("Error: no valid emails found.", file=sys.stderr)
        return 1

    with open(output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "enterprise_role", "org_name", "org_role"])
        for email in emails:
            writer.writerow([email, "account_member", org_name, "org_deepwiki"])

    print(f"Wrote {len(emails)} user(s) to {output}")
    return 0
