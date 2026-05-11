"""Sync GitHub Team membership and repo access to Devin organizations.

Supports two modes:

- **Auto mode** (default): discovers all GitHub teams and auto-creates Devin orgs.
- **Legacy mode**: uses explicit ``team_mappings`` from ``config.yaml``.

Ported from github-permissions-devin-sync's ``sync.py`` from async/httpx to
synchronous/requests via :class:`user_management.core.DevinAPIClient`.  The
shape and behaviour are unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from user_management.core.client import DevinAPIClient
from user_management.core.models import (
    DevinGitConnection,
    DevinOrg,
    DevinUser,
    GitHubOrgConfig,
    GitHubTeam,
    GitPermissionCreateRequest,
    MemberSyncResult,
    OrgCreateResult,
    RepoSyncResult,
    SyncSummary,
)
from user_management.github_sync.config import load_state_file, save_state_file
from user_management.github_sync.github_client import GitHubClient

logger = logging.getLogger(__name__)


# ======================================================================
# Email resolution
# ======================================================================
_NUMERIC_LOCAL_RE = re.compile(r"^\d+$")


def _is_numeric_email(email: str) -> bool:
    """True if the local part of *email* is purely numeric.

    SAML nameIds like ``360600@cognizant.com`` are IDs, not real person
    emails; these are deprioritized in resolution.
    """
    local_part = email.rsplit("@", 1)[0]
    return bool(_NUMERIC_LOCAL_RE.match(local_part))


def build_email_lookup(enterprise_users: list[DevinUser]) -> dict[str, DevinUser]:
    """Build a mapping from email and name (lowercased) to Devin user.

    Email-based matches take priority over name-based matches.
    """
    mapping: dict[str, DevinUser] = {}
    for user in enterprise_users:
        if user.name:
            mapping[user.name.lower()] = user
    for user in enterprise_users:
        if user.email:
            mapping[user.email.lower()] = user
    return mapping


def resolve_gh_login_to_devin_user(
    login: str,
    email_lookup: dict[str, DevinUser],
    saml_map: dict[str, str],
    audit_map: dict[str, str],
    profile_emails: dict[str, str],
) -> Optional[DevinUser]:
    """Resolve a GitHub login to a Devin user using layered email sources.

    Priority:
      1. Public profile email (firstname.lastname — authoritative)
      2. Audit log invite email
      3. SAML nameId (only if non-numeric)
      4. Username/name fallback
      5. Numeric SAML nameId (last resort — keeps existing users matched)
    """
    login_lower = login.lower()

    candidate_emails: list[str] = []
    if login_lower in profile_emails and profile_emails[login_lower]:
        candidate_emails.append(profile_emails[login_lower].lower())
    if login_lower in audit_map:
        candidate_emails.append(audit_map[login_lower].lower())
    if login_lower in saml_map:
        saml_email = saml_map[login_lower].lower()
        if not _is_numeric_email(saml_email):
            candidate_emails.append(saml_email)

    for email in candidate_emails:
        if email in email_lookup:
            return email_lookup[email]

    if login_lower in email_lookup:
        return email_lookup[login_lower]

    if login_lower in saml_map:
        saml_email = saml_map[login_lower].lower()
        if _is_numeric_email(saml_email) and saml_email in email_lookup:
            return email_lookup[saml_email]

    return None


# ======================================================================
# Devin client wrappers — convert raw dicts to pydantic models
# ======================================================================
def _list_organizations(client: DevinAPIClient) -> list[DevinOrg]:
    return [
        DevinOrg(
            org_id=o["org_id"],
            name=o.get("name", ""),
            created_at=o.get("created_at"),
            max_cycle_acu_limit=o.get("max_cycle_acu_limit"),
            max_session_acu_limit=o.get("max_session_acu_limit"),
        )
        for o in client.list_organizations()
    ]


def _create_organization(client: DevinAPIClient, name: str) -> DevinOrg:
    data = client.create_organization(name)
    return DevinOrg(
        org_id=data["org_id"],
        name=data.get("name", name),
        created_at=data.get("created_at"),
        max_cycle_acu_limit=data.get("max_cycle_acu_limit"),
        max_session_acu_limit=data.get("max_session_acu_limit"),
    )


def _list_git_connections(client: DevinAPIClient) -> list[DevinGitConnection]:
    return [
        DevinGitConnection(
            git_connection_id=c["git_connection_id"],
            git_provider_type=c.get("git_provider_type", ""),
            name=c.get("name"),
            host=c.get("host", ""),
        )
        for c in client.list_git_connections()
    ]


def _list_enterprise_users(client: DevinAPIClient) -> list[DevinUser]:
    return [
        DevinUser(
            user_id=u["user_id"],
            email=u.get("email"),
            name=u.get("name"),
        )
        for u in client.list_users()
    ]


def _list_org_members(client: DevinAPIClient, org_id: str) -> list[DevinUser]:
    return [
        DevinUser(
            user_id=m["user_id"],
            email=m.get("email"),
            name=m.get("name"),
        )
        for m in client.list_org_members(org_id)
    ]


def _list_git_permissions(client: DevinAPIClient, org_id: str) -> list[dict]:
    return client.list_org_git_permissions(org_id)


def _replace_git_permissions(
    client: DevinAPIClient,
    org_id: str,
    permissions: list[GitPermissionCreateRequest],
) -> dict:
    payload = [p.model_dump(exclude_none=True) for p in permissions]
    return client.set_org_git_permissions(org_id, payload)


def _list_roles(client: DevinAPIClient) -> list[dict]:
    return client.list_roles()


# ======================================================================
# Connection discovery
# ======================================================================
def find_github_connection(
    connections: list[DevinGitConnection],
    github_org: Optional[str] = None,
) -> Optional[DevinGitConnection]:
    """Find the GitHub git connection from the enterprise connections list.

    When *github_org* is provided the connection whose ``name`` matches that
    org (case-insensitive) is required.  Otherwise falls back to the generic
    preference order (``github_app`` > ``github_token`` > ``github_individual_token``).
    """
    preference = ["github_app", "github_token", "github_individual_token"]
    github_connections = [
        c for c in connections if c.git_provider_type in preference
    ]
    if not github_connections:
        return None

    if github_org:
        for conn in github_connections:
            if conn.name and conn.name.lower() == github_org.lower():
                logger.info(
                    "Matched git connection '%s' (%s) to GitHub org '%s'",
                    conn.name,
                    conn.git_connection_id,
                    github_org,
                )
                return conn
        logger.error(
            "No git connection with name matching GitHub org '%s' found. "
            "Available connections: %s. Repo sync will be skipped.",
            github_org,
            [f"{c.name} ({c.git_connection_id})" for c in github_connections],
        )
        return None

    github_connections.sort(
        key=lambda c: preference.index(c.git_provider_type)
        if c.git_provider_type in preference
        else 99
    )
    return github_connections[0]


# ======================================================================
# Member sync
# ======================================================================
def sync_members_for_team(
    *,
    team_slug: str,
    org_id: str,
    github_client: GitHubClient,
    devin_client: DevinAPIClient,
    github_org: str,
    email_lookup: dict[str, DevinUser],
    saml_map: dict[str, str],
    audit_map: dict[str, str],
    profile_emails: dict[str, str],
    org_role_id: str,
    dry_run: bool,
) -> MemberSyncResult:
    """Sync GitHub team members to a Devin org's members."""
    result = MemberSyncResult(team_slug=team_slug, devin_org_id=org_id)

    try:
        gh_members = github_client.list_team_members(github_org, team_slug)
        logger.info("[%s] GitHub team has %d members", team_slug, len(gh_members))

        desired_user_ids: dict[str, str] = {}
        for member in gh_members:
            devin_user = resolve_gh_login_to_devin_user(
                member.login, email_lookup, saml_map, audit_map, profile_emails,
            )
            if devin_user:
                desired_user_ids[devin_user.user_id] = member.login
            else:
                result.users_skipped.append(member.login)
                logger.warning(
                    "[%s] Cannot resolve GitHub user '%s' to a Devin user",
                    team_slug,
                    member.login,
                )

        current_members = _list_org_members(devin_client, org_id)
        current_user_ids = {m.user_id for m in current_members}

        to_add = set(desired_user_ids.keys()) - current_user_ids
        to_remove = current_user_ids - set(desired_user_ids.keys())

        logger.info(
            "[%s] Members to add: %d, to remove: %d, already synced: %d",
            team_slug,
            len(to_add),
            len(to_remove),
            len(current_user_ids & set(desired_user_ids.keys())),
        )

        for user_id in sorted(to_add):
            gh_login = desired_user_ids[user_id]
            if dry_run:
                logger.info(
                    "[DRY RUN] Would add user %s (%s) to org %s",
                    gh_login,
                    user_id,
                    org_id,
                )
            else:
                try:
                    devin_client.assign_user_to_org(org_id, user_id, org_role_id)
                    logger.info("Added user %s (%s) to org %s", gh_login, user_id, org_id)
                except Exception as exc:
                    error_msg = f"Failed to add user {gh_login} ({user_id}): {exc}"
                    logger.error(error_msg)
                    result.errors.append(error_msg)
                    continue
            result.users_added.append(gh_login)

        for user_id in sorted(to_remove):
            display = user_id
            for m in current_members:
                if m.user_id == user_id:
                    display = m.email or m.name or user_id
                    break

            if dry_run:
                logger.info(
                    "[DRY RUN] Would remove user %s (%s) from org %s",
                    display,
                    user_id,
                    org_id,
                )
            else:
                try:
                    devin_client.remove_user_from_org(org_id, user_id)
                    logger.info("Removed user %s (%s) from org %s", display, user_id, org_id)
                except Exception as exc:
                    error_msg = f"Failed to remove user {display} ({user_id}): {exc}"
                    logger.error(error_msg)
                    result.errors.append(error_msg)
                    continue
            result.users_removed.append(display)

    except Exception as exc:
        error_msg = f"Member sync failed for {team_slug}: {exc}"
        logger.error(error_msg)
        result.errors.append(error_msg)

    return result


