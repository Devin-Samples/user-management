"""Sync GitHub Team membership and repo access to Devin organizations.

GitHub Teams are the source of truth. Each run:

1. Fetches the desired team members and repos from GitHub.
2. Resolves GitHub users to Devin users (via email / SAML / audit log / username).
3. Diffs against current Devin org members & git permissions, applies the delta.

Both single-team-mapping (legacy) and auto-discovery modes are supported.
See :mod:`user_management.github_sync.sync` for orchestration and
:mod:`user_management.github_sync.config` for YAML loading.
"""

from user_management.github_sync.config import load_config, load_state_file, save_state_file
from user_management.github_sync.github_client import GitHubClient
from user_management.github_sync.sync import (
    build_email_lookup,
    find_github_connection,
    resolve_gh_login_to_devin_user,
    run_auto_sync,
    run_legacy_sync,
    sync_members_for_team,
    sync_repos_for_team,
)

__all__ = [
    "GitHubClient",
    "build_email_lookup",
    "find_github_connection",
    "load_config",
    "load_state_file",
    "resolve_gh_login_to_devin_user",
    "run_auto_sync",
    "run_legacy_sync",
    "save_state_file",
    "sync_members_for_team",
    "sync_repos_for_team",
]
