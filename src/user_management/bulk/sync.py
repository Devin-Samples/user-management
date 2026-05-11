"""Bulk sync orchestration — diff/apply CSV state against the Devin API.

Supports two modes:

1. **Legacy (action-based)**: CSV rows have an ``action`` column
   (``add`` / ``remove`` / ``update``).
2. **Sync (source-of-truth)**: CSV has no ``action`` column — it IS the
   desired state.  The tool diffs current API state against the CSV and
   applies only necessary changes.

IMPORTANT SAFEGUARD — Non-convention org protection:
Orgs that do NOT follow the ``{Team}/{Project}`` naming convention (i.e. those
without a ``/`` in their name) are treated as **unmanaged** and are NEVER
deleted by this tool.  This prevents accidental deletion of the enterprise's
default org, which would break the invite API.  The check is enforced at
three layers:

  1. :meth:`BulkManager.compute_org_diff` — never marks them for deletion
  2. :meth:`BulkManager._sync_delete_org` — refuses to execute deletion
  3. :meth:`BulkManager.process_org_removals` — refuses to execute deletion
     (legacy mode)
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from user_management.bulk.spreadsheet import (
    OrgRow,
    UserRow,
    write_orgs_csv,
    write_users_csv,
)
from user_management.core.client import DevinAPIClient


# ---------------------------------------------------------------------------
# Safeguard helpers
# ---------------------------------------------------------------------------
def is_managed_org(org_name: str) -> bool:
    """Return True if the org follows the ``{Team}/{Project}`` naming convention.

    Only "managed" orgs (those containing ``/``) may be deleted by this tool.
    Non-convention orgs (e.g. the enterprise default org) are treated as
    **unmanaged** and are preserved under all circumstances.
    """
    return "/" in org_name


# ---------------------------------------------------------------------------
# Diff data structures
# ---------------------------------------------------------------------------
@dataclass
class OrgDiff:
    """Computed diff for organizations."""

    to_create: list[OrgRow] = field(default_factory=list)
    to_update: list[tuple[OrgRow, dict]] = field(default_factory=list)
    to_delete: list[dict] = field(default_factory=list)


@dataclass
class UserDiff:
    """Computed diff for users."""

    to_invite: list[UserRow] = field(default_factory=list)
    to_remove: list[dict] = field(default_factory=list)
    role_changes: list[tuple[UserRow, dict]] = field(default_factory=list)
    org_additions: list[tuple[UserRow, str]] = field(default_factory=list)
    org_removals: list[tuple[dict, str, str]] = field(default_factory=list)
    org_role_changes: list[tuple[UserRow, dict, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@dataclass
class ValidationResult:
    """Result of validating CSV data before applying changes."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


class SyncValidator:
    """Validates CSV data against enterprise configuration."""

    VALID_ENTERPRISE_ROLES = {"account_admin", "account_member"}
    VALID_ORG_ROLES = {"org_admin", "org_member", "org_deepwiki"}

    ROLE_ALIASES: dict[str, str] = {
        "enterprise_admin": "account_admin",
        "enterprise_member": "account_member",
        "admin": "account_admin",
        "member": "account_member",
    }

    def __init__(self, known_roles: set[str] | None = None):
        self.known_roles = known_roles or (
            self.VALID_ENTERPRISE_ROLES | self.VALID_ORG_ROLES
        )

    def resolve_role(self, role_name: str) -> str:
        """Resolve a role name/alias to a canonical role_id."""
        lower = role_name.lower().strip()
        if lower in self.ROLE_ALIASES:
            return self.ROLE_ALIASES[lower]
        return lower

    def validate_users(self, users: list[UserRow]) -> ValidationResult:
        result = ValidationResult()
        seen_emails: set[tuple[str, str]] = set()
        for i, row in enumerate(users):
            resolved = self.resolve_role(row.enterprise_role)
            if (
                resolved not in self.known_roles
                and resolved not in self.VALID_ENTERPRISE_ROLES
            ):
                result.errors.append(
                    f"Row {i + 2}: invalid enterprise_role '{row.enterprise_role}' "
                    f"(valid: {sorted(self.VALID_ENTERPRISE_ROLES | set(self.ROLE_ALIASES.keys()))})"
                )
            if row.org_role:
                org_resolved = self.resolve_role(row.org_role)
                if org_resolved not in self.VALID_ORG_ROLES:
                    result.errors.append(
                        f"Row {i + 2}: invalid org_role '{row.org_role}' "
                        f"(valid: {sorted(self.VALID_ORG_ROLES)})"
                    )
            key = (row.email, row.org_name)
            if key in seen_emails:
                result.warnings.append(
                    f"Row {i + 2}: duplicate entry for {row.email} in "
                    f"{row.org_name or 'enterprise'}"
                )
            seen_emails.add(key)
        return result

    def validate_orgs(self, orgs: list[OrgRow]) -> ValidationResult:
        result = ValidationResult()
        seen_names: set[str] = set()
        for i, row in enumerate(orgs):
            if not row.org_name.strip():
                result.errors.append(f"Row {i + 2}: org_name is required")
            if row.org_name in seen_names:
                result.errors.append(
                    f"Row {i + 2}: duplicate org name '{row.org_name}'"
                )
            seen_names.add(row.org_name)
            if row.cycle_acu_limit is not None and row.cycle_acu_limit < 0:
                result.errors.append(
                    f"Row {i + 2}: cycle_acu_limit must be non-negative"
                )
            if row.session_acu_limit is not None and row.session_acu_limit < 0:
                result.errors.append(
                    f"Row {i + 2}: session_acu_limit must be non-negative"
                )
        return result

    def validate_repos(
        self, orgs: list[OrgRow], github_connection_id: str, client: DevinAPIClient
    ) -> ValidationResult:
        """Validate that repos in the CSV look like ``owner/repo`` paths."""
        result = ValidationResult()
        all_repos: set[str] = set()
        for org_row in orgs:
            for repo in org_row.repos:
                all_repos.add(repo)

        for repo in sorted(all_repos):
            if "/" not in repo or repo.count("/") != 1:
                result.errors.append(
                    f"Repo '{repo}' must be in 'owner/repo' format"
                )
                continue
            parts = repo.split("/")
            if not parts[0] or not parts[1]:
                result.errors.append(f"Repo '{repo}' has empty owner or repo name")
        return result


