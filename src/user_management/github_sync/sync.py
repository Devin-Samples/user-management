"""Sync GitHub Team membership and repository access to Devin organizations.

All Devin API calls go through ``core.client.DevinAPIClient`` (synchronous,
requests-based) in line with the repo convention.  The ``DevinAPIClient``
returns raw dicts; thin wrappers convert them to the typed models defined in
``core.models`` where needed.

Supports two modes:
  - Auto mode (default): discovers all GitHub teams and auto-creates orgs.
  - Legacy mode: uses explicit ``team_mappings`` from config.yaml.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from user_management.core.client import DevinAPIClient
from user_management.core.models import (
    DevinGitConnection,
    DevinOrg,
    DevinUser,
)
from user_management.github_sync.config import (
    get_devin_api_base_url,
    get_devin_api_token,
    get_github_token,
    load_config,
    load_env,
)
from user_management.github_sync.github_client import GitHubClient
from user_management.github_sync.models import (
    GitHubOrgConfig,
    GitHubRepo,
    GitHubTeam,
    GitHubUser,
    MemberSyncResult,
    OrgCreateResult,
    RepoSyncResult,
    SyncSummary,
)

logger = logging.getLogger("github_sync")


# ======================================================================
# Helpers to convert DevinAPIClient dict responses to typed models
# ======================================================================


def _dicts_to_users(items: list[dict]) -> list[DevinUser]:
    return [DevinUser(**d) for d in items]


def _dicts_to_orgs(items: list[dict]) -> list[DevinOrg]:
    return [DevinOrg(**d) for d in items]


def _dicts_to_connections(items: list[dict]) -> list[DevinGitConnection]:
    return [DevinGitConnection(**d) for d in items]


# ======================================================================
# State file helpers
# ======================================================================


def _state_file_path(config_path: str) -> Path:
    """Derive the state file path from the config file location."""
    config_dir = Path(config_path).resolve().parent
    return config_dir / "sync-state.json"


def _normalize_org_mapping(raw_mapping: dict) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for slug, value in raw_mapping.items():
        if isinstance(value, str):
            mapping[slug] = {"org_id": value, "cached_org_name": ""}
        elif isinstance(value, dict) and "org_id" in value:
            mapping[slug] = value
        else:
            logger.warning(
                "Skipping malformed state entry for team '%s'", slug,
            )
    return mapping


def load_state_file(
    config_path: str,
    github_org: str,
) -> dict[str, dict[str, str]]:
    """Load team_slug -> {org_id, cached_org_name} for one GitHub org."""
    path = _state_file_path(config_path)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            raw = data.get(github_org, {}).get("team_org_map", {})
            if raw:
                mapping = _normalize_org_mapping(raw)
                logger.info(
                    "Loaded state file with %d mappings for '%s'",
                    len(mapping), github_org,
                )
                return mapping
        except Exception as exc:
            logger.warning("Failed to read state file %s: %s", path, exc)

    # Legacy per-org state file
    legacy = (
        Path(config_path).resolve().parent
        / f"sync-state-{github_org}.json"
    )
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text())
            raw = data.get("team_org_map", {})
            mapping = _normalize_org_mapping(raw)
            logger.info(
                "Loaded legacy state file with %d mappings from %s",
                len(mapping), legacy,
            )
            return mapping
        except Exception as exc:
            logger.warning(
                "Failed to read legacy state file %s: %s", legacy, exc,
            )

    logger.debug("No state file found for org '%s'", github_org)
    return {}


def save_state_file(
    config_path: str,
    github_org: str,
    team_org_map: dict[str, dict[str, str]],
    dry_run: bool = False,
) -> None:
    """Persist team_slug -> {org_id, cached_org_name} to the state file."""
    if dry_run:
        logger.info(
            "[DRY RUN] Would save state file with %d mappings for '%s'",
            len(team_org_map), github_org,
        )
        return
    path = _state_file_path(config_path)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            pass
    existing[github_org] = {"team_org_map": team_org_map}
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    logger.info(
        "Saved state file with %d mappings for '%s' to %s",
        len(team_org_map), github_org, path,
    )


# ======================================================================
# Email resolution
# ======================================================================

_NUMERIC_LOCAL_RE = re.compile(r"^\d+$")


def _is_numeric_email(email: str) -> bool:
    """Return True if the local part of *email* is purely numeric.

    Numeric SAML nameIds (e.g. ``123456@example.com``) are employee IDs,
    not real person emails.
    """
    local_part = email.rsplit("@", 1)[0]
    return bool(_NUMERIC_LOCAL_RE.match(local_part))


def build_email_lookup(
    enterprise_users: list[DevinUser],
) -> dict[str, DevinUser]:
    """Build email/name -> DevinUser lookup. Email takes priority."""
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
    """Resolve a GitHub login to a Devin user via layered email sources.

    Priority: profile email > audit log > non-numeric SAML > username
    fallback > numeric SAML (last resort).
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

    # Last resort: numeric SAML so existing users aren't dropped.
    if login_lower in saml_map:
        saml_email = saml_map[login_lower].lower()
        if _is_numeric_email(saml_email) and saml_email in email_lookup:
            return email_lookup[saml_email]

    return None