# ======================================================================
# Repo sync
# ======================================================================
def sync_repos_for_team(
    *,
    team_slug: str,
    org_id: str,
    github_client: GitHubClient,
    devin_client: DevinAPIClient,
    github_org: str,
    git_connection: DevinGitConnection,
    dry_run: bool,
) -> RepoSyncResult:
    """Sync GitHub team repos to Devin org git permissions."""
    result = RepoSyncResult(
        team_slug=team_slug,
        devin_org_id=org_id,
        git_connection_id=git_connection.git_connection_id,
    )

    try:
        gh_repos = github_client.list_team_repos(github_org, team_slug)
        logger.info(
            "[%s] GitHub team has access to %d repos", team_slug, len(gh_repos)
        )

        desired_permissions: list[GitPermissionCreateRequest] = []
        for repo in gh_repos:
            desired_permissions.append(
                GitPermissionCreateRequest(
                    git_connection_id=git_connection.git_connection_id,
                    repo_path=repo.full_name,
                )
            )
            result.repos_synced.append(repo.full_name)

        current_permissions = _list_git_permissions(devin_client, org_id)
        current_repo_paths = {
            p.get("repo_path") for p in current_permissions if p.get("repo_path")
        }
        desired_repo_paths = {p.repo_path for p in desired_permissions}

        added = desired_repo_paths - current_repo_paths
        removed = current_repo_paths - desired_repo_paths

        if added:
            logger.info("[%s] Repos to add: %s", team_slug, sorted(added))
        if removed:
            logger.info("[%s] Repos to remove: %s", team_slug, sorted(removed))
        if not added and not removed:
            logger.info("[%s] Repo permissions already in sync", team_slug)

        if dry_run:
            logger.info(
                "[DRY RUN] Would replace %d git permissions for org %s",
                len(desired_permissions),
                org_id,
            )
        else:
            if desired_permissions or current_permissions:
                _replace_git_permissions(devin_client, org_id, desired_permissions)
                logger.info(
                    "Replaced git permissions for org %s with %d entries",
                    org_id,
                    len(desired_permissions),
                )

    except Exception as exc:
        error_msg = f"Repo sync failed for {team_slug}: {exc}"
        logger.error(error_msg)
        result.errors.append(error_msg)

    return result


