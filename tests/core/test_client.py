"""Unit tests for DevinAPIClient — written FIRST per TDD methodology.

All HTTP calls are mocked using the `responses` library.
"""
import pytest
import responses

from user_management.core.client import (
    AuthError,
    DevinAPIClient,
    NotFoundError,
    PermissionError,
    RateLimitError,
    ValidationError,
)

BASE_URL = "https://api.devin.ai"


@pytest.fixture
def client():
    return DevinAPIClient(api_key="cog_test_key", base_url=BASE_URL)


# ---------------------------------------------------------------------------
# Auth & Identity
# ---------------------------------------------------------------------------
class TestVerifyCredentials:
    @responses.activate
    def test_success(self, client):
        responses.get(
            f"{BASE_URL}/v3/self",
            json={
                "principal_type": "service_user",
                "service_user_id": "su-123",
                "service_user_name": "test-key",
                "org_id": None,
            },
        )
        result = client.verify_credentials()
        assert result["principal_type"] == "service_user"
        assert result["service_user_id"] == "su-123"
        assert result["service_user_name"] == "test-key"

    @responses.activate
    def test_auth_header_sent(self, client):
        responses.get(
            f"{BASE_URL}/v3/self",
            json={"principal_type": "service_user"},
        )
        client.verify_credentials()
        assert responses.calls[0].request.headers["Authorization"] == "Bearer cog_test_key"

    @responses.activate
    def test_401_raises_auth_error(self, client):
        responses.get(f"{BASE_URL}/v3/self", json={"detail": "Unauthorized"}, status=401)
        with pytest.raises(AuthError):
            client.verify_credentials()

    @responses.activate
    def test_403_raises_permission_error(self, client):
        responses.get(f"{BASE_URL}/v3/self", json={"detail": "Forbidden"}, status=403)
        with pytest.raises(PermissionError):
            client.verify_credentials()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class TestListUsers:
    @responses.activate
    def test_list_all_users(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/members/users",
            json={
                "items": [{"user_id": "u1", "email": "a@x.com"}],
                "has_next_page": False,
                "end_cursor": None,
                "total": 1,
            },
        )
        result = client.list_users()
        assert len(result) == 1
        assert result[0]["email"] == "a@x.com"

    @responses.activate
    def test_list_users_with_email_filter(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/members/users",
            json={
                "items": [{"user_id": "u1", "email": "a@x.com"}],
                "has_next_page": False,
                "end_cursor": None,
                "total": 1,
            },
        )
        client.list_users(email="a@x.com")
        assert "email=a%40x.com" in responses.calls[0].request.url or "email=a@x.com" in responses.calls[0].request.url

    @responses.activate
    def test_list_users_pagination(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/members/users",
            json={
                "items": [{"user_id": "u1"}],
                "has_next_page": True,
                "end_cursor": "cursor1",
                "total": 2,
            },
        )
        responses.get(
            f"{BASE_URL}/v3/enterprise/members/users",
            json={
                "items": [{"user_id": "u2"}],
                "has_next_page": False,
                "end_cursor": None,
                "total": 2,
            },
        )
        result = client.list_users()
        assert len(result) == 2
        assert result[0]["user_id"] == "u1"
        assert result[1]["user_id"] == "u2"


class TestBulkInviteUsers:
    @responses.activate
    def test_invite_small_batch(self, client):
        responses.post(
            f"{BASE_URL}/v3/enterprise/members/users",
            json={"invited": ["a@x.com", "b@x.com"]},
            status=200,
        )
        client.bulk_invite_users(["a@x.com", "b@x.com"], "account_member")
        body = responses.calls[0].request.body
        assert b"a@x.com" in body
        assert b"account_member" in body

    @responses.activate
    def test_invite_auto_batches_over_100(self, client):
        # Generate 150 emails
        emails = [f"user{i}@x.com" for i in range(150)]
        responses.post(
            f"{BASE_URL}/v3/enterprise/members/users",
            json={"invited": emails[:100]},
            status=200,
        )
        responses.post(
            f"{BASE_URL}/v3/enterprise/members/users",
            json={"invited": emails[100:]},
            status=200,
        )
        client.bulk_invite_users(emails, "account_member")
        assert len(responses.calls) == 2