# ---------------------------------------------------------------------------
# BulkManager — core orchestration
# ---------------------------------------------------------------------------
class BulkManager:
    """Orchestrates bulk enterprise operations using the Devin v3 API."""

    _ROLE_ALIASES: dict[str, str] = {
        "enterprise_admin": "account_admin",
        "enterprise_member": "account_member",
        "org_admin": "org_admin",
        "org_member": "org_member",
        "org_deepwiki": "org_deepwiki",
    }

    def __init__(self, client: DevinAPIClient, dry_run: bool = False):
        self.client = client
        self.dry_run = dry_run
        self.github_connection_id: str = ""
        self.role_map: dict[str, str] = {}
        self.org_map: dict[str, str] = {}
        self.user_map: dict[str, str] = {}
        self.current_users: list[dict] = []
        self.current_orgs: list[dict] = []
        self.results: list[dict] = []

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """Verify credentials, discover resources, build lookup maps."""
        self.client.verify_credentials()
        try:
            self.github_connection_id = self.client.get_github_connection_id()
        except Exception:
            self.github_connection_id = ""

        roles = self.client.list_roles()
        for role in roles:
            rid = role["role_id"]
            self.role_map[rid] = rid
        for alias, rid in self._ROLE_ALIASES.items():
            self.role_map[alias] = rid

        self.current_orgs = self.client.list_organizations()
        for org in self.current_orgs:
            self.org_map[org["name"]] = org["org_id"]

        self.current_users = self.client.list_users()
        for user in self.current_users:
            self.user_map[user["email"]] = user["user_id"]

    def _resolve_role(self, role_name: str) -> str:
        if role_name in self.role_map:
            return self.role_map[role_name]
        lower = role_name.lower().strip()
        if lower in self.role_map:
            return self.role_map[lower]
        return role_name

    def _record(self, operation: str, target: str, status: str, error: str = "") -> None:
        self.results.append(
            {
                "operation": operation,
                "target": target,
                "status": status,
                "error_message": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    # ------------------------------------------------------------------
    # Diff computation
    # ------------------------------------------------------------------
    def compute_org_diff(self, desired_orgs: list[OrgRow]) -> OrgDiff:
        """Compare desired org state from CSV with current API state."""
        diff = OrgDiff()
        desired_map = {row.org_name: row for row in desired_orgs}
        current_map = {org["name"]: org for org in self.current_orgs}

        for name, row in desired_map.items():
            if name not in current_map:
                diff.to_create.append(row)

        for name, desired_row in desired_map.items():
            if name not in current_map:
                continue
            current = current_map[name]
            org_id = current["org_id"]

            if "current_repos" not in current:
                try:
                    perms = self.client.list_org_git_permissions(org_id)
                    current["current_repos"] = sorted(
                        p.get("repo_path", "") for p in perms if p.get("repo_path")
                    )
                except Exception:
                    current["current_repos"] = []

            desired_repos_sorted = sorted(desired_row.repos)
            current_repos_sorted = current["current_repos"]

            needs_update = False
            if (
                desired_row.cycle_acu_limit is not None
                and desired_row.cycle_acu_limit != current.get("max_cycle_acu_limit")
            ):
                needs_update = True
            if (
                desired_row.session_acu_limit is not None
                and desired_row.session_acu_limit
                != current.get("max_session_acu_limit")
            ):
                needs_update = True
            if desired_row.repos and desired_repos_sorted != current_repos_sorted:
                needs_update = True

            if needs_update:
                diff.to_update.append((desired_row, current))

        # SAFEGUARD (Layer 1)
        for name, org in current_map.items():
            if name not in desired_map and is_managed_org(name):
                diff.to_delete.append(org)

        return diff

    def compute_user_diff(self, desired_users: list[UserRow]) -> UserDiff:
        """Compare desired user state from CSV with current API state."""
        diff = UserDiff()

        desired_by_email: dict[str, dict] = {}
        for row in desired_users:
            if row.email not in desired_by_email:
                desired_by_email[row.email] = {
                    "enterprise_role": (
                        self._resolve_role(row.enterprise_role)
                        if row.enterprise_role
                        else "account_member"
                    ),
                    "org_memberships": {},
                    "rows": [],
                }
            desired_by_email[row.email]["rows"].append(row)
            if row.org_name:
                desired_by_email[row.email]["org_memberships"][row.org_name] = (
                    row.org_role or "org_member"
                )

        current_by_email: dict[str, dict] = {}
        for user in self.current_users:
            enterprise_role = ""
            org_memberships: dict[str, str] = {}
            org_name_to_id: dict[str, str] = {}
            for ra in user.get("role_assignments", []):
                role = ra.get("role", {})
                if ra.get("org_id") is None:
                    enterprise_role = role.get("role_id", "")
                else:
                    org_id = ra["org_id"]
                    org_memberships[org_id] = role.get("role_id", "")
                    for oname, oid in self.org_map.items():
                        if oid == org_id:
                            org_name_to_id[oname] = org_id
                            break
            current_by_email[user["email"]] = {
                "user_id": user["user_id"],
                "enterprise_role": enterprise_role,
                "org_memberships": org_memberships,
                "org_name_to_id": org_name_to_id,
                "user": user,
            }

        for email, desired in desired_by_email.items():
            if email not in current_by_email:
                diff.to_invite.append(desired["rows"][0])

        for email, current in current_by_email.items():
            if email not in desired_by_email:
                diff.to_remove.append(current["user"])

        for email in desired_by_email:
            if email not in current_by_email:
                continue
            desired = desired_by_email[email]
            current = current_by_email[email]

            if desired["enterprise_role"] != current["enterprise_role"]:
                diff.role_changes.append((desired["rows"][0], current["user"]))

            desired_org_memberships = desired["org_memberships"]
            current_org_by_name = current["org_name_to_id"]
            current_org_roles = current["org_memberships"]

            for org_name, desired_role in desired_org_memberships.items():
                org_id = self.org_map.get(org_name)
                if not org_id:
                    continue
                if org_name not in current_org_by_name:
                    for row in desired["rows"]:
                        if row.org_name == org_name:
                            diff.org_additions.append((row, org_id))
                            break
                else:
                    current_role = current_org_roles.get(org_id, "")
                    if desired_role != current_role:
                        for row in desired["rows"]:
                            if row.org_name == org_name:
                                diff.org_role_changes.append(
                                    (row, current["user"], org_id)
                                )
                                break

            # SAFEGUARD: never remove users from unmanaged orgs.
            for org_name, org_id in current_org_by_name.items():
                if not is_managed_org(org_name):
                    continue
                if org_name not in desired_org_memberships:
                    diff.org_removals.append((current["user"], org_id, org_name))

        return diff

    # ------------------------------------------------------------------
    # Sync execution
    # ------------------------------------------------------------------
    def execute_sync(self, org_diff: OrgDiff, user_diff: UserDiff) -> None:
        """Apply the computed diffs to the enterprise via API calls."""
        for row in org_diff.to_create:
            self._sync_create_org(row)
        for desired_row, current_org in org_diff.to_update:
            self._sync_update_org(desired_row, current_org)
        if user_diff.to_invite:
            self._sync_invite_users(user_diff.to_invite)
        for desired_row, current_user in user_diff.role_changes:
            self._sync_update_enterprise_role(desired_row, current_user)
        for row, org_id in user_diff.org_additions:
            self._sync_add_to_org(row, org_id)
        for row, current_user, org_id in user_diff.org_role_changes:
            self._sync_update_org_role(row, current_user, org_id)
        for user, org_id, org_name in user_diff.org_removals:
            self._sync_remove_from_org(user, org_id, org_name)
        for user in user_diff.to_remove:
            self._sync_remove_user(user)
        for org in org_diff.to_delete:
            self._sync_delete_org(org)

    def _sync_create_org(self, row: OrgRow) -> None:
        if self.dry_run:
            self._record("create_org", row.org_name, "dry-run")
            if row.cycle_acu_limit is not None:
                self._record(
                    "set_acu_limit",
                    f"{row.org_name} (cycle={row.cycle_acu_limit})",
                    "dry-run",
                )
            if row.repos:
                self._record(
                    "set_git_permissions",
                    f"{row.org_name} → {row.repos}",
                    "dry-run",
                )
            return
        try:
            resp = self.client.create_organization(
                row.org_name,
                max_cycle_acu_limit=row.cycle_acu_limit,
                max_session_acu_limit=row.session_acu_limit,
            )
            new_org_id = resp["org_id"]
            self.org_map[row.org_name] = new_org_id
            self._record("create_org", row.org_name, "succeeded")

            if row.cycle_acu_limit is not None:
                self.client.set_org_acu_limit(new_org_id, row.cycle_acu_limit)
                self._record(
                    "set_acu_limit",
                    f"{row.org_name} (cycle={row.cycle_acu_limit})",
                    "succeeded",
                )

            if row.repos:
                perms = [
                    {
                        "git_connection_id": self.github_connection_id,
                        "repo_path": repo,
                    }
                    for repo in row.repos
                ]
                self.client.set_org_git_permissions(new_org_id, perms)
                self._record(
                    "set_git_permissions",
                    f"{row.org_name} → {row.repos}",
                    "succeeded",
                )
        except Exception as e:
            self._record("create_org", row.org_name, "failed", str(e))

    def _sync_update_org(self, desired: OrgRow, current: dict) -> None:
        org_id = current["org_id"]
        needs_update = False
        if (
            desired.cycle_acu_limit is not None
            and desired.cycle_acu_limit != current.get("max_cycle_acu_limit")
        ):
            needs_update = True
        if (
            desired.session_acu_limit is not None
            and desired.session_acu_limit != current.get("max_session_acu_limit")
        ):
            needs_update = True

        if needs_update:
            if self.dry_run:
                self._record("update_org", desired.org_name, "dry-run")
            else:
                try:
                    self.client.update_organization(
                        org_id,
                        max_cycle_acu_limit=desired.cycle_acu_limit,
                        max_session_acu_limit=desired.session_acu_limit,
                    )
                    self._record("update_org", desired.org_name, "succeeded")
                except Exception as e:
                    self._record("update_org", desired.org_name, "failed", str(e))

        if (
            desired.cycle_acu_limit is not None
            and desired.cycle_acu_limit != current.get("max_cycle_acu_limit")
        ):
            if self.dry_run:
                self._record(
                    "set_acu_limit",
                    f"{desired.org_name} (cycle={desired.cycle_acu_limit})",
                    "dry-run",
                )
            else:
                try:
                    self.client.set_org_acu_limit(org_id, desired.cycle_acu_limit)
                    self._record(
                        "set_acu_limit",
                        f"{desired.org_name} (cycle={desired.cycle_acu_limit})",
                        "succeeded",
                    )
                except Exception as e:
                    self._record("set_acu_limit", desired.org_name, "failed", str(e))

        if desired.repos:
            current_repos = current.get("current_repos", [])
            if sorted(desired.repos) != current_repos:
                if self.dry_run:
                    self._record(
                        "set_git_permissions",
                        f"{desired.org_name} → {desired.repos}",
                        "dry-run",
                    )
                else:
                    try:
                        perms = [
                            {
                                "git_connection_id": self.github_connection_id,
                                "repo_path": repo,
                            }
                            for repo in desired.repos
                        ]
                        self.client.set_org_git_permissions(org_id, perms)
                        self._record(
                            "set_git_permissions",
                            f"{desired.org_name} → {desired.repos}",
                            "succeeded",
                        )
                    except Exception as e:
                        self._record(
                            "set_git_permissions",
                            desired.org_name,
                            "failed",
                            str(e),
                        )

    def _sync_invite_users(self, rows: list[UserRow]) -> None:
        by_role: dict[str, list[UserRow]] = defaultdict(list)
        for row in rows:
            role_id = (
                self._resolve_role(row.enterprise_role)
                if row.enterprise_role
                else "account_member"
            )
            by_role[role_id].append(row)

        for role_id, role_rows in by_role.items():
            emails = [r.email for r in role_rows]
            if self.dry_run:
                for email in emails:
                    self._record("invite_user", f"{email} (role={role_id})", "dry-run")
                continue
            try:
                self.client.bulk_invite_users(emails, role_id)
                for email in emails:
                    self._record(
                        "invite_user", f"{email} (role={role_id})", "succeeded"
                    )
            except Exception as e:
                for email in emails:
                    self._record("invite_user", email, "failed", str(e))
                continue

            for row in role_rows:
                if not row.org_name:
                    continue
                org_id = self.org_map.get(row.org_name)
                if not org_id:
                    self._record(
                        "assign_to_org",
                        f"{row.email} → {row.org_name}",
                        "failed",
                        f"Org '{row.org_name}' not found",
                    )
                    continue
                try:
                    found = self.client.list_users(email=row.email)
                    if not found:
                        self._record(
                            "assign_to_org",
                            f"{row.email} → {row.org_name}",
                            "failed",
                            "User not found after invite",
                        )
                        continue
                    user_id = found[0]["user_id"]
                    self.user_map[row.email] = user_id
                    org_role = row.org_role or "org_member"
                    self.client.assign_user_to_org(org_id, user_id, org_role)
                    self._record(
                        "assign_to_org",
                        f"{row.email} → {row.org_name} ({org_role})",
                        "succeeded",
                    )
                except Exception as e:
                    self._record(
                        "assign_to_org",
                        f"{row.email} → {row.org_name}",
                        "failed",
                        str(e),
                    )

    def _sync_update_enterprise_role(
        self, desired_row: UserRow, current_user: dict
    ) -> None:
        user_id = current_user["user_id"]
        new_role = self._resolve_role(desired_row.enterprise_role)
        if self.dry_run:
            self._record(
                "update_enterprise_role",
                f"{desired_row.email} → {new_role}",
                "dry-run",
            )
            return
        try:
            self.client.update_user_enterprise_role(user_id, new_role)
            self._record(
                "update_enterprise_role",
                f"{desired_row.email} → {new_role}",
                "succeeded",
            )
        except Exception as e:
            self._record(
                "update_enterprise_role", desired_row.email, "failed", str(e)
            )

    def _sync_add_to_org(self, row: UserRow, org_id: str) -> None:
        user_id = self.user_map.get(row.email)
        if not user_id:
            self._record(
                "assign_to_org",
                f"{row.email} → {row.org_name}",
                "failed",
                "User ID not found",
            )
            return
        org_role = row.org_role or "org_member"
        if self.dry_run:
            self._record(
                "assign_to_org",
                f"{row.email} → {row.org_name} ({org_role})",
                "dry-run",
            )
            return
        try:
            self.client.assign_user_to_org(org_id, user_id, org_role)
            self._record(
                "assign_to_org",
                f"{row.email} → {row.org_name} ({org_role})",
                "succeeded",
            )
        except Exception as e:
            self._record(
                "assign_to_org",
                f"{row.email} → {row.org_name}",
                "failed",
                str(e),
            )

    def _sync_update_org_role(
        self, row: UserRow, current_user: dict, org_id: str
    ) -> None:
        user_id = current_user["user_id"]
        new_role = row.org_role or "org_member"
        if self.dry_run:
            self._record(
                "update_org_role",
                f"{row.email} in {row.org_name} → {new_role}",
                "dry-run",
            )
            return
        try:
            self.client.update_user_org_role(org_id, user_id, new_role)
            self._record(
                "update_org_role",
                f"{row.email} in {row.org_name} → {new_role}",
                "succeeded",
            )
        except Exception as e:
            self._record(
                "update_org_role",
                f"{row.email} in {row.org_name}",
                "failed",
                str(e),
            )

    def _sync_remove_from_org(
        self, user: dict, org_id: str, org_name: str
    ) -> None:
        email = user["email"]
        user_id = user["user_id"]
        if self.dry_run:
            self._record("remove_from_org", f"{email} from {org_name}", "dry-run")
            return
        try:
            self.client.remove_user_from_org(org_id, user_id)
            self._record(
                "remove_from_org", f"{email} from {org_name}", "succeeded"
            )
        except Exception as e:
            self._record(
                "remove_from_org", f"{email} from {org_name}", "failed", str(e)
            )

    def _sync_remove_user(self, user: dict) -> None:
        email = user["email"]
        user_id = user["user_id"]
        if self.dry_run:
            self._record("remove_user", email, "dry-run")
            return
        try:
            self.client.delete_user(user_id)
            self._record("remove_user", email, "succeeded")
        except Exception as e:
            self._record("remove_user", email, "failed", str(e))

    def _sync_delete_org(self, org: dict) -> None:
        org_name = org["name"]
        org_id = org["org_id"]
        # SAFEGUARD (Layer 2)
        if not is_managed_org(org_name):
            self._record(
                "delete_org",
                org_name,
                "skipped",
                "Non-convention org — managed externally; deletion blocked",
            )
            return
        if self.dry_run:
            self._record("delete_org", org_name, "dry-run")
            return
        try:
            self.client.delete_organization(org_id)
            self._record("delete_org", org_name, "succeeded")
        except Exception as e:
            self._record("delete_org", org_name, "failed", str(e))

    # ------------------------------------------------------------------
    # Legacy action-based processing (backward compatibility)
    # ------------------------------------------------------------------
    def process_org_additions(self, org_rows: list[OrgRow]) -> None:
        for row in org_rows:
            if self.dry_run:
                self._record("create_org", row.org_name, "dry-run")
                continue
            try:
                resp = self.client.create_organization(
                    row.org_name,
                    max_cycle_acu_limit=row.cycle_acu_limit,
                    max_session_acu_limit=row.session_acu_limit,
                )
                new_org_id = resp["org_id"]
                self.org_map[row.org_name] = new_org_id
                self._record("create_org", row.org_name, "succeeded")

                if row.cycle_acu_limit is not None:
                    self.client.set_org_acu_limit(new_org_id, row.cycle_acu_limit)
                    self._record("set_acu_limit", row.org_name, "succeeded")

                if row.repos:
                    perms = [
                        {
                            "git_connection_id": self.github_connection_id,
                            "repo_path": repo,
                        }
                        for repo in row.repos
                    ]
                    self.client.set_org_git_permissions(new_org_id, perms)
                    self._record("set_git_permissions", row.org_name, "succeeded")
            except Exception as e:
                self._record("create_org", row.org_name, "failed", str(e))

    def process_org_updates(self, org_rows: list[OrgRow]) -> None:
        for row in org_rows:
            org_id = self.org_map.get(row.org_name)
            if not org_id:
                self._record(
                    "update_org",
                    row.org_name,
                    "failed",
                    f"Org '{row.org_name}' not found",
                )
                continue
            if self.dry_run:
                self._record("update_org", row.org_name, "dry-run")
                continue
            try:
                self.client.update_organization(
                    org_id,
                    max_cycle_acu_limit=row.cycle_acu_limit,
                    max_session_acu_limit=row.session_acu_limit,
                )
                self._record("update_org", row.org_name, "succeeded")

                if row.repos:
                    perms = [
                        {
                            "git_connection_id": self.github_connection_id,
                            "repo_path": repo,
                        }
                        for repo in row.repos
                    ]
                    self.client.set_org_git_permissions(org_id, perms)
                    self._record("set_git_permissions", row.org_name, "succeeded")
            except Exception as e:
                self._record("update_org", row.org_name, "failed", str(e))

    def process_org_removals(self, org_rows: list[OrgRow]) -> None:
        for row in org_rows:
            # SAFEGUARD (Layer 3)
            if not is_managed_org(row.org_name):
                self._record(
                    "delete_org",
                    row.org_name,
                    "skipped",
                    "Non-convention org — managed externally; deletion blocked",
                )
                continue
            org_id = self.org_map.get(row.org_name)
            if not org_id:
                self._record(
                    "delete_org",
                    row.org_name,
                    "failed",
                    f"Org '{row.org_name}' not found",
                )
                continue
            if self.dry_run:
                self._record("delete_org", row.org_name, "dry-run")
                continue
            try:
                self.client.delete_organization(org_id)
                del self.org_map[row.org_name]
                self._record("delete_org", row.org_name, "succeeded")
            except Exception as e:
                self._record("delete_org", row.org_name, "failed", str(e))

    def process_user_additions(self, user_rows: list[UserRow]) -> None:
        by_role: dict[str, list[UserRow]] = defaultdict(list)
        for row in user_rows:
            role_id = (
                self._resolve_role(row.enterprise_role)
                if row.enterprise_role
                else "account_member"
            )
            by_role[role_id].append(row)

        for role_id, rows in by_role.items():
            emails = [r.email for r in rows]
            if self.dry_run:
                for email in emails:
                    self._record("invite_user", email, "dry-run")
                continue
            try:
                self.client.bulk_invite_users(emails, role_id)
                for email in emails:
                    self._record("invite_user", email, "succeeded")
            except Exception as e:
                for email in emails:
                    self._record("invite_user", email, "failed", str(e))
                continue

            for row in rows:
                if not row.org_name:
                    continue
                org_id = self.org_map.get(row.org_name)
                if not org_id:
                    self._record(
                        "assign_to_org",
                        f"{row.email} → {row.org_name}",
                        "failed",
                        f"Org '{row.org_name}' not found",
                    )
                    continue
                try:
                    found = self.client.list_users(email=row.email)
                    if not found:
                        self._record(
                            "assign_to_org",
                            f"{row.email} → {row.org_name}",
                            "failed",
                            "User not found after invite",
                        )
                        continue
                    user_id = found[0]["user_id"]
                    self.user_map[row.email] = user_id
                    org_role = row.org_role or "org_member"
                    self.client.assign_user_to_org(org_id, user_id, org_role)
                    self._record(
                        "assign_to_org", f"{row.email} → {row.org_name}", "succeeded"
                    )
                except Exception as e:
                    self._record(
                        "assign_to_org",
                        f"{row.email} → {row.org_name}",
                        "failed",
                        str(e),
                    )

    def process_user_removals(self, user_rows: list[UserRow]) -> None:
        for row in user_rows:
            user_id = self.user_map.get(row.email)
            if not user_id:
                self._record(
                    "remove_user",
                    row.email,
                    "failed",
                    f"User '{row.email}' not found",
                )
                continue
            if self.dry_run:
                target = (
                    f"{row.email} from {row.org_name}"
                    if row.org_name
                    else row.email
                )
                self._record("remove_user", target, "dry-run")
                continue
            try:
                if row.org_name:
                    org_id = self.org_map.get(row.org_name)
                    if not org_id:
                        self._record(
                            "remove_user",
                            f"{row.email} from {row.org_name}",
                            "failed",
                            f"Org '{row.org_name}' not found",
                        )
                        continue
                    self.client.remove_user_from_org(org_id, user_id)
                    self._record(
                        "remove_from_org",
                        f"{row.email} from {row.org_name}",
                        "succeeded",
                    )
                else:
                    self.client.delete_user(user_id)
                    self._record("delete_user", row.email, "succeeded")
            except Exception as e:
                self._record("remove_user", row.email, "failed", str(e))

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def get_summary(self) -> dict[str, int]:
        total = len(self.results)
        succeeded = sum(1 for r in self.results if r["status"] == "succeeded")
        failed = sum(1 for r in self.results if r["status"] == "failed")
        dry_run = sum(1 for r in self.results if r["status"] == "dry-run")
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "dry_run": dry_run,
            "skipped": total - succeeded - failed - dry_run,
        }

    def get_results_csv(self) -> str:
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "operation",
                "target",
                "status",
                "error_message",
                "timestamp",
            ],
        )
        writer.writeheader()
        for r in self.results:
            writer.writerow(r)
        return output.getvalue()

    def print_summary(self) -> None:
        summary = self.get_summary()
        print("\n" + "=" * 60)
        print("BULK OPERATION SUMMARY")
        print("=" * 60)
        print(f"  Total operations:  {summary['total']}")
        print(f"  Succeeded:         {summary['succeeded']}")
        print(f"  Failed:            {summary['failed']}")
        if summary["dry_run"]:
            print(f"  Dry-run (planned): {summary['dry_run']}")
        print("=" * 60)
        if summary["failed"]:
            print("\nFailed operations:")
            for r in self.results:
                if r["status"] == "failed":
                    print(
                        f"  - {r['operation']} {r['target']}: {r['error_message']}"
                    )

    def print_diff(self, org_diff: OrgDiff, user_diff: UserDiff) -> None:
        """Print a human-readable diff summary."""
        print("\n" + "=" * 60)
        print("SYNC DIFF — Changes to apply")
        print("=" * 60)

        if org_diff.to_create:
            print(f"\n  Orgs to CREATE ({len(org_diff.to_create)}):")
            for row in org_diff.to_create:
                extra = []
                if row.cycle_acu_limit is not None:
                    extra.append(f"cycle_acu={row.cycle_acu_limit}")
                if row.session_acu_limit is not None:
                    extra.append(f"session_acu={row.session_acu_limit}")
                if row.repos:
                    extra.append(f"repos={row.repos}")
                print(
                    f"    + {row.org_name}"
                    + (f" ({', '.join(extra)})" if extra else "")
                )

        if org_diff.to_update:
            updates_needed = []
            for desired, current in org_diff.to_update:
                changes = []
                if (
                    desired.cycle_acu_limit is not None
                    and desired.cycle_acu_limit != current.get("max_cycle_acu_limit")
                ):
                    changes.append(
                        f"cycle_acu: {current.get('max_cycle_acu_limit')} → "
                        f"{desired.cycle_acu_limit}"
                    )
                if (
                    desired.session_acu_limit is not None
                    and desired.session_acu_limit
                    != current.get("max_session_acu_limit")
                ):
                    changes.append(
                        f"session_acu: {current.get('max_session_acu_limit')} → "
                        f"{desired.session_acu_limit}"
                    )
                if desired.repos:
                    current_repos = current.get("current_repos", [])
                    if sorted(desired.repos) != current_repos:
                        changes.append(
                            f"repos: {current_repos} → {sorted(desired.repos)}"
                        )
                if changes:
                    updates_needed.append((desired.org_name, changes))
            if updates_needed:
                print(f"\n  Orgs to UPDATE ({len(updates_needed)}):")
                for name, changes in updates_needed:
                    print(f"    ~ {name}: {', '.join(changes)}")

        if org_diff.to_delete:
            print(f"\n  Orgs to DELETE ({len(org_diff.to_delete)}):")
            for org in org_diff.to_delete:
                print(f"    - {org['name']} (id={org['org_id']})")

        if user_diff.to_invite:
            print(f"\n  Users to INVITE ({len(user_diff.to_invite)}):")
            for row in user_diff.to_invite[:20]:
                print(f"    + {row.email} (role={row.enterprise_role})")
            if len(user_diff.to_invite) > 20:
                print(f"    ... and {len(user_diff.to_invite) - 20} more")

        if user_diff.to_remove:
            print(f"\n  Users to REMOVE ({len(user_diff.to_remove)}):")
            for user in user_diff.to_remove[:20]:
                print(f"    - {user['email']}")
            if len(user_diff.to_remove) > 20:
                print(f"    ... and {len(user_diff.to_remove) - 20} more")

        if user_diff.role_changes:
            print(f"\n  Enterprise role CHANGES ({len(user_diff.role_changes)}):")
            for row, _user in user_diff.role_changes[:20]:
                print(f"    ~ {row.email} → {row.enterprise_role}")

        if user_diff.org_additions:
            print(f"\n  Org membership ADDITIONS ({len(user_diff.org_additions)}):")
            for row, _org_id in user_diff.org_additions[:20]:
                print(f"    + {row.email} → {row.org_name} ({row.org_role})")

        if user_diff.org_removals:
            print(f"\n  Org membership REMOVALS ({len(user_diff.org_removals)}):")
            for user, _org_id, org_name in user_diff.org_removals[:20]:
                print(f"    - {user['email']} from {org_name}")

        if user_diff.org_role_changes:
            print(f"\n  Org role CHANGES ({len(user_diff.org_role_changes)}):")
            for row, _user, _org_id in user_diff.org_role_changes[:20]:
                print(f"    ~ {row.email} in {row.org_name} → {row.org_role}")

        total = (
            len(org_diff.to_create)
            + len(org_diff.to_delete)
            + len(user_diff.to_invite)
            + len(user_diff.to_remove)
            + len(user_diff.role_changes)
            + len(user_diff.org_additions)
            + len(user_diff.org_removals)
            + len(user_diff.org_role_changes)
        )
        if total == 0:
            print("\n  No changes needed — enterprise state matches CSV.")
        print("=" * 60)

    # ------------------------------------------------------------------
    # Export / pull
    # ------------------------------------------------------------------
    def export_current_state(
        self, include_unmanaged_orgs: bool = False
    ) -> tuple[list[OrgRow], list[UserRow]]:
        """Build sync-format ``OrgRow`` / ``UserRow`` lists from current API state."""
        try:
            acu_entries = self.client.list_acu_limits()
        except Exception:
            acu_entries = []
        acu_by_org: dict[str, dict] = {
            entry["org_id"]: entry for entry in acu_entries if entry.get("org_id")
        }

        org_rows: list[OrgRow] = []
        for org in self.current_orgs:
            name = org.get("name", "")
            if not include_unmanaged_orgs and not is_managed_org(name):
                continue

            org_id = org.get("org_id", "")
            acu_entry = acu_by_org.get(org_id, {})
            cycle_limit = acu_entry.get("cycle_acu_limit")
            if cycle_limit is None:
                cycle_limit = org.get("max_cycle_acu_limit")
            session_limit = acu_entry.get("session_acu_limit")
            if session_limit is None:
                session_limit = org.get("max_session_acu_limit")

            repos: list[str] = []
            if is_managed_org(name) and org_id:
                try:
                    perms = self.client.list_org_git_permissions(org_id)
                    repos = sorted(
                        {p.get("repo_path", "") for p in perms if p.get("repo_path")}
                    )
                except Exception:
                    repos = []

            org_rows.append(
                OrgRow(
                    org_name=name,
                    action="sync",
                    cycle_acu_limit=cycle_limit if cycle_limit is not None else None,
                    session_acu_limit=(
                        session_limit if session_limit is not None else None
                    ),
                    repos=repos,
                )
            )

        org_rows.sort(key=lambda r: r.org_name.lower())

        org_id_to_name = {oid: name for name, oid in self.org_map.items()}

        user_rows: list[UserRow] = []
        for user in self.current_users:
            email = user.get("email", "")
            if not email:
                continue

            enterprise_role = ""
            managed_memberships: list[tuple[str, str]] = []

            for ra in user.get("role_assignments", []):
                role = ra.get("role", {}) or {}
                role_id = role.get("role_id", "")
                if ra.get("org_id") is None:
                    enterprise_role = role_id
                else:
                    org_id = ra.get("org_id", "")
                    org_name = org_id_to_name.get(org_id, "")
                    if not org_name or not is_managed_org(org_name):
                        continue
                    managed_memberships.append((org_name, role_id))

            if not enterprise_role:
                enterprise_role = "account_member"

            if not managed_memberships:
                user_rows.append(
                    UserRow(
                        email=email,
                        action="sync",
                        enterprise_role=enterprise_role,
                        org_name="",
                        org_role="",
                    )
                )
                continue

            for org_name, org_role in managed_memberships:
                user_rows.append(
                    UserRow(
                        email=email,
                        action="sync",
                        enterprise_role=enterprise_role,
                        org_name=org_name,
                        org_role=org_role,
                    )
                )

        user_rows.sort(key=lambda r: (r.email.lower(), r.org_name.lower()))
        return org_rows, user_rows

    def write_exported_state(
        self,
        orgs_out: str,
        users_out: str,
        include_unmanaged_orgs: bool = False,
    ) -> tuple[int, int]:
        """Pull current state and write sync-format CSVs to disk."""
        org_rows, user_rows = self.export_current_state(
            include_unmanaged_orgs=include_unmanaged_orgs
        )
        with open(orgs_out, "w", newline="", encoding="utf-8") as f:
            write_orgs_csv(org_rows, f)
        with open(users_out, "w", newline="", encoding="utf-8") as f:
            write_users_csv(user_rows, f)
        return len(org_rows), len(user_rows)