# ======================================================================
# Member sync
# ======================================================================


def sync_members_for_team(
    *,
    team_slug: str,
    org_id: str,
    github_client: GitHubClient,
    client: DevinAPIClient,
    github_org: str,
    email_lookup: dict[str, DevinUser],
    saml_map: dict[str, str],
    audit_map: dict[str, str],
    profile_emails: dict[str, str],
    org_role_id: str,
    dry_run: bool,
    prefetched_members: Optional[list[GitHubUser]] = None,
) -> MemberSyncResult:
    """Sync GitHub team members to a Devin org's members."""
    result = MemberSyncResult(team_slug=team_slug, devin_org_id=org_id)

    try:
        gh_members = (
            prefetched_members
            if prefetched_members is not None
            else github_client.list_team_members(github_org, team_slug)
        )
        logger.info(
            "[%s] GitHub team has %d members", team_slug, len(gh_members),
        )

        desired_user_ids: dict[str, str] = {}
        for member in gh_members:
            devin_user = resolve_gh_login_to_devin_user(
                member.login, email_lookup, saml_map,
                audit_map, profile_emails,
            )
            if devin_user:
                desired_user_ids[devin_user.user_id] = member.login
            else:
                result.users_skipped.append(member.login)
                logger.warning(
                    "[%s] Cannot resolve GitHub user '%s' to a Devin user",
                    team_slug, member.login,
                )

        current_members = client.list_org_members(org_id)
        current_user_ids = {m["user_id"] for m in current_members}

        to_add = set(desired_user_ids.keys()) - current_user_ids
        to_remove = current_user_ids - set(desired_user_ids.keys())

        logger.info(
            "[%s] Members to add: %d, to remove: %d, already synced: %d",
            team_slug, len(to_add), len(to_remove),
            len(current_user_ids & set(desired_user_ids.keys())),
        )

        for user_id in sorted(to_add):
            gh_login = desired_user_ids[user_id]
            if dry_run:
                logger.info(
                    "[DRY RUN] Would add user %s (%s) to org %s",
                    gh_login, user_id, org_id,
                )
            else:
                try:
                    client.assign_user_to_org(org_id, user_id, org_role_id)
                    logger.info(
                        "Added user %s (%s) to org %s",
                        gh_login, user_id, org_id,
                    )
                except Exception as exc:
                    error_msg = (
                        f"Failed to add user {gh_login} ({user_id}): {exc}"
                    )
                    logger.error(error_msg)
                    result.errors.append(error_msg)
                    continue
            result.users_added.append(gh_login)

        for user_id in sorted(to_remove):
            display = user_id
            for m in current_members:
                if m["user_id"] == user_id:
                    display = (
                        m.get("email") or m.get("name") or user_id
                    )
                    break
            if dry_run:
                logger.info(
                    "[DRY RUN] Would remove user %s (%s) from org %s",
                    display, user_id, org_id,
                )
            else:
                try:
                    client.remove_user_from_org(org_id, user_id)
                    logger.info(
                        "Removed user %s (%s) from org %s",
                        display, user_id, org_id,
                    )
                except Exception as exc:
                    error_msg = (
                        f"Failed to remove user {display} ({user_id}): "
                        f"{exc}"
                    )
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


