"""Pydantic models for Devin v3 API resources and GitHub team responses.

These are the canonical types used by both modules.  The ``bulk`` module
mostly works with raw dicts returned by :class:`DevinAPIClient` (legacy from
devin-bulk-manager), but the ``github_sync`` module wraps them into the
pydantic models defined here for stronger typing.
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


# ---------------------------------------------------------------------------
# GitHub models (used by github_sync)
# ---------------------------------------------------------------------------


class GitHubUser(BaseModel):
    """Minimal representation of a GitHub team member."""

    login: str
    id: int
    email: Optional[str] = None


class GitHubTeam(BaseModel):
    """Minimal representation of a GitHub team."""

    slug: str
    name: str
    id: int
    team_type: str = ""


class GitHubRepo(BaseModel):
    """Minimal representation of a GitHub team repository."""

    full_name: str  # e.g. "myorg/myrepo"
    name: str
    private: bool = False


# ---------------------------------------------------------------------------
# github_sync config models
# ---------------------------------------------------------------------------


class TeamMapping(BaseModel):
    """Maps a single GitHub team to a Devin organization (legacy manual mode)."""

    github_team_slug: str
    devin_org_id: str
    sync_members: bool = True
    sync_repos: bool = True


class EmailResolutionConfig(BaseModel):
    """Controls which methods are used for resolving GitHub user emails."""

    saml_graphql: bool = True
    audit_log_invites: bool = True
    commit_history: bool = True
    public_profile: bool = True
    allowed_email_domains: list[str] = Field(default_factory=list)


class GitHubOrgConfig(BaseModel):
    """Per-GitHub-org settings used inside the top-level config."""

    github_org: str
    github_token_env_var: Optional[str] = None
    team_mappings: list[TeamMapping] = Field(default_factory=list)
    auto_invite_members: bool = False
    team_filter: list[str] = Field(default_factory=list)
    skip_team_patterns: list[str] = Field(default_factory=list)
    skip_enterprise_teams: bool = True
    skip_empty_teams: bool = True
    org_name_template: str = "{gh_org}-{team_slug}"
    email_resolution: EmailResolutionConfig = Field(default_factory=EmailResolutionConfig)

    @property
    def is_auto_mode(self) -> bool:
        """True when no explicit ``team_mappings`` are provided (auto-discover mode)."""
        return len(self.team_mappings) == 0


class SyncConfig(BaseModel):
    """Top-level github_sync config loaded from YAML.

    Supports both single-org shorthand and multi-org ``github_orgs`` lists.
    """

    github_orgs: list[GitHubOrgConfig] = Field(default_factory=list)

    # Single-org backward compatibility fields
    github_org: Optional[str] = None
    team_mappings: list[TeamMapping] = Field(default_factory=list)
    github_token_env_var: Optional[str] = None
    auto_invite_members: bool = False
    team_filter: list[str] = Field(default_factory=list)
    skip_team_patterns: list[str] = Field(default_factory=list)
    skip_enterprise_teams: bool = True
    skip_empty_teams: bool = True
    org_name_template: str = "{gh_org}-{team_slug}"
    email_resolution: EmailResolutionConfig = Field(default_factory=EmailResolutionConfig)

    default_member_role: str = "member"
    dry_run: bool = False

    @model_validator(mode="after")
    def _normalize_orgs(self) -> "SyncConfig":
        if not self.github_orgs and self.github_org:
            self.github_orgs = [
                GitHubOrgConfig(
                    github_org=self.github_org,
                    github_token_env_var=self.github_token_env_var,
                    team_mappings=self.team_mappings,
                    auto_invite_members=self.auto_invite_members,
                    team_filter=self.team_filter,
                    skip_team_patterns=self.skip_team_patterns,
                    skip_enterprise_teams=self.skip_enterprise_teams,
                    skip_empty_teams=self.skip_empty_teams,
                    org_name_template=self.org_name_template,
                    email_resolution=self.email_resolution,
                )
            ]
        if not self.github_orgs:
            raise ValueError(
                "At least one GitHub org must be configured via "
                "'github_orgs' list or 'github_org' field."
            )
        return self

    @property
    def is_auto_mode(self) -> bool:
        return all(org.is_auto_mode for org in self.github_orgs)


# ---------------------------------------------------------------------------
# github_sync result models
# ---------------------------------------------------------------------------


class MemberSyncResult(BaseModel):
    """Summary of a member sync operation for one team mapping."""

    team_slug: str
    devin_org_id: str
    users_added: list[str] = Field(default_factory=list)
    users_removed: list[str] = Field(default_factory=list)
    users_skipped: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RepoSyncResult(BaseModel):
    """Summary of a repo sync operation for one team mapping."""

    team_slug: str
    devin_org_id: str
    repos_synced: list[str] = Field(default_factory=list)
    git_connection_id: Optional[str] = None
    errors: list[str] = Field(default_factory=list)


class OrgCreateResult(BaseModel):
    """Summary of a Devin org creation."""

    org_name: str
    org_id: Optional[str] = None
    error: Optional[str] = None


class SyncSummary(BaseModel):
    """Overall summary of a full sync run."""

    orgs_created: list[OrgCreateResult] = Field(default_factory=list)
    orgs_stale: list[str] = Field(default_factory=list)
    orgs_matched_by_state: list[str] = Field(default_factory=list)
    member_results: list[MemberSyncResult] = Field(default_factory=list)
    repo_results: list[RepoSyncResult] = Field(default_factory=list)
    dry_run: bool = False


# Rebuild forward references so DevinRoleAssignment can reference DevinRole.
DevinRoleAssignment.model_rebuild()
