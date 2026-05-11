"""GitHub REST API client for fetching team members and repositories.

Ported from github-permissions-devin-sync's ``github_client.py`` from
httpx → requests so the whole repo can stay synchronous.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

import requests

from user_management.core.models import GitHubRepo, GitHubTeam, GitHubUser

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_PER_PAGE = 100
DEFAULT_TIMEOUT_SECONDS = 30.0


class GitHubClient:
    """Synchronous GitHub REST API client with automatic pagination."""

    def __init__(self, token: str, base_url: str = GITHUB_API_BASE) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._session = requests.Session()
        self._session.headers.update(self._headers)

    # ------------------------------------------------------------------
    # Team discovery
    # ------------------------------------------------------------------
    def list_org_teams(self, org: str) -> list[GitHubTeam]:
        """``GET /orgs/{org}/teams`` — all teams in the GitHub org."""
        url = f"{self._base_url}/orgs/{org}/teams"
        teams: list[GitHubTeam] = []

        for page_data in self._paginate(url):
            for item in page_data:
                teams.append(
                    GitHubTeam(
                        slug=item["slug"],
                        name=item["name"],
                        id=item["id"],
                        team_type=item.get("privacy", ""),
                    )
                )

        logger.info("Fetched %d teams from GitHub org %s", len(teams), org)
        return teams

    # ------------------------------------------------------------------
    # Team members & repos
    # ------------------------------------------------------------------
    def list_team_members(self, org: str, team_slug: str) -> list[GitHubUser]:
        """``GET /orgs/{org}/teams/{team_slug}/members``."""
        url = f"{self._base_url}/orgs/{org}/teams/{team_slug}/members"
        members: list[GitHubUser] = []

        for page_data in self._paginate(url):
            for item in page_data:
                members.append(
                    GitHubUser(
                        login=item["login"],
                        id=item["id"],
                        email=item.get("email"),
                    )
                )

        logger.info(
            "Fetched %d members from GitHub team %s/%s",
            len(members),
            org,
            team_slug,
        )
        return members

    def list_team_repos(self, org: str, team_slug: str) -> list[GitHubRepo]:
        """``GET /orgs/{org}/teams/{team_slug}/repos``."""
        url = f"{self._base_url}/orgs/{org}/teams/{team_slug}/repos"
        repos: list[GitHubRepo] = []

        for page_data in self._paginate(url):
            for item in page_data:
                repos.append(
                    GitHubRepo(
                        full_name=item["full_name"],
                        name=item["name"],
                        private=item.get("private", False),
                    )
                )

        logger.info(
            "Fetched %d repos from GitHub team %s/%s", len(repos), org, team_slug
        )
        return repos

    # ------------------------------------------------------------------
    # Email resolution helpers
    # ------------------------------------------------------------------
    def get_user_profile_email(self, login: str) -> Optional[str]:
        """``GET /users/{login}`` — return the public email if any."""
        url = f"{self._base_url}/users/{login}"
        try:
            resp = self._session.get(url, timeout=DEFAULT_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json().get("email") or None
        except Exception as exc:
            logger.debug("Failed to fetch profile for %s: %s", login, exc)
            return None

    def get_saml_identities(self, org: str) -> dict[str, str]:
        """Fetch SAML identity mappings via GraphQL.

        Returns ``{github_login: saml_name_id (email)}``.  Requires the
        token to have ``admin:org`` scope and SSO to be configured.
        """
        query = """
        query($org: String!, $cursor: String) {
          organization(login: $org) {
            samlIdentityProvider {
              externalIdentities(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                edges {
                  node {
                    user { login }
                    samlIdentity { nameId }
                  }
                }
              }
            }
          }
        }
        """
        result: dict[str, str] = {}
        cursor: Optional[str] = None

        while True:
            variables: dict[str, str] = {"org": org}
            if cursor:
                variables["cursor"] = cursor

            try:
                resp = self._session.post(
                    "https://api.github.com/graphql",
                    json={"query": query, "variables": variables},
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("GraphQL SAML query failed: %s", exc)
                break

            data = resp.json()
            provider = (
                data.get("data", {})
                .get("organization", {})
                .get("samlIdentityProvider")
            )
            if not provider:
                logger.debug("No SAML identity provider found for org %s", org)
                break

            edges = provider.get("externalIdentities", {}).get("edges", [])
            for edge in edges:
                node = edge.get("node", {})
                user = node.get("user")
                saml = node.get("samlIdentity")
                if user and saml and user.get("login") and saml.get("nameId"):
                    result[user["login"].lower()] = saml["nameId"]

            page_info = provider.get("externalIdentities", {}).get("pageInfo", {})
            if page_info.get("hasNextPage"):
                cursor = page_info.get("endCursor")
            else:
                break

        logger.info("Resolved %d SAML identities for org %s", len(result), org)
        return result

    def get_audit_log_invite_emails(self, org: str) -> dict[str, str]:
        """``GET /orgs/{org}/audit-log`` filtered to invite_member actions."""
        url = f"{self._base_url}/orgs/{org}/audit-log"
        params = {"phrase": "action:org.invite_member", "per_page": 100}
        result: dict[str, str] = {}

        try:
            for page_data in self._paginate(url, params=params):
                for entry in page_data:
                    login = (entry.get("user") or "").lower()
                    email = entry.get("data", {}).get("invitee_email") or entry.get(
                        "invitee_email"
                    )
                    if login and email:
                        result[login] = email
        except Exception as exc:
            logger.warning("Audit log query failed for org %s: %s", org, exc)

        logger.info(
            "Resolved %d invite emails from audit log for org %s",
            len(result),
            org,
        )
        return result

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------
    def _paginate(
        self,
        url: str,
        params: Optional[dict] = None,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> Iterator[list[dict]]:
        """Yield pages of JSON results, following GitHub's Link header."""
        if params is None:
            params = {}
        params = dict(params)
        params["per_page"] = per_page
        next_url: Optional[str] = url

        while next_url is not None:
            resp = self._session.get(
                next_url, params=params, timeout=DEFAULT_TIMEOUT_SECONDS
            )
            resp.raise_for_status()

            data = resp.json()
            if not data:
                break

            yield data

            next_url = self._parse_next_link(resp.headers.get("Link"))
            params = {}  # baked into the next URL

    @staticmethod
    def _parse_next_link(link_header: Optional[str]) -> Optional[str]:
        if not link_header:
            return None
        for part in link_header.split(","):
            part = part.strip()
            if 'rel="next"' in part:
                return part.split(";")[0].strip().strip("<>")
        return None