class TestDeleteUser:
    @responses.activate
    def test_delete_user(self, client):
        responses.delete(f"{BASE_URL}/v3/enterprise/members/users/u1", status=200, json={})
        client.delete_user("u1")
        assert responses.calls[0].request.method == "DELETE"

    @responses.activate
    def test_delete_user_not_found(self, client):
        responses.delete(
            f"{BASE_URL}/v3/enterprise/members/users/u999",
            json={"detail": "Not Found"},
            status=404,
        )
        with pytest.raises(NotFoundError):
            client.delete_user("u999")


class TestUpdateUserEnterpriseRole:
    @responses.activate
    def test_update_role(self, client):
        responses.patch(
            f"{BASE_URL}/v3/enterprise/members/users/u1",
            json={"user_id": "u1", "role_id": "account_admin"},
            status=200,
        )
        client.update_user_enterprise_role("u1", "account_admin")
        assert b"account_admin" in responses.calls[0].request.body


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
class TestListRoles:
    @responses.activate
    def test_list_roles(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/roles",
            json={
                "items": [
                    {"role_name": "Admin", "role_id": "account_admin", "role_type": "enterprise"},
                    {"role_name": "Member", "role_id": "account_member", "role_type": "enterprise"},
                    {"role_name": "Admin", "role_id": "org_admin", "role_type": "org"},
                ],
                "has_next_page": False,
                "end_cursor": None,
                "total": 3,
            },
        )
        result = client.list_roles()
        assert len(result) == 3
        assert result[0]["role_id"] == "account_admin"


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------
class TestListOrganizations:
    @responses.activate
    def test_list_orgs(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/organizations",
            json={
                "items": [{"org_id": "o1", "name": "Eng/Pay"}],
                "has_next_page": False,
                "end_cursor": None,
                "total": 1,
            },
        )
        result = client.list_organizations()
        assert len(result) == 1
        assert result[0]["name"] == "Eng/Pay"


class TestCreateOrganization:
    @responses.activate
    def test_create_org(self, client):
        responses.post(
            f"{BASE_URL}/v3/enterprise/organizations",
            json={"org_id": "o-new", "name": "Team/Proj"},
            status=200,
        )
        client.create_organization("Team/Proj", max_cycle_acu_limit=5000, max_session_acu_limit=100)
        body = responses.calls[0].request.body
        assert b"Team/Proj" in body

    @responses.activate
    def test_create_org_minimal(self, client):
        responses.post(
            f"{BASE_URL}/v3/enterprise/organizations",
            json={"org_id": "o-new", "name": "Team/Proj"},
            status=200,
        )
        result = client.create_organization("Team/Proj")
        assert result["org_id"] == "o-new"


class TestDeleteOrganization:
    @responses.activate
    def test_delete_org(self, client):
        responses.delete(f"{BASE_URL}/v3/enterprise/organizations/o1", status=200, json={})
        client.delete_organization("o1")

    @responses.activate
    def test_delete_org_not_found(self, client):
        responses.delete(
            f"{BASE_URL}/v3/enterprise/organizations/o999",
            json={"detail": "Not Found"},
            status=404,
        )
        with pytest.raises(NotFoundError):
            client.delete_organization("o999")


class TestUpdateOrganization:
    @responses.activate
    def test_update_org(self, client):
        responses.patch(
            f"{BASE_URL}/v3/enterprise/organizations/o1",
            json={"org_id": "o1", "name": "New/Name"},
            status=200,
        )
        client.update_organization("o1", name="New/Name", max_cycle_acu_limit=3000)
        assert b"New/Name" in responses.calls[0].request.body


