"""Pydantic models for Devin v3 API resources.

The ``bulk`` module mostly works with raw dicts returned by
:class:`DevinAPIClient` (legacy from devin-bulk-manager); these typed models
are provided as the canonical public types for callers that want stronger
typing.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Devin API models
# ---------------------------------------------------------------------------


class DevinRole(BaseModel):
    """Role definition from ``GET /v3/enterprise/roles``."""

    role_name: str
    role_id: str
    role_type: str  # "enterprise" or "org"


class DevinRoleAssignment(BaseModel):
    """A role assignment on a Devin user."""

    role: DevinRole
    org_id: Optional[str] = None


class DevinUser(BaseModel):
    """User object from ``GET /v3/enterprise/members/users``."""

    user_id: str
    email: Optional[str] = None
    name: Optional[str] = None
    role_assignments: list[DevinRoleAssignment] = Field(default_factory=list)


class DevinOrgMember(BaseModel):
    """User object from ``GET /v3/enterprise/organizations/{org_id}/members/users``."""

    user_id: str
    email: Optional[str] = None
    name: Optional[str] = None
    role_assignments: list[DevinRoleAssignment] = Field(default_factory=list)


class DevinOrg(BaseModel):
    """Organization from ``GET /v3/enterprise/organizations``."""

    org_id: str
    name: str
    created_at: Optional[int] = None
    max_cycle_acu_limit: Optional[int] = None
    max_session_acu_limit: Optional[int] = None


class DevinGitConnection(BaseModel):
    """Git connection from ``GET /v3/enterprise/git-providers/connections``."""

    git_connection_id: str
    git_provider_type: str
    name: Optional[str] = None
    host: str = ""


class DevinGitPermission(BaseModel):
    """Git permission from ``GET …/git-providers/permissions``."""

    git_permission_id: str
    git_connection_id: str
    repo_path: Optional[str] = None
    group_prefix: Optional[str] = None
    prefix_path: Optional[str] = None
    created_at: Optional[int] = None


class GitPermissionCreateRequest(BaseModel):
    """Single permission entry for the bulk create / replace endpoints."""

    git_connection_id: str
    repo_path: Optional[str] = None
    group_prefix: Optional[str] = None
    prefix_path: Optional[str] = None

    @model_validator(mode="after")
    def validate_exactly_one_path(self) -> "GitPermissionCreateRequest":
        provided = [
            f for f in [self.repo_path, self.group_prefix, self.prefix_path] if f is not None
        ]
        if len(provided) != 1:
            raise ValueError(
                "Exactly one of repo_path, group_prefix, or prefix_path must be provided"
            )
        return self


class GitPermissionBulkCreateRequest(BaseModel):
    """Request body for ``PUT …/git-providers/permissions``."""

    permissions: list[GitPermissionCreateRequest] = Field(..., max_length=200)


# Rebuild forward references so DevinRoleAssignment can reference DevinRole.
DevinRoleAssignment.model_rebuild()
