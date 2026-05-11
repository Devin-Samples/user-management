"""Individual diagnostic checks.

Each check returns ``True`` on success, ``False`` on failure.  They are
designed to be runnable independently from the CLI or stitched together
via :func:`run_all`.
"""

from __future__ import annotations

from typing import Optional

import requests

from user_management.core.client import DevinAPIClient
from user_management.core.config import (
    get_devin_api_base_url,
    get_devin_api_key,
    get_github_token,
)
from user_management.github_sync.github_client import GitHubClient


def check_devin_auth() -> bool:
    """Verify the Devin API key and base URL are usable."""
    print("== Devin auth ==")
    api_key = get_devin_api_key(required=False)
    base_url = get_devin_api_base_url()
    if not api_key:
        print("  X DEVIN_API_KEY is not set")
        return False
    print(f"  base URL: {base_url}")
    client = DevinAPIClient(api_key=api_key, base_url=base_url)
    try:
        creds = client.verify_credentials()
    except Exception as exc:
        print(f"  X GET /v3/self failed: {exc}")
        return False
    print(
        f"  OK service_user={creds.get('service_user_name')} "
        f"(id={creds.get('service_user_id')})"
    )
    return True


def check_github_token() -> bool:
    """Verify ``GITHUB_TOKEN`` is set and has the required scopes."""
    print("== GitHub token ==")
    token = get_github_token(required=False)
    if not token:
        print("  X GITHUB_TOKEN is not set")
        return False
    try:
        resp = requests.get(
            "https://api.github.com/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"  X GET /user failed: {exc}")
        return False

    scopes_header = resp.headers.get("X-OAuth-Scopes", "")
    scopes = {s.strip() for s in scopes_header.split(",") if s.strip()}
    login = resp.json().get("login", "<unknown>")
    print(f"  OK authenticated as {login}")
    print(f"  scopes: {sorted(scopes) or '(none reported)'}")
    if "read:org" not in scopes and "admin:org" not in scopes:
        print(
            "  ! warning: token lacks read:org scope — team/member listing "
            "may fail"
        )
        return False
    return True


def check_github_app() -> bool:
    """Verify there is a GitHub git connection (App or token) in Devin."""
    print("== GitHub git connection ==")
    api_key = get_devin_api_key(required=False)
    if not api_key:
        print("  X DEVIN_API_KEY is not set")
        return False
    client = DevinAPIClient(
        api_key=api_key, base_url=get_devin_api_base_url()
    )
    try:
        connections = client.list_git_connections()
    except Exception as exc:
        print(f"  X listing git connections failed: {exc}")
        return False

    print(f"  found {len(connections)} git connection(s)")

    github_apps = [c for c in connections if c.get("git_provider_type") == "github_app"]
    github_tokens = [
        c
        for c in connections
        if c.get("git_provider_type") in {"github_token", "github_individual_token"}
    ]

    for conn in github_apps:
        print(
            f"  OK github_app: name={conn.get('name', '<unnamed>')}  "
            f"id={conn['git_connection_id']}"
        )
    for conn in github_tokens:
        print(
            f"  OK github_token: name={conn.get('name', '<unnamed>')}  "
            f"id={conn['git_connection_id']}"
        )

    if not github_apps and not github_tokens:
        print("  X no GitHub git connection found — repo sync will be skipped")
        return False
    if not github_apps:
        print(
            "  ! warning: no GitHub App found.  Token-based connections work "
            "for repo access but Apps are preferred."
        )
    return True


def check_email_visibility(
    github_org: Optional[str] = None,
    team_slug: Optional[str] = None,
) -> bool:
    """For a given GitHub team, report email visibility for its members.

    If ``github_org``/``team_slug`` are not supplied, this check is skipped.
    """
    print("== GitHub email visibility ==")
    if not github_org or not team_slug:
        print(
            "  (skipped — pass --github-org and --team-slug to run "
            "this check)"
        )
        return True

    token = get_github_token(required=False)
    if not token:
        print("  X GITHUB_TOKEN is not set")
        return False

    github_client = GitHubClient(token=token)
    try:
        members = github_client.list_team_members(github_org, team_slug)
    except Exception as exc:
        print(f"  X listing team members failed: {exc}")
        return False

    if not members:
        print(f"  ! team {github_org}/{team_slug} has no members")
        return True

    with_email = sum(1 for m in members if m.email)
    without_email = len(members) - with_email
    print(f"  {len(members)} member(s) — {with_email} with email, {without_email} without")
    for m in members:
        status = "OK" if m.email else "X "
        email_display = m.email or "(not visible)"
        print(f"    {status} {m.login:20s} {email_display}")

    if without_email:
        print(
            "\n  ! members without visible email will be skipped during sync "
            "unless resolved via SAML / audit-log / public-profile lookups."
        )
    return without_email == 0


def run_all(
    github_org: Optional[str] = None,
    team_slug: Optional[str] = None,
) -> bool:
    """Run all checks. Returns True iff every check passes."""
    results = [
        check_devin_auth(),
        check_github_token(),
        check_github_app(),
        check_email_visibility(github_org=github_org, team_slug=team_slug),
    ]
    return all(results)