# ---------------------------------------------------------------------------
# Org Membership
# ---------------------------------------------------------------------------
class TestOrgMembership:
    @responses.activate
    def test_assign_user_to_org(self, client):
        responses.post(
            f"{BASE_URL}/v3/enterprise/organizations/o1/members/users/u1",
            json={},
            status=200,
        )
        client.assign_user_to_org("o1", "u1", "org_member")
        assert b"org_member" in responses.calls[0].request.body

    @responses.activate
    def test_remove_user_from_org(self, client):
        responses.delete(
            f"{BASE_URL}/v3/enterprise/organizations/o1/members/users/u1",
            json={},
            status=200,
        )
        client.remove_user_from_org("o1", "u1")

    @responses.activate
    def test_update_user_org_role(self, client):
        responses.patch(
            f"{BASE_URL}/v3/enterprise/organizations/o1/members/users/u1",
            json={},
            status=200,
        )
        client.update_user_org_role("o1", "u1", "org_admin")
        assert b"org_admin" in responses.calls[0].request.body


# ---------------------------------------------------------------------------
# ACU Limits
# ---------------------------------------------------------------------------
class TestACULimits:
    @responses.activate
    def test_set_org_acu_limit(self, client):
        responses.put(
            f"{BASE_URL}/v3/enterprise/consumption/acu-limits/devin/organizations/o1",
            json={},
            status=200,
        )
        client.set_org_acu_limit("o1", 5000)
        assert b"5000" in responses.calls[0].request.body

    @responses.activate
    def test_delete_org_acu_limit(self, client):
        responses.delete(
            f"{BASE_URL}/v3/enterprise/consumption/acu-limits/devin/organizations/o1",
            json={},
            status=200,
        )
        client.delete_org_acu_limit("o1")

    @responses.activate
    def test_list_acu_limits(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/consumption/acu-limits/devin",
            json={
                "items": [{"org_id": "o1", "cycle_acu_limit": 5000}],
                "has_next_page": False,
                "total": 1,
            },
        )
        result = client.list_acu_limits()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Git Connections
# ---------------------------------------------------------------------------
class TestGitConnections:
    @responses.activate
    def test_list_git_connections(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/git-providers/connections",
            json={
                "items": [
                    {
                        "git_connection_id": "gc-1",
                        "git_provider_type": "github_app",
                        "name": "MyGH",
                        "host": "github.com",
                        "created_at": 1234567890,
                    }
                ],
                "has_next_page": False,
                "end_cursor": None,
                "total": 1,
            },
        )
        result = client.list_git_connections()
        assert len(result) == 1
        assert result[0]["git_provider_type"] == "github_app"

    @responses.activate
    def test_get_github_connection_id(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/git-providers/connections",
            json={
                "items": [
                    {"git_connection_id": "gc-1", "git_provider_type": "github_app", "name": "GH", "host": "github.com", "created_at": 0},
                    {"git_connection_id": "gc-2", "git_provider_type": "gitlab_token", "name": "GL", "host": "gitlab.com", "created_at": 0},
                ],
                "has_next_page": False,
                "end_cursor": None,
                "total": 2,
            },
        )
        result = client.get_github_connection_id()
        assert result == "gc-1"

    @responses.activate
    def test_get_github_connection_id_not_found(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/git-providers/connections",
            json={
                "items": [
                    {"git_connection_id": "gc-2", "git_provider_type": "gitlab_token", "name": "GL", "host": "gitlab.com", "created_at": 0},
                ],
                "has_next_page": False,
                "end_cursor": None,
                "total": 1,
            },
        )
        with pytest.raises(NotFoundError, match="GitHub"):
            client.get_github_connection_id()