def find_github_connection(
    connections: list[DevinGitConnection],
    github_org: Optional[str] = None,
) -> Optional[DevinGitConnection]:
    """Find the GitHub git connection, optionally matching by org name."""
    preference = [
        "github_app", "github_token", "github_individual_token",
    ]
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
                    conn.name, conn.git_connection_id, github_org,
                )
                return conn
        logger.error(
            "No git connection matching GitHub org '%s' found. "
            "Available: %s. Repo sync will be skipped.",
            github_org,
            [
                f"{c.name} ({c.git_connection_id})"
                for c in github_connections
            ],
        )
        return None

    github_connections.sort(
        key=lambda c: (
            preference.index(c.git_provider_type)
            if c.git_provider_type in preference
            else 99
        )
    )
    return github_connections[0]


def sync_repos_for_team(
    *,
    team_slug: str,
    org_id: str,
    github_client: GitHubClient,
    client: DevinAPIClient,
    github_org: str,
    git_connection: DevinGitConnection,
    dry_run: bool,
    prefetched_repos: Optional[list[GitHubRepo]] = None,
) -> RepoSyncResult:
    """Sync GitHub team repos to Devin org git permissions."""
    result = RepoSyncResult(
        team_slug=team_slug,
        devin_org_id=org_id,
        git_connection_id=git_connection.git_connection_id,
    )

    try:
        gh_repos = (
            prefetched_repos
            if prefetched_repos is not None
            else github_client.list_team_repos(github_org, team_slug)
        )
        logger.info(
            "[%s] GitHub team has access to %d repos",
            team_slug, len(gh_repos),
        )

        desired_permissions: list[dict] = []
        for repo in gh_repos:
            desired_permissions.append({
                "git_connection_id": git_connection.git_connection_id,
                "repo_path": repo.full_name,
            })
            result.repos_synced.append(repo.full_name)

        current_permissions = client.list_git_permissions(org_id)
        current_repo_paths = {
            p.get("repo_path") for p in current_permissions if p.get("repo_path")
        }
        desired_repo_paths = {
            p["repo_path"] for p in desired_permissions
        }

        added = desired_repo_paths - current_repo_paths
        removed = current_repo_paths - desired_repo_paths

        if added:
            logger.info("[%s] Repos to add: %s", team_slug, sorted(added))
        if removed:
            logger.info(
                "[%s] Repos to remove: %s", team_slug, sorted(removed),
            )
        if not added and not removed:
            logger.info("[%s] Repo permissions already in sync", team_slug)

        if dry_run:
            logger.info(
                "[DRY RUN] Would replace %d git permissions for org %s",
                len(desired_permissions), org_id,
            )
        else:
            if desired_permissions or current_permissions:
                client.replace_git_permissions(org_id, desired_permissions)
                logger.info(
                    "Replaced git permissions for org %s with %d entries",
                    org_id, len(desired_permissions),
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
    default_member_role: str,
    dry_run: bool,
    client: DevinAPIClient,
    config_path: str = "config.yaml",
) -> SyncSummary:
    """Auto-discover GitHub teams and sync to Devin orgs."""
    summary = SyncSummary(dry_run=dry_run)

    token_env_var = org_config.github_token_env_var or "GITHUB_TOKEN"
    github_client = GitHubClient(token=get_github_token(token_env_var))

    # Phase 1: Discovery
    logger.info(
        "Discovering GitHub teams in org '%s'...", org_config.github_org,
    )
    all_teams = github_client.list_org_teams(org_config.github_org)

    teams: list[GitHubTeam] = []
    for team in all_teams:
        if (
            org_config.team_filter
            and team.slug not in org_config.team_filter
        ):
            logger.debug(
                "Skipping team '%s' (not in team_filter)", team.slug,
            )
            continue
        if org_config.skip_enterprise_teams and team.team_type == "secret":
            logger.info(
                "Skipping enterprise team '%s'", team.slug,
            )
            continue
        if org_config.skip_team_patterns and any(
            pat in team.slug for pat in org_config.skip_team_patterns
        ):
            logger.info(
                "Skipping team '%s' (matches skip_team_patterns)",
                team.slug,
            )
            continue
        teams.append(team)

    logger.info(
        "Found %d teams to sync (of %d total)", len(teams), len(all_teams),
    )

    # Fetch Devin state
    logger.info("Fetching Devin organizations...")
    devin_orgs = _dicts_to_orgs(client.list_organizations())
    devin_org_by_name: dict[str, DevinOrg] = {
        org.name: org for org in devin_orgs
    }
    devin_org_by_id: dict[str, DevinOrg] = {
        org.org_id: org for org in devin_orgs
    }
    logger.info("Found %d existing Devin orgs", len(devin_orgs))

    state_map = load_state_file(config_path, org_config.github_org)

    logger.info("Fetching Devin enterprise users...")
    enterprise_users = _dicts_to_users(client.list_users())
    logger.info("Found %d enterprise users", len(enterprise_users))

    logger.info("Fetching Devin git connections...")
    git_connections = _dicts_to_connections(client.list_git_connections())
    git_connection = find_github_connection(
        git_connections, github_org=org_config.github_org,
    )
    if git_connection:
        logger.info(
            "Using git connection: %s (%s)",
            git_connection.git_connection_id,
            git_connection.git_provider_type,
        )
    else:
        logger.warning(
            "No GitHub git connection found. Repo sync will be skipped.",
        )

    # Resolve roles
    logger.info("Fetching Devin roles...")
    roles = client.list_roles()
    org_member_role_id: Optional[str] = None
    enterprise_member_role_id: Optional[str] = None
    for role in roles:
        if (
            role.get("role_type") == "org"
            and role.get("role_name", "").lower() == default_member_role
        ):
            org_member_role_id = role["role_id"]
        if (
            role.get("role_type") == "enterprise"
            and role.get("role_name", "").lower() == "member"
        ):
            enterprise_member_role_id = role["role_id"]
    if not org_member_role_id:
        logger.error(
            "No org role matching '%s' found. Member sync will fail.",
            default_member_role,
        )
    else:
        logger.info(
            "Using org role: %s (%s)",
            default_member_role, org_member_role_id,
        )

    # Resolve emails
    email_lookup = build_email_lookup(enterprise_users)

    saml_map: dict[str, str] = {}
    if org_config.email_resolution.saml_graphql:
        logger.info("Fetching SAML identities...")
        saml_map = github_client.get_saml_identities(org_config.github_org)

    audit_map: dict[str, str] = {}
    if org_config.email_resolution.audit_log_invites:
        logger.info("Fetching audit log invite emails...")
        audit_map = github_client.get_audit_log_invite_emails(
            org_config.github_org,
        )

    # Pre-fetch team data
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
            "Fetching public profile emails for %d unique users...",
            len(all_logins),
        )
        for login in sorted(all_logins):
            email = github_client.get_user_profile_email(login)
            if email:
                if allowed_domains:
                    domain = email.lower().rsplit("@", 1)[-1]
                    if domain not in allowed_domains:
                        logger.debug(
                            "Skipping profile email for %s (%s) "
                            "— domain not allowed",
                            login, email,
                        )
                        continue
                profile_emails[login.lower()] = email

    # Numeric-email cleanup
    all_logins_set: set[str] = set()
    for team in teams:
        for m in team_gh_members.get(team.slug, []):
            all_logins_set.add(m.login)

    if not org_config.auto_invite_members:
        logger.info(
            "Skipping numeric-email cleanup "
            "(auto_invite_members is disabled)",
        )
    else:
        for login in sorted(all_logins_set):
            login_lower = login.lower()
            real_email: Optional[str] = None
            if (
                login_lower in profile_emails
                and profile_emails[login_lower]
            ):
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
                    "[DRY RUN] Would delete numeric-email user %s (%s) "
                    "for %s and re-invite with %s",
                    old_user.user_id, numeric_email, login, real_email,
                )
            else:
                logger.info(
                    "Deleting numeric-email user %s (%s) for %s "
                    "— will re-invite with %s",
                    old_user.user_id, numeric_email, login, real_email,
                )
                try:
                    client.delete_user(old_user.user_id)
                    enterprise_users[:] = [
                        u
                        for u in enterprise_users
                        if u.user_id != old_user.user_id
                    ]
                    email_lookup = build_email_lookup(enterprise_users)
                except Exception as exc:
                    logger.error(
                        "Failed to delete numeric-email user %s (%s): %s",
                        old_user.user_id, numeric_email, exc,
                    )

    # Auto-invite unmatched users
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
            if (
                login_lower in profile_emails
                and profile_emails[login_lower]
            ):
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
                        "Skipping auto-invite for %s (%s) "
                        "— domain not allowed",
                        login, candidate_email,
                    )
                    candidate_email = None

            if candidate_email:
                emails_to_invite.add(candidate_email.lower())

        if emails_to_invite:
            if not enterprise_member_role_id:
                logger.error(
                    "Cannot auto-invite: no enterprise 'Member' role found",
                )
            else:
                sorted_emails = sorted(emails_to_invite)
                if dry_run:
                    logger.info(
                        "[DRY RUN] Would invite %d users: %s",
                        len(sorted_emails), sorted_emails,
                    )
                else:
                    logger.info(
                        "Inviting %d users as enterprise members: %s",
                        len(sorted_emails), sorted_emails,
                    )
                    try:
                        client.bulk_invite_users(
                            sorted_emails, enterprise_member_role_id,
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to invite enterprise users: %s", exc,
                        )

                    # Refresh lookup
                    enterprise_users = _dicts_to_users(client.list_users())
                    email_lookup = build_email_lookup(enterprise_users)

    # Phase 2: Reconcile
    desired_teams: dict[str, tuple[GitHubTeam, Optional[DevinOrg]]] = {}
    for team in teams:
        if org_config.skip_empty_teams:
            members = team_gh_members.get(team.slug, [])
            repos = team_gh_repos.get(team.slug, [])
            matched_count = 0
            for member in members:
                dv = resolve_gh_login_to_devin_user(
                    member.login, email_lookup, saml_map,
                    audit_map, profile_emails,
                )
                if dv:
                    matched_count += 1
            if matched_count == 0 and len(repos) == 0:
                logger.info(
                    "Skipping team '%s' (0 matched members, 0 repos)",
                    team.slug,
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
                    "Team '%s': resolved via state file to org '%s' (%s)"
                    " (display name was changed from '%s')",
                    team.slug, existing_org.name,
                    existing_org.org_id, org_name,
                )
                summary.orgs_matched_by_state.append(
                    f"{team.slug}: '{org_name}' -> "
                    f"'{existing_org.name}' ({existing_org.org_id})"
                )
        elif state_org_id and state_org_id not in devin_org_by_id:
            logger.warning(
                "Team '%s': state file references org %s but it no "
                "longer exists. Will create or match by name.",
                team.slug, state_org_id,
            )

        if not existing_org and org_name in devin_org_by_name:
            existing_org = devin_org_by_name[org_name]

        desired_teams[org_name] = (team, existing_org)

    orgs_to_create = [
        name
        for name, (_, org) in desired_teams.items()
        if org is None
    ]

    state_org_ids = {entry["org_id"] for entry in state_map.values()}
    desired_org_ids = {
        org.org_id
        for _, (_, org) in desired_teams.items()
        if org is not None
    }
    prefix = org_config.org_name_template.format(
        gh_org=org_config.github_org, team_slug="",
    )
    for oname, org in devin_org_by_name.items():
        if oname.startswith(prefix) and oname not in desired_teams:
            if org.org_id in desired_org_ids or org.org_id in state_org_ids:
                continue
            summary.orgs_stale.append(oname)
            logger.warning(
                "Devin org '%s' has no matching GitHub team. NOT deleting.",
                oname,
            )

    # Phase 3: Apply
    updated_state_map: dict[str, dict[str, str]] = dict(state_map)
    for org_name in orgs_to_create:
        team, _ = desired_teams[org_name]
        if dry_run:
            logger.info("[DRY RUN] Would create Devin org: %s", org_name)
            summary.orgs_created.append(
                OrgCreateResult(org_name=org_name),
            )
        else:
            try:
                raw = client.create_organization(name=org_name)
                new_org = DevinOrg(**raw)
                desired_teams[org_name] = (team, new_org)
                devin_org_by_name[org_name] = new_org
                devin_org_by_id[new_org.org_id] = new_org
                updated_state_map[team.slug] = {
                    "org_id": new_org.org_id,
                    "cached_org_name": new_org.name,
                }
                logger.info(
                    "Created Devin org: %s (%s)",
                    org_name, new_org.org_id,
                )
                summary.orgs_created.append(
                    OrgCreateResult(
                        org_name=org_name, org_id=new_org.org_id,
                    ),
                )
            except Exception as exc:
                error_msg = f"Failed to create org '{org_name}': {exc}"
                logger.error(error_msg)
                summary.orgs_created.append(
                    OrgCreateResult(org_name=org_name, error=error_msg),
                )
                continue

    for org_name, (team, org) in desired_teams.items():
        if org is not None:
            updated_state_map[team.slug] = {
                "org_id": org.org_id,
                "cached_org_name": org.name,
            }

    save_state_file(
        config_path, org_config.github_org,
        updated_state_map, dry_run=dry_run,
    )

    # Sync members and repos per team
    for org_name, (team, devin_org) in desired_teams.items():
        if not devin_org:
            logger.warning(
                "Skipping team '%s' — org '%s' not available",
                team.slug, org_name,
            )
            continue

        org_id = devin_org.org_id
        logger.info(
            "Syncing team '%s' -> org '%s' (%s)",
            team.slug, devin_org.name, org_id,
        )

        if not org_member_role_id:
            summary.member_results.append(
                MemberSyncResult(
                    team_slug=team.slug,
                    devin_org_id=org_id,
                    errors=[
                        f"No org role matching '{default_member_role}' "
                        "found — skipping member sync"
                    ],
                ),
            )
        else:
            member_result = sync_members_for_team(
                team_slug=team.slug,
                org_id=org_id,
                github_client=github_client,
                client=client,
                github_org=org_config.github_org,
                email_lookup=email_lookup,
                saml_map=saml_map,
                audit_map=audit_map,
                profile_emails=profile_emails,
                org_role_id=org_member_role_id,
                dry_run=dry_run,
                prefetched_members=team_gh_members.get(team.slug),
            )
            summary.member_results.append(member_result)

        if git_connection:
            repo_result = sync_repos_for_team(
                team_slug=team.slug,
                org_id=org_id,
                github_client=github_client,
                client=client,
                github_org=org_config.github_org,
                git_connection=git_connection,
                dry_run=dry_run,
                prefetched_repos=team_gh_repos.get(team.slug),
            )
            summary.repo_results.append(repo_result)
        else:
            summary.repo_results.append(
                RepoSyncResult(
                    team_slug=team.slug,
                    devin_org_id=org_id,
                    errors=[
                        "No GitHub git connection found "
                        "— skipping repo sync"
                    ],
                ),
            )

    return summary