# ======================================================================
# Auto-sync orchestration
# ======================================================================
def run_auto_sync(
    org_config: GitHubOrgConfig,
    *,
    devin_client: DevinAPIClient,
    github_client: GitHubClient,
    default_member_role: str,
    dry_run: bool,
    config_path: str = "config.yaml",
) -> SyncSummary:
    """Auto-discover GitHub teams and sync them to Devin orgs."""
    summary = SyncSummary(dry_run=dry_run)

    # ── Phase 1: Discovery ──────────────────────────────────────────
    logger.info("Discovering GitHub teams in org '%s'...", org_config.github_org)
    all_teams = github_client.list_org_teams(org_config.github_org)

    teams: list[GitHubTeam] = []
    for team in all_teams:
        if org_config.team_filter and team.slug not in org_config.team_filter:
            logger.debug("Skipping team '%s' (not in team_filter)", team.slug)
            continue
        if org_config.skip_team_patterns and any(
            pattern in team.slug for pattern in org_config.skip_team_patterns
        ):
            logger.info(
                "Skipping team '%s' (matches skip_team_patterns)", team.slug,
            )
            continue
        teams.append(team)

    logger.info("Found %d teams to sync (of %d total)", len(teams), len(all_teams))

    logger.info("Fetching Devin organizations...")
    devin_orgs = _list_organizations(devin_client)
    devin_org_by_name: dict[str, DevinOrg] = {org.name: org for org in devin_orgs}
    devin_org_by_id: dict[str, DevinOrg] = {org.org_id: org for org in devin_orgs}
    logger.info("Found %d existing Devin orgs", len(devin_orgs))

    state_map = load_state_file(config_path, org_config.github_org)

    logger.info("Fetching Devin enterprise users...")
    enterprise_users = _list_enterprise_users(devin_client)
    logger.info("Found %d enterprise users", len(enterprise_users))

    logger.info("Fetching Devin git connections...")
    git_connections = _list_git_connections(devin_client)
    git_connection = find_github_connection(
        git_connections, github_org=org_config.github_org
    )
    if git_connection:
        logger.info(
            "Using git connection: %s (%s)",
            git_connection.git_connection_id,
            git_connection.git_provider_type,
        )
    else:
        logger.warning("No GitHub git connection found. Repo sync will be skipped.")

    logger.info("Fetching Devin roles...")
    roles = _list_roles(devin_client)
    org_member_role_id: Optional[str] = None
    enterprise_member_role_id: Optional[str] = None
    for role in roles:
        if role.get("role_type") == "org" and role.get("role_name", "").lower() == default_member_role:
            org_member_role_id = role["role_id"]
        if role.get("role_type") == "enterprise" and role.get("role_name", "").lower() == "member":
            enterprise_member_role_id = role["role_id"]
    if not org_member_role_id:
        logger.error(
            "No org role matching '%s' found. Member sync will fail.",
            default_member_role,
        )
    else:
        logger.info("Using org role: %s (%s)", default_member_role, org_member_role_id)

    email_lookup = build_email_lookup(enterprise_users)

    saml_map: dict[str, str] = {}
    if org_config.email_resolution.saml_graphql:
        logger.info("Fetching SAML identities...")
        saml_map = github_client.get_saml_identities(org_config.github_org)

    audit_map: dict[str, str] = {}
    if org_config.email_resolution.audit_log_invites:
        logger.info("Fetching audit log invite emails...")
        audit_map = github_client.get_audit_log_invite_emails(org_config.github_org)

    team_gh_members: dict[str, list] = {}
    team_gh_repos: dict[str, list] = {}
    for team in teams:
        team_gh_members[team.slug] = github_client.list_team_members(
            org_config.github_org, team.slug,
        )
        team_gh_repos[team.slug] = github_client.list_team_repos(
            org_config.github_org, team.slug,
        )

    allowed_domains = [
        d.lower().lstrip("@")
        for d in org_config.email_resolution.allowed_email_domains
    ]

    profile_emails: dict[str, str] = {}
    if org_config.email_resolution.public_profile:
        all_logins: set[str] = set()
        for team in teams:
            for m in team_gh_members.get(team.slug, []):
                all_logins.add(m.login)
        logger.info(
            "Fetching public profile emails for %d unique users...", len(all_logins)
        )
        for login in sorted(all_logins):
            email = github_client.get_user_profile_email(login)
            if email:
                if allowed_domains:
                    domain = email.lower().rsplit("@", 1)[-1]
                    if domain not in allowed_domains:
                        logger.debug(
                            "Skipping public profile email for %s (%s) — "
                            "domain not in allowed list",
                            login,
                            email,
                        )
                        continue
                profile_emails[login.lower()] = email

    # 1d. Replace numeric-email Devin users with real-name emails.
    all_logins_set: set[str] = set()
    for team in teams:
        for m in team_gh_members.get(team.slug, []):
            all_logins_set.add(m.login)

    if not org_config.auto_invite_members:
        logger.info("Skipping numeric-email cleanup (auto_invite_members is disabled)")
    else:
        for login in sorted(all_logins_set):
            login_lower = login.lower()
            real_email: Optional[str] = None
            if login_lower in profile_emails and profile_emails[login_lower]:
                pe = profile_emails[login_lower].lower()
                if not _is_numeric_email(pe):
                    real_email = pe
            if not real_email and login_lower in audit_map:
                ae = audit_map[login_lower].lower()
                if not _is_numeric_email(ae):
                    real_email = ae
            if not real_email:
                continue

            numeric_email: Optional[str] = None
            if login_lower in saml_map:
                se = saml_map[login_lower].lower()
                if _is_numeric_email(se) and se in email_lookup:
                    numeric_email = se
            if not numeric_email:
                continue

            old_user = email_lookup[numeric_email]
            if real_email in email_lookup:
                continue

            if dry_run:
                logger.info(
                    "[DRY RUN] Would delete numeric-email Devin user %s (%s) "
                    "for %s and re-invite with %s",
                    old_user.user_id, numeric_email, login, real_email,
                )
            else:
                logger.info(
                    "Deleting numeric-email Devin user %s (%s) for %s — "
                    "will re-invite with %s",
                    old_user.user_id, numeric_email, login, real_email,
                )
                try:
                    devin_client.delete_user(old_user.user_id)
                    enterprise_users[:] = [
                        u for u in enterprise_users if u.user_id != old_user.user_id
                    ]
                    email_lookup = build_email_lookup(enterprise_users)
                except Exception as exc:
                    logger.error(
                        "Failed to delete numeric-email user %s (%s): %s",
                        old_user.user_id,
                        numeric_email,
                        exc,
                    )

    # 1e. Auto-invite unmatched users as enterprise members.
    if org_config.auto_invite_members:
        emails_to_invite: set[str] = set()
        for login in all_logins_set:
            devin_user = resolve_gh_login_to_devin_user(
                login, email_lookup, saml_map, audit_map, profile_emails,
            )
            if devin_user:
                continue
            login_lower = login.lower()
            candidate_email: Optional[str] = None
            if login_lower in profile_emails and profile_emails[login_lower]:
                candidate_email = profile_emails[login_lower]
            elif login_lower in audit_map:
                ae = audit_map[login_lower]
                if not _is_numeric_email(ae):
                    candidate_email = ae
            elif login_lower in saml_map:
                se = saml_map[login_lower]
                if not _is_numeric_email(se):
                    candidate_email = se

            if candidate_email and allowed_domains:
                domain = candidate_email.lower().rsplit("@", 1)[-1]
                if domain not in allowed_domains:
                    logger.info(
                        "Skipping auto-invite for %s (%s) — domain not in allowed list",
                        login,
                        candidate_email,
                    )
                    candidate_email = None

            if candidate_email:
                emails_to_invite.add(candidate_email.lower())

        if emails_to_invite:
            if not enterprise_member_role_id:
                logger.error("Cannot auto-invite: no enterprise 'Member' role found")
            else:
                sorted_emails = sorted(emails_to_invite)
                if dry_run:
                    logger.info(
                        "[DRY RUN] Would invite %d users as enterprise members: %s",
                        len(sorted_emails),
                        sorted_emails,
                    )
                else:
                    logger.info(
                        "Inviting %d users as enterprise members: %s",
                        len(sorted_emails),
                        sorted_emails,
                    )
                    try:
                        devin_client.bulk_invite_users(
                            sorted_emails, enterprise_member_role_id,
                        )
                        # Refresh enterprise users to pick up the new IDs.
                        enterprise_users = _list_enterprise_users(devin_client)
                    except Exception as exc:
                        logger.error("Failed to invite enterprise users: %s", exc)
                    email_lookup = build_email_lookup(enterprise_users)

    # ── Phase 2: Reconcile ──────────────────────────────────────────
    desired_teams: dict[str, tuple[GitHubTeam, Optional[DevinOrg]]] = {}
    for team in teams:
        if org_config.skip_empty_teams:
            members = team_gh_members.get(team.slug, [])
            repos = team_gh_repos.get(team.slug, [])
            matched_count = 0
            for member in members:
                devin_user = resolve_gh_login_to_devin_user(
                    member.login, email_lookup, saml_map, audit_map, profile_emails,
                )
                if devin_user:
                    matched_count += 1
            if matched_count == 0 and len(repos) == 0:
                logger.info(
                    "Skipping team '%s' (0 matched members, 0 repos)", team.slug,
                )
                continue

        org_name = org_config.org_name_template.format(
            gh_org=org_config.github_org, team_slug=team.slug,
        )

        existing_org: Optional[DevinOrg] = None
        state_entry = state_map.get(team.slug)
        state_org_id = state_entry["org_id"] if state_entry else None
        if state_org_id and state_org_id in devin_org_by_id:
            existing_org = devin_org_by_id[state_org_id]
            if existing_org.name != org_name:
                logger.info(
                    "Team '%s': resolved via state file to org '%s' (%s) "
                    "(display name was changed from '%s')",
                    team.slug,
                    existing_org.name,
                    existing_org.org_id,
                    org_name,
                )
                summary.orgs_matched_by_state.append(
                    f"{team.slug}: '{org_name}' -> '{existing_org.name}' "
                    f"({existing_org.org_id})"
                )
        elif state_org_id and state_org_id not in devin_org_by_id:
            logger.warning(
                "Team '%s': state file references org %s but it no longer exists. "
                "Will create a new org or match by name.",
                team.slug,
                state_org_id,
            )

        if not existing_org and org_name in devin_org_by_name:
            existing_org = devin_org_by_name[org_name]
            logger.debug(
                "Team '%s': matched by name to org '%s' (%s)",
                team.slug,
                org_name,
                existing_org.org_id,
            )

        desired_teams[org_name] = (team, existing_org)

    orgs_to_create = [
        name for name, (_team, org) in desired_teams.items() if org is None
    ]

    state_org_ids = {entry["org_id"] for entry in state_map.values()}
    desired_org_ids = {
        org.org_id for _, (_, org) in desired_teams.items() if org is not None
    }
    prefix = org_config.org_name_template.format(
        gh_org=org_config.github_org, team_slug="",
    )
    for org_name, org in devin_org_by_name.items():
        if org_name.startswith(prefix) and org_name not in desired_teams:
            if org.org_id in desired_org_ids or org.org_id in state_org_ids:
                continue
            summary.orgs_stale.append(org_name)
            logger.warning(
                "Devin org '%s' has no matching GitHub team. NOT deleting.",
                org_name,
            )

    # ── Phase 3: Apply ──────────────────────────────────────────────
    updated_state_map: dict[str, dict[str, str]] = dict(state_map)
    for org_name in orgs_to_create:
        team, _ = desired_teams[org_name]
        if dry_run:
            logger.info("[DRY RUN] Would create Devin org: %s", org_name)
            summary.orgs_created.append(OrgCreateResult(org_name=org_name))
        else:
            try:
                new_org = _create_organization(devin_client, org_name)
                desired_teams[org_name] = (team, new_org)
                devin_org_by_name[org_name] = new_org
                devin_org_by_id[new_org.org_id] = new_org
                updated_state_map[team.slug] = {
                    "org_id": new_org.org_id,
                    "cached_org_name": new_org.name,
                }
                logger.info("Created Devin org: %s (%s)", org_name, new_org.org_id)
                summary.orgs_created.append(
                    OrgCreateResult(org_name=org_name, org_id=new_org.org_id)
                )
            except Exception as exc:
                error_msg = f"Failed to create org '{org_name}': {exc}"
                logger.error(error_msg)
                summary.orgs_created.append(
                    OrgCreateResult(org_name=org_name, error=error_msg)
                )
                continue

    for org_name, (team, org) in desired_teams.items():
        if org is not None:
            updated_state_map[team.slug] = {
                "org_id": org.org_id,
                "cached_org_name": org.name,
            }

    save_state_file(config_path, org_config.github_org, updated_state_map, dry_run=dry_run)

    for org_name, (team, devin_org) in desired_teams.items():
        if not devin_org:
            logger.warning(
                "Skipping team '%s' — Devin org '%s' not available",
                team.slug,
                org_name,
            )
            continue

        org_id = devin_org.org_id
        logger.info(
            "Syncing team '%s' -> Devin org '%s' (%s)",
            team.slug,
            devin_org.name,
            org_id,
        )

        if not org_member_role_id:
            summary.member_results.append(
                MemberSyncResult(
                    team_slug=team.slug,
                    devin_org_id=org_id,
                    errors=[
                        f"No org role matching '{default_member_role}' found — "
                        "skipping member sync"
                    ],
                )
            )
        else:
            summary.member_results.append(
                sync_members_for_team(
                    team_slug=team.slug,
                    org_id=org_id,
                    github_client=github_client,
                    devin_client=devin_client,
                    github_org=org_config.github_org,
                    email_lookup=email_lookup,
                    saml_map=saml_map,
                    audit_map=audit_map,
                    profile_emails=profile_emails,
                    org_role_id=org_member_role_id,
                    dry_run=dry_run,
                )
            )

        if git_connection:
            summary.repo_results.append(
                sync_repos_for_team(
                    team_slug=team.slug,
                    org_id=org_id,
                    github_client=github_client,
                    devin_client=devin_client,
                    github_org=org_config.github_org,
                    git_connection=git_connection,
                    dry_run=dry_run,
                )
            )
        else:
            summary.repo_results.append(
                RepoSyncResult(
                    team_slug=team.slug,
                    devin_org_id=org_id,
                    errors=["No GitHub git connection found — skipping repo sync"],
                )
            )

    return summary