# ---------------------------------------------------------------------------
# Git Permissions
# ---------------------------------------------------------------------------
class TestGitPermissions:
    @responses.activate
    def test_list_org_git_permissions(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/organizations/o1/git-providers/permissions",
            json={
                "items": [{"git_connection_id": "gc-1", "repo_path": "org/repo1"}],
                "has_next_page": False,
                "end_cursor": None,
                "total": 1,
            },
        )
        result = client.list_org_git_permissions("o1")
        assert len(result) == 1

    @responses.activate
    def test_set_org_git_permissions(self, client):
        responses.put(
            f"{BASE_URL}/v3/enterprise/organizations/o1/git-providers/permissions",
            json={},
            status=200,
        )
        perms = [{"git_connection_id": "gc-1", "repo_path": "org/repo1"}]
        client.set_org_git_permissions("o1", perms)
        assert b"org/repo1" in responses.calls[0].request.body

    @responses.activate
    def test_add_org_git_permissions(self, client):
        responses.post(
            f"{BASE_URL}/v3/enterprise/organizations/o1/git-providers/permissions",
            json={},
            status=200,
        )
        perms = [{"git_connection_id": "gc-1", "repo_path": "org/repo2"}]
        client.add_org_git_permissions("o1", perms)
        assert b"org/repo2" in responses.calls[0].request.body

    @responses.activate
    def test_clear_org_git_permissions(self, client):
        responses.delete(
            f"{BASE_URL}/v3/enterprise/organizations/o1/git-providers/permissions",
            json={},
            status=200,
        )
        client.clear_org_git_permissions("o1")


# ---------------------------------------------------------------------------
# Error handling & Retry
# ---------------------------------------------------------------------------
class TestErrorHandling:
    @responses.activate
    def test_422_raises_validation_error(self, client):
        responses.get(
            f"{BASE_URL}/v3/self",
            json={"detail": "Validation Error"},
            status=422,
        )
        with pytest.raises(ValidationError):
            client.verify_credentials()

    @responses.activate
    def test_404_raises_not_found_error(self, client):
        responses.delete(
            f"{BASE_URL}/v3/enterprise/organizations/o999",
            json={"detail": "Not Found"},
            status=404,
        )
        with pytest.raises(NotFoundError):
            client.delete_organization("o999")

    @responses.activate
    def test_429_retries_with_backoff(self, client):
        responses.get(
            f"{BASE_URL}/v3/self",
            json={"detail": "Rate limited"},
            status=429,
        )
        responses.get(
            f"{BASE_URL}/v3/self",
            json={"principal_type": "service_user"},
            status=200,
        )
        result = client.verify_credentials()
        assert result["principal_type"] == "service_user"
        assert len(responses.calls) == 2

    @responses.activate
    def test_429_exhausted_raises_rate_limit_error(self, client):
        for _ in range(5):
            responses.get(
                f"{BASE_URL}/v3/self",
                json={"detail": "Rate limited"},
                status=429,
            )
        with pytest.raises(RateLimitError):
            client.verify_credentials()


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------
class TestPagination:
    @responses.activate
    def test_paginate_multiple_pages(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/organizations",
            json={
                "items": [{"org_id": "o1"}],
                "has_next_page": True,
                "end_cursor": "c1",
                "total": 3,
            },
        )
        responses.get(
            f"{BASE_URL}/v3/enterprise/organizations",
            json={
                "items": [{"org_id": "o2"}, {"org_id": "o3"}],
                "has_next_page": False,
                "end_cursor": None,
                "total": 3,
            },
        )
        result = client.list_organizations()
        assert len(result) == 3
        # Verify second request included after parameter
        assert "after=c1" in responses.calls[1].request.url

    @responses.activate
    def test_paginate_single_page(self, client):
        responses.get(
            f"{BASE_URL}/v3/enterprise/organizations",
            json={
                "items": [{"org_id": "o1"}],
                "has_next_page": False,
                "end_cursor": None,
                "total": 1,
            },
        )
        result = client.list_organizations()
        assert len(result) == 1
        assert len(responses.calls) == 1