# ======================================================================
# Legacy sync orchestration (explicit team_mappings)
# ======================================================================


def run_legacy_sync(
    org_config: GitHubOrgConfig,
    default_member_role: str,
    dry_run: bool,
    client: DevinAPIClient,
) -> SyncSummary:
    """Execute the legacy sync for explicit team mappings."""
    summary = SyncSummary(dry_run=dry_run)

    token_env_var = org_config.github_token_env_var or "GITHUB_TOKEN"
    github_client = GitHubClient(token=get_github_token(token_env_var))

    enterprise_users: list[DevinUser] = []
    needs_members = any(
        m.sync_members for m in org_config.team_mappings
    )
    needs_repos = any(m.sync_repos for m in org_config.team_mappings)

    if needs_members:
        logger.info("Fetching Devin enterprise users...")
        enterprise_users = _dicts_to_users(client.list_users())
        logger.info("Found %d enterprise users", len(enterprise_users))

    git_connection: Optional[DevinGitConnection] = None
    if needs_repos:
        logger.info("Fetching Devin git connections...")
        git_connections = _dicts_to_connections(
            client.list_git_connections(),
        )
        git_connection = find_github_connection(
            git_connections, github_org=org_config.github_org,
        )
        if git_connection is None:
            logger.error(
                "No GitHub git connection found. "
                "Repo sync will be skipped.",
            )

    email_lookup = build_email_lookup(enterprise_users)

    org_member_role_id: Optional[str] = None
    if needs_members:
        logger.info("Fetching Devin roles...")
        roles = client.list_roles()
        for role in roles:
            if (
                role.get("role_type") == "org"
                and role.get("role_name", "").lower()
                == default_member_role
            ):
                org_member_role_id = role["role_id"]
                break
        if org_member_role_id:
            logger.info(
                "Using org role: %s (%s)",
                default_member_role, org_member_role_id,
            )
        else:
            logger.error(
                "No org role matching '%s' found.", default_member_role,
            )

    for mapping in org_config.team_mappings:
        logger.info(
            "Processing team mapping: %s -> %s",
            mapping.github_team_slug, mapping.devin_org_id,
        )

        if mapping.sync_members:
            if not org_member_role_id:
                summary.member_results.append(
                    MemberSyncResult(
                        team_slug=mapping.github_team_slug,
                        devin_org_id=mapping.devin_org_id,
                        errors=[
                            f"No org role matching "
                            f"'{default_member_role}' found"
                        ],
                    ),
                )
            else:
                member_result = sync_members_for_team(
                    team_slug=mapping.github_team_slug,
                    org_id=mapping.devin_org_id,
                    github_client=github_client,
                    client=client,
                    github_org=org_config.github_org,
                    email_lookup=email_lookup,
                    saml_map={},
                    audit_map={},
                    profile_emails={},
                    org_role_id=org_member_role_id,
                    dry_run=dry_run,
                )
                summary.member_results.append(member_result)

        if mapping.sync_repos:
            if git_connection is None:
                summary.repo_results.append(
                    RepoSyncResult(
                        team_slug=mapping.github_team_slug,
                        devin_org_id=mapping.devin_org_id,
                        errors=[
                            "No GitHub git connection found "
                            "— skipping repo sync"
                        ],
                    ),
                )
            else:
                repo_result = sync_repos_for_team(
                    team_slug=mapping.github_team_slug,
                    org_id=mapping.devin_org_id,
                    github_client=github_client,
                    client=client,
                    github_org=org_config.github_org,
                    git_connection=git_connection,
                    dry_run=dry_run,
                )
                summary.repo_results.append(repo_result)

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
            print(
                f"    Skipped: {len(r.users_skipped)} "
                "(no matching Devin user)"
            )
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