# ======================================================================
# Legacy sync (explicit team_mappings)
# ======================================================================
def run_legacy_sync(
    org_config: GitHubOrgConfig,
    *,
    devin_client: DevinAPIClient,
    github_client: GitHubClient,
    default_member_role: str,
    dry_run: bool,
) -> SyncSummary:
    """Execute the legacy sync for explicit team mappings in the configuration."""
    summary = SyncSummary(dry_run=dry_run)

    enterprise_users: list[DevinUser] = []
    needs_members = any(m.sync_members for m in org_config.team_mappings)
    needs_repos = any(m.sync_repos for m in org_config.team_mappings)

    if needs_members:
        logger.info("Fetching Devin enterprise users...")
        enterprise_users = _list_enterprise_users(devin_client)
        logger.info("Found %d enterprise users", len(enterprise_users))

    git_connection: Optional[DevinGitConnection] = None
    if needs_repos:
        logger.info("Fetching Devin git connections...")
        git_connections = _list_git_connections(devin_client)
        git_connection = find_github_connection(
            git_connections, github_org=org_config.github_org
        )
        if git_connection is None:
            logger.error(
                "No GitHub git connection found in Devin. "
                "Repo sync will be skipped for all team mappings."
            )

    email_lookup = build_email_lookup(enterprise_users)

    org_member_role_id: Optional[str] = None
    if needs_members:
        logger.info("Fetching Devin roles...")
        roles = _list_roles(devin_client)
        for role in roles:
            if (
                role.get("role_type") == "org"
                and role.get("role_name", "").lower() == default_member_role
            ):
                org_member_role_id = role["role_id"]
                break
        if org_member_role_id:
            logger.info(
                "Using org role: %s (%s)", default_member_role, org_member_role_id
            )
        else:
            logger.error("No org role matching '%s' found.", default_member_role)

    for mapping in org_config.team_mappings:
        logger.info(
            "Processing team mapping: %s -> %s",
            mapping.github_team_slug,
            mapping.devin_org_id,
        )

        if mapping.sync_members:
            if not org_member_role_id:
                summary.member_results.append(
                    MemberSyncResult(
                        team_slug=mapping.github_team_slug,
                        devin_org_id=mapping.devin_org_id,
                        errors=[
                            f"No org role matching '{default_member_role}' found — "
                            "skipping member sync"
                        ],
                    )
                )
            else:
                summary.member_results.append(
                    sync_members_for_team(
                        team_slug=mapping.github_team_slug,
                        org_id=mapping.devin_org_id,
                        github_client=github_client,
                        devin_client=devin_client,
                        github_org=org_config.github_org,
                        email_lookup=email_lookup,
                        saml_map={},
                        audit_map={},
                        profile_emails={},
                        org_role_id=org_member_role_id,
                        dry_run=dry_run,
                    )
                )

        if mapping.sync_repos:
            if git_connection is None:
                summary.repo_results.append(
                    RepoSyncResult(
                        team_slug=mapping.github_team_slug,
                        devin_org_id=mapping.devin_org_id,
                        errors=[
                            "No GitHub git connection found — skipping repo sync"
                        ],
                    )
                )
            else:
                summary.repo_results.append(
                    sync_repos_for_team(
                        team_slug=mapping.github_team_slug,
                        org_id=mapping.devin_org_id,
                        github_client=github_client,
                        devin_client=devin_client,
                        github_org=org_config.github_org,
                        git_connection=git_connection,
                        dry_run=dry_run,
                    )
                )

    return summary


