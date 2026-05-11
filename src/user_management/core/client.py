"""Synchronous Devin v3 API client used by both ``bulk`` and ``github_sync``.

This is a port of devin-bulk-manager's ``api_client.py`` extended with the
endpoints github-permissions-devin-sync needed (notably
``replace_git_permissions``).  It returns raw dicts so existing bulk code
keeps working; callers that prefer typed objects can wrap the responses in
the pydantic models from :mod:`user_management.core.models`.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from user_management.core.errors import (
    APIError,
    AuthError,
    NotFoundError,
    PermissionError,
    RateLimitError,
    ValidationError,
)


class DevinAPIClient:
    """Client for the Devin v3 Enterprise API.

    Authentication is via a service-user Bearer token. The token must have
    the permissions required by whichever operations the caller invokes
    (e.g. ``ManageAccountMembership``, ``ManageGitIntegrations``).
    """

    MAX_BULK_INVITE = 100
    MAX_GIT_PERMISSIONS = 200
    MAX_RETRIES = 4
    RETRY_BASE_DELAY = 0.1  # seconds (short for tests; real usage can override)

    def __init__(self, api_key: str, base_url: str = "https://api.devin.ai"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {api_key}"})

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Make an HTTP request with retry on 429."""
        url = f"{self.base_url}{path}"
        resp: requests.Response | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            resp = self._session.request(method, url, **kwargs)
            if resp.status_code != 429:
                break
            if attempt == self.MAX_RETRIES:
                raise RateLimitError(
                    f"Rate limit exceeded after {self.MAX_RETRIES} retries",
                    status_code=429,
                    response_body=resp.text,
                )
            delay = self.RETRY_BASE_DELAY * (2 ** attempt)
            time.sleep(delay)

        assert resp is not None  # guaranteed by loop executing at least once
        self._check_errors(resp)
        return resp

    def _check_errors(self, resp: requests.Response) -> None:
        """Raise typed exceptions for HTTP error codes."""
        if resp.status_code < 400:
            return
        body = resp.text
        code = resp.status_code
        if code == 401:
            raise AuthError(f"Authentication failed: {body}", code, body)
        if code == 403:
            raise PermissionError(f"Permission denied: {body}", code, body)
        if code == 404:
            raise NotFoundError(f"Not found: {body}", code, body)
        if code == 422:
            raise ValidationError(f"Validation error: {body}", code, body)
        if code == 429:
            raise RateLimitError(f"Rate limited: {body}", code, body)
        raise APIError(f"API error {code}: {body}", code, body)

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params).json()

    def _post_json(self, path: str, body: dict) -> dict:
        resp = self._request("POST", path, json=body)
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def _put_json(self, path: str, body: dict) -> dict:
        resp = self._request("PUT", path, json=body)
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def _patch_json(self, path: str, body: dict) -> dict:
        resp = self._request("PATCH", path, json=body)
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def _delete(self, path: str) -> dict:
        resp = self._request("DELETE", path)
        if resp.text:
            return resp.json()
        return {}

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Follow cursor-based pagination, returning a combined items list."""
        all_items: list[dict] = []
        params = dict(params or {})
        while True:
            data = self._get_json(path, params=params)
            all_items.extend(data.get("items", []))
            if not data.get("has_next_page"):
                break
            params["after"] = data["end_cursor"]
        return all_items

    # ------------------------------------------------------------------
    # Auth & Identity
    # ------------------------------------------------------------------
    def verify_credentials(self) -> dict:
        """``GET /v3/self`` — verify the API key is valid."""
        return self._get_json("/v3/self")

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    def list_users(self, email: str | None = None) -> list[dict]:
        """``GET /v3/enterprise/members/users`` with optional email filter."""
        params: dict[str, str] = {}
        if email:
            params["email"] = email
        return self._paginate("/v3/enterprise/members/users", params=params)

    def bulk_invite_users(self, emails: list[str], enterprise_role_id: str) -> list[dict]:
        """``POST /v3/enterprise/members/users`` — auto-batches if >100 emails."""
        results = []
        for i in range(0, len(emails), self.MAX_BULK_INVITE):
            batch = emails[i : i + self.MAX_BULK_INVITE]
            resp = self._post_json(
                "/v3/enterprise/members/users",
                {"emails": batch, "enterprise_role_id": enterprise_role_id},
            )
            results.append(resp)
        return results

    def delete_user(self, user_id: str) -> dict:
        """``DELETE /v3/enterprise/members/users/{user_id}``."""
        return self._delete(f"/v3/enterprise/members/users/{user_id}")

    def update_user_enterprise_role(self, user_id: str, role_id: str) -> dict:
        """``PATCH /v3/enterprise/members/users/{user_id}``."""
        return self._patch_json(
            f"/v3/enterprise/members/users/{user_id}", {"role_id": role_id}
        )

    # ------------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------------
    def list_roles(self) -> list[dict]:
        """``GET /v3/enterprise/roles``."""
        return self._paginate("/v3/enterprise/roles")

    # ------------------------------------------------------------------
    # Organizations
    # ------------------------------------------------------------------
    def list_organizations(self) -> list[dict]:
        """``GET /v3/enterprise/organizations``."""
        return self._paginate("/v3/enterprise/organizations")

    def create_organization(
        self,
        name: str,
        max_cycle_acu_limit: int | None = None,
        max_session_acu_limit: int | None = None,
    ) -> dict:
        """``POST /v3/enterprise/organizations``."""
        body: dict[str, Any] = {"name": name}
        if max_cycle_acu_limit is not None:
            body["max_cycle_acu_limit"] = max_cycle_acu_limit
        if max_session_acu_limit is not None:
            body["max_session_acu_limit"] = max_session_acu_limit
        return self._post_json("/v3/enterprise/organizations", body)

    def delete_organization(self, org_id: str) -> dict:
        """``DELETE /v3/enterprise/organizations/{org_id}``."""
        return self._delete(f"/v3/enterprise/organizations/{org_id}")

    def update_organization(
        self,
        org_id: str,
        name: str | None = None,
        max_cycle_acu_limit: int | None = None,
        max_session_acu_limit: int | None = None,
    ) -> dict:
        """``PATCH /v3/enterprise/organizations/{org_id}``."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if max_cycle_acu_limit is not None:
            body["max_cycle_acu_limit"] = max_cycle_acu_limit
        if max_session_acu_limit is not None:
            body["max_session_acu_limit"] = max_session_acu_limit
        return self._patch_json(f"/v3/enterprise/organizations/{org_id}", body)

    # ------------------------------------------------------------------
    # Org Membership
    # ------------------------------------------------------------------
    def list_org_members(self, org_id: str) -> list[dict]:
        """``GET /v3/enterprise/organizations/{org_id}/members/users``."""
        return self._paginate(f"/v3/enterprise/organizations/{org_id}/members/users")

    def assign_user_to_org(self, org_id: str, user_id: str, role_id: str) -> dict:
        """``POST /v3/enterprise/organizations/{org_id}/members/users/{user_id}``."""
        return self._post_json(
            f"/v3/enterprise/organizations/{org_id}/members/users/{user_id}",
            {"role_id": role_id},
        )

    def remove_user_from_org(self, org_id: str, user_id: str) -> dict:
        """``DELETE /v3/enterprise/organizations/{org_id}/members/users/{user_id}``."""
        return self._delete(
            f"/v3/enterprise/organizations/{org_id}/members/users/{user_id}"
        )

    def update_user_org_role(self, org_id: str, user_id: str, role_id: str) -> dict:
        """``PATCH /v3/enterprise/organizations/{org_id}/members/users/{user_id}``."""
        return self._patch_json(
            f"/v3/enterprise/organizations/{org_id}/members/users/{user_id}",
            {"role_id": role_id},
        )

    # ------------------------------------------------------------------
    # ACU Limits
    # ------------------------------------------------------------------
    def set_org_acu_limit(self, org_id: str, cycle_acu_limit: int) -> dict:
        """``PUT /v3/enterprise/consumption/acu-limits/devin/organizations/{org_id}``."""
        return self._put_json(
            f"/v3/enterprise/consumption/acu-limits/devin/organizations/{org_id}",
            {"cycle_acu_limit": cycle_acu_limit},
        )

    def delete_org_acu_limit(self, org_id: str) -> dict:
        """``DELETE /v3/enterprise/consumption/acu-limits/devin/organizations/{org_id}``."""
        return self._delete(
            f"/v3/enterprise/consumption/acu-limits/devin/organizations/{org_id}"
        )

    def list_acu_limits(self) -> list[dict]:
        """``GET /v3/enterprise/consumption/acu-limits/devin``."""
        return self._paginate("/v3/enterprise/consumption/acu-limits/devin")

    # ------------------------------------------------------------------
    # Git Connections
    # ------------------------------------------------------------------
    def list_git_connections(self) -> list[dict]:
        """``GET /v3/enterprise/git-providers/connections``."""
        return self._paginate("/v3/enterprise/git-providers/connections")

    def get_github_connection_id(self) -> str:
        """Auto-discover the GitHub git connection ID.

        Returns the first connection whose ``git_provider_type`` is one of
        ``github_app``, ``github_token``, or ``github_individual_token``,
        preferring app-based connections over token-based ones.
        """
        preference = ["github_app", "github_token", "github_individual_token"]
        connections = self.list_git_connections()
        github_connections = [
            c for c in connections if c.get("git_provider_type") in preference
        ]
        if not github_connections:
            raise NotFoundError(
                "No GitHub connection found (github_app, github_token, or github_individual_token)"
            )
        github_connections.sort(
            key=lambda c: preference.index(c["git_provider_type"])
        )
        return github_connections[0]["git_connection_id"]

    # ------------------------------------------------------------------
    # Git Permissions
    # ------------------------------------------------------------------
    def list_org_git_permissions(self, org_id: str) -> list[dict]:
        """``GET /v3/enterprise/organizations/{org_id}/git-providers/permissions``."""
        return self._paginate(
            f"/v3/enterprise/organizations/{org_id}/git-providers/permissions"
        )

    def set_org_git_permissions(self, org_id: str, permissions: list[dict]) -> dict:
        """``PUT …/git-providers/permissions`` — atomically replaces all entries."""
        return self._put_json(
            f"/v3/enterprise/organizations/{org_id}/git-providers/permissions",
            {"permissions": permissions[: self.MAX_GIT_PERMISSIONS]},
        )

    def add_org_git_permissions(self, org_id: str, permissions: list[dict]) -> dict:
        """``POST …/git-providers/permissions`` — appends entries."""
        return self._post_json(
            f"/v3/enterprise/organizations/{org_id}/git-providers/permissions",
            {"permissions": permissions[: self.MAX_GIT_PERMISSIONS]},
        )

    def clear_org_git_permissions(self, org_id: str) -> dict:
        """``DELETE …/git-providers/permissions``."""
        return self._delete(
            f"/v3/enterprise/organizations/{org_id}/git-providers/permissions"
        )

    # Aliases that match github-permissions-devin-sync's old method names so
    # the ported sync code reads naturally.
    list_git_permissions = list_org_git_permissions
    replace_git_permissions = set_org_git_permissions