# ======================================================================
# CLI entry point
# ======================================================================


def run_sync(
    config_path: str = "config.yaml",
    dry_run_flag: bool = False,
    verbose: bool = False,
) -> int:
    """Run the GitHub team sync. Returns 0 on success, 1 on errors."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    load_env()
    config = load_config(config_path)

    api_token = get_devin_api_token()
    client = DevinAPIClient(
        api_key=api_token,
        base_url=get_devin_api_base_url(),
    )

    dry_run = dry_run_flag or config.dry_run
    if dry_run:
        logger.info("Running in DRY RUN mode — no changes will be made")

    all_summaries: list[SyncSummary] = []
    for org_config in config.github_orgs:
        logger.info("=" * 60)
        logger.info("Processing GitHub org: %s", org_config.github_org)
        logger.info("=" * 60)

        if org_config.is_auto_mode:
            logger.info(
                "Running in AUTO mode (discovering teams)",
            )
            summary = run_auto_sync(
                org_config,
                default_member_role=config.default_member_role,
                dry_run=dry_run,
                client=client,
                config_path=config_path,
            )
        else:
            logger.info(
                "Running in LEGACY mode (explicit team_mappings)",
            )
            summary = run_legacy_sync(
                org_config,
                default_member_role=config.default_member_role,
                dry_run=dry_run,
                client=client,
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