# ======================================================================
# Summary printing
# ======================================================================
def print_summary(summary: SyncSummary) -> None:
    """Print a human-readable summary of the sync run."""
    print("\n" + "=" * 60)
    if summary.dry_run:
        print("  SYNC SUMMARY (DRY RUN — no changes were made)")
    else:
        print("  SYNC SUMMARY")
    print("=" * 60)

    if summary.orgs_created:
        print("\n  Org Creation Results:")
        print("  " + "-" * 40)
        for r in summary.orgs_created:
            if r.error:
                print(f"  FAILED: {r.org_name} — {r.error}")
            elif r.org_id:
                print(f"  Created: {r.org_name} ({r.org_id})")
            else:
                print(f"  [DRY RUN] Would create: {r.org_name}")

    if summary.orgs_matched_by_state:
        print("\n  Orgs Matched by State File (rename-safe):")
        print("  " + "-" * 40)
        for desc in summary.orgs_matched_by_state:
            print(f"  {desc}")

    if summary.orgs_stale:
        print("\n  Stale Orgs (no matching GH team — NOT deleted):")
        print("  " + "-" * 40)
        for name in summary.orgs_stale:
            print(f"  WARNING: {name}")

    if summary.member_results:
        print("\n  Member Sync Results:")
        print("  " + "-" * 40)
        for r in summary.member_results:
            print(f"  Team: {r.team_slug} -> Org: {r.devin_org_id}")
            print(f"    Added:   {len(r.users_added)}")
            print(f"    Removed: {len(r.users_removed)}")
            print(f"    Skipped: {len(r.users_skipped)} (no matching Devin user)")
            if r.errors:
                print(f"    Errors:  {len(r.errors)}")
                for err in r.errors:
                    print(f"      - {err}")

    if summary.repo_results:
        print("\n  Repo Sync Results:")
        print("  " + "-" * 40)
        for r in summary.repo_results:
            print(f"  Team: {r.team_slug} -> Org: {r.devin_org_id}")
            print(f"    Repos synced: {len(r.repos_synced)}")
            if r.git_connection_id:
                print(f"    Git connection: {r.git_connection_id}")
            if r.errors:
                print(f"    Errors: {len(r.errors)}")
                for err in r.errors:
                    print(f"      - {err}")

    total_errors = (
        sum(len(r.errors) for r in summary.member_results)
        + sum(len(r.errors) for r in summary.repo_results)
        + sum(1 for r in summary.orgs_created if r.error)
    )
    print(f"\n  Total errors: {total_errors}")
    print("=" * 60 + "\n")
