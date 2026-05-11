"""Unit tests for BulkManager orchestration — covers both legacy and sync modes.

Uses a mock DevinAPIClient to verify orchestration logic.
"""
from unittest.mock import MagicMock

import pytest

from user_management.core.client import DevinAPIClient
from user_management.bulk.sync import (
    BulkManager,
    OrgDiff,
    UserDiff,
    SyncValidator,
    is_managed_org,
)
from user_management.bulk.spreadsheet import OrgRow, UserRow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_client():
    client = MagicMock(spec=DevinAPIClient)
    client.verify_credentials.return_value = {
        "principal_type": "service_user",
        "service_user_id": "su-1",
        "service_user_name": "test-key",
    }
    client.get_github_connection_id.return_value = "gc-1"
    client.list_roles.return_value = [
        {"role_name": "Admin", "role_id": "account_admin", "role_type": "enterprise"},
        {"role_name": "Member", "role_id": "account_member", "role_type": "enterprise"},
        {"role_name": "Admin", "role_id": "org_admin", "role_type": "org"},
        {"role_name": "Member", "role_id": "org_member", "role_type": "org"},
    ]
    client.list_organizations.return_value = [
        {"org_id": "o1", "name": "Existing/Org", "max_cycle_acu_limit": 1000, "max_session_acu_limit": 50},
    ]
    # Default: no repo permissions set on any org. Tests that care about
    # repo diffing can override this per-test.
    client.list_org_git_permissions.return_value = []
    client.list_users.return_value = [
        {
            "user_id": "u1",
            "email": "existing@company.com",
            "name": "Existing User",
            "role_assignments": [
                {"role": {"role_id": "account_member", "role_name": "Member", "role_type": "enterprise"}, "org_id": None},
                {"role": {"role_id": "org_member", "role_name": "Member", "role_type": "org"}, "org_id": "o1"},
            ],
        },
    ]
    return client


@pytest.fixture
def manager(mock_client):
    return BulkManager(client=mock_client)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------
class TestInitialization:
    def test_initialize_verifies_credentials(self, manager, mock_client):
        manager.initialize()
        mock_client.verify_credentials.assert_called_once()

    def test_initialize_discovers_github_connection(self, manager, mock_client):
        manager.initialize()
        mock_client.get_github_connection_id.assert_called_once()
        assert manager.github_connection_id == "gc-1"

    def test_initialize_builds_role_map(self, manager, mock_client):
        manager.initialize()
        assert manager.role_map["account_admin"] == "account_admin"
        assert manager.role_map["account_member"] == "account_member"
        assert "enterprise_admin" in manager.role_map or "account_admin" in manager.role_map

    def test_initialize_builds_org_map(self, manager, mock_client):
        manager.initialize()
        assert manager.org_map["Existing/Org"] == "o1"

    def test_initialize_builds_user_map(self, manager, mock_client):
        manager.initialize()
        assert manager.user_map["existing@company.com"] == "u1"

    def test_initialize_stores_current_state(self, manager, mock_client):
        manager.initialize()
        assert len(manager.current_orgs) == 1
        assert len(manager.current_users) == 1


# ---------------------------------------------------------------------------
# Validation (SyncValidator)
# ---------------------------------------------------------------------------
class TestSyncValidator:
    def test_valid_enterprise_roles(self):
        v = SyncValidator()
        users = [
            UserRow(email="a@x.com", action="sync", enterprise_role="account_admin"),
            UserRow(email="b@x.com", action="sync", enterprise_role="account_member"),
            UserRow(email="c@x.com", action="sync", enterprise_role="enterprise_member"),
            UserRow(email="d@x.com", action="sync", enterprise_role="enterprise_admin"),
        ]
        result = v.validate_users(users)
        assert result.is_valid, f"Unexpected errors: {result.errors}"

    def test_invalid_enterprise_role(self):
        v = SyncValidator()
        users = [
            UserRow(email="a@x.com", action="sync", enterprise_role="superadmin"),
        ]
        result = v.validate_users(users)
        assert not result.is_valid
        assert any("superadmin" in e for e in result.errors)

    def test_invalid_org_role(self):
        v = SyncValidator()
        users = [
            UserRow(email="a@x.com", action="sync", enterprise_role="account_member",
                    org_name="T/P", org_role="org_superadmin"),
        ]
        result = v.validate_users(users)
        assert not result.is_valid
        assert any("org_superadmin" in e for e in result.errors)

    def test_valid_org_roles(self):
        v = SyncValidator()
        users = [
            UserRow(email="a@x.com", action="sync", enterprise_role="account_member",
                    org_name="T/P", org_role="org_admin"),
            UserRow(email="b@x.com", action="sync", enterprise_role="account_member",
                    org_name="T/P", org_role="org_member"),
            UserRow(email="c@x.com", action="sync", enterprise_role="account_member",
                    org_name="T/P", org_role="org_deepwiki"),
        ]
        result = v.validate_users(users)
        assert result.is_valid

    def test_duplicate_email_warns(self):
        v = SyncValidator()
        users = [
            UserRow(email="a@x.com", action="sync", enterprise_role="account_member"),
            UserRow(email="a@x.com", action="sync", enterprise_role="account_member"),
        ]
        result = v.validate_users(users)
        assert len(result.warnings) >= 1
        assert any("duplicate" in w.lower() for w in result.warnings)

    def test_org_name_convention_valid(self):
        v = SyncValidator()
        orgs = [
            OrgRow(org_name="Engineering/Payments",
                   action="sync", cycle_acu_limit=5000, session_acu_limit=100, repos=[]),
        ]
        result = v.validate_orgs(orgs)
        assert result.is_valid

    def test_org_name_without_slash_is_valid(self):
        v = SyncValidator()
        orgs = [
            OrgRow(org_name="TestOrg",
                   action="sync"),
        ]
        result = v.validate_orgs(orgs)
        assert result.is_valid

    def test_org_name_duplicate(self):
        v = SyncValidator()
        orgs = [
            OrgRow(org_name="T/P", action="sync"),
            OrgRow(org_name="T/P", action="sync"),
        ]
        result = v.validate_orgs(orgs)
        assert not result.is_valid
        assert any("duplicate" in e for e in result.errors)

    def test_negative_acu_limit(self):
        v = SyncValidator()
        orgs = [
            OrgRow(org_name="T/P", action="sync",
                   cycle_acu_limit=-100),
        ]
        result = v.validate_orgs(orgs)
        assert not result.is_valid
        assert any("non-negative" in e for e in result.errors)

    def test_repo_format_valid(self):
        v = SyncValidator()
        orgs = [
            OrgRow(org_name="T/P", action="sync",
                   repos=["myorg/myrepo", "other/repo"]),
        ]
        result = v.validate_repos(orgs, "gc-1", MagicMock())
        assert result.is_valid

    def test_repo_format_invalid_no_slash(self):
        v = SyncValidator()
        orgs = [
            OrgRow(org_name="T/P", action="sync",
                   repos=["just-a-repo"]),
        ]
        result = v.validate_repos(orgs, "gc-1", MagicMock())
        assert not result.is_valid
        assert any("owner/repo" in e for e in result.errors)

    def test_repo_format_invalid_too_many_slashes(self):
        v = SyncValidator()
        orgs = [
            OrgRow(org_name="T/P", action="sync",
                   repos=["a/b/c"]),
        ]
        result = v.validate_repos(orgs, "gc-1", MagicMock())
        assert not result.is_valid

    def test_role_alias_resolution(self):
        v = SyncValidator()
        assert v.resolve_role("enterprise_admin") == "account_admin"
        assert v.resolve_role("enterprise_member") == "account_member"
        assert v.resolve_role("account_admin") == "account_admin"
        assert v.resolve_role("org_member") == "org_member"


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------
class TestOrgDiff:
    def test_org_to_create(self, manager, mock_client):
        manager.initialize()
        desired = [
            OrgRow(org_name="New/Org", action="sync",
                   cycle_acu_limit=5000, session_acu_limit=100, repos=["a/b"]),
        ]
        diff = manager.compute_org_diff(desired)
        assert len(diff.to_create) == 1
        assert diff.to_create[0].org_name == "New/Org"
        # Existing/Org is not in desired → should be deleted
        assert len(diff.to_delete) == 1
        assert diff.to_delete[0]["name"] == "Existing/Org"

    def test_org_to_update(self, manager, mock_client):
        manager.initialize()
        desired = [
            OrgRow(org_name="Existing/Org", action="sync",
                   cycle_acu_limit=2000, session_acu_limit=100, repos=[]),
        ]
        diff = manager.compute_org_diff(desired)
        assert len(diff.to_create) == 0
        assert len(diff.to_delete) == 0
        assert len(diff.to_update) == 1
        assert diff.to_update[0][0].org_name == "Existing/Org"

    def test_org_no_changes(self, manager, mock_client):
        """When desired state matches current state, to_update is empty (true idempotency)."""
        manager.initialize()
        desired = [
            OrgRow(org_name="Existing/Org", action="sync",
                   cycle_acu_limit=1000, session_acu_limit=50, repos=[]),
        ]
        diff = manager.compute_org_diff(desired)
        assert len(diff.to_create) == 0
        assert len(diff.to_delete) == 0
        # Nothing differs → to_update should be empty
        assert len(diff.to_update) == 0

    def test_org_to_delete(self, manager, mock_client):
        manager.initialize()
        desired: list[OrgRow] = []  # empty desired state → delete everything
        diff = manager.compute_org_diff(desired)
        assert len(diff.to_delete) == 1
        assert diff.to_delete[0]["name"] == "Existing/Org"


class TestUserDiff:
    def test_user_to_invite(self, manager, mock_client):
        manager.initialize()
        desired = [
            UserRow(email="new@x.com", action="sync", enterprise_role="account_member"),
            UserRow(email="existing@company.com", action="sync", enterprise_role="account_member"),
        ]
        diff = manager.compute_user_diff(desired)
        assert len(diff.to_invite) == 1
        assert diff.to_invite[0].email == "new@x.com"
        assert len(diff.to_remove) == 0  # existing user is in desired state

    def test_user_to_remove(self, manager, mock_client):
        manager.initialize()
        desired: list[UserRow] = []  # empty → remove all users
        diff = manager.compute_user_diff(desired)
        assert len(diff.to_remove) == 1
        assert diff.to_remove[0]["email"] == "existing@company.com"

    def test_enterprise_role_change(self, manager, mock_client):
        manager.initialize()
        desired = [
            UserRow(email="existing@company.com", action="sync", enterprise_role="account_admin"),
        ]
        diff = manager.compute_user_diff(desired)
        assert len(diff.role_changes) == 1
        assert diff.role_changes[0][0].enterprise_role == "account_admin"

    def test_no_role_change_when_same(self, manager, mock_client):
        manager.initialize()
        desired = [
            UserRow(email="existing@company.com", action="sync", enterprise_role="account_member",
                    org_name="Existing/Org", org_role="org_member"),
        ]
        diff = manager.compute_user_diff(desired)
        assert len(diff.role_changes) == 0
        assert len(diff.org_additions) == 0
        assert len(diff.org_removals) == 0

    def test_org_addition(self, manager, mock_client):
        """User exists but needs to be added to a new org."""
        # Add another org to the map
        mock_client.list_organizations.return_value = [
            {"org_id": "o1", "name": "Existing/Org", "max_cycle_acu_limit": 1000, "max_session_acu_limit": 50},
            {"org_id": "o2", "name": "New/Org", "max_cycle_acu_limit": None, "max_session_acu_limit": None},
        ]
        manager.initialize()
        desired = [
            UserRow(email="existing@company.com", action="sync", enterprise_role="account_member",
                    org_name="Existing/Org", org_role="org_member"),
            UserRow(email="existing@company.com", action="sync", enterprise_role="account_member",
                    org_name="New/Org", org_role="org_admin"),
        ]
        diff = manager.compute_user_diff(desired)
        assert len(diff.org_additions) == 1
        assert diff.org_additions[0][0].org_name == "New/Org"
        assert diff.org_additions[0][1] == "o2"

    def test_org_removal(self, manager, mock_client):
        """User is in an org via API but that org membership is not in CSV."""
        manager.initialize()
        desired = [
            UserRow(email="existing@company.com", action="sync", enterprise_role="account_member"),
            # No org membership specified → should remove from Existing/Org
        ]
        diff = manager.compute_user_diff(desired)
        assert len(diff.org_removals) == 1
        assert diff.org_removals[0][2] == "Existing/Org"

    def test_org_role_change(self, manager, mock_client):
        """User is in org but with different role."""
        manager.initialize()
        desired = [
            UserRow(email="existing@company.com", action="sync", enterprise_role="account_member",
                    org_name="Existing/Org", org_role="org_admin"),
        ]
        diff = manager.compute_user_diff(desired)
        assert len(diff.org_role_changes) == 1
        assert diff.org_role_changes[0][0].org_role == "org_admin"


# ---------------------------------------------------------------------------
# Sync execution
# ---------------------------------------------------------------------------
class TestSyncExecution:
    def test_sync_creates_org(self, manager, mock_client):
        manager.initialize()
        mock_client.create_organization.return_value = {"org_id": "o-new", "name": "New/Org"}
        org_diff = OrgDiff(
            to_create=[OrgRow(org_name="New/Org", action="sync",
                              cycle_acu_limit=5000, session_acu_limit=100, repos=["a/b"])],
        )
        user_diff = UserDiff()
        manager.execute_sync(org_diff, user_diff)
        mock_client.create_organization.assert_called_once()
        mock_client.set_org_acu_limit.assert_called_once_with("o-new", 5000)
        mock_client.set_org_git_permissions.assert_called_once()

    def test_sync_deletes_org(self, manager, mock_client):
        manager.initialize()
        org_diff = OrgDiff(
            to_delete=[{"org_id": "o-old", "name": "Old/Org"}],
        )
        user_diff = UserDiff()
        manager.execute_sync(org_diff, user_diff)
        mock_client.delete_organization.assert_called_once_with("o-old")

    def test_sync_invites_users(self, manager, mock_client):
        manager.initialize()
        mock_client.bulk_invite_users.return_value = [{"invited": ["new@x.com"]}]
        mock_client.list_users.return_value = [{"user_id": "u-new", "email": "new@x.com"}]
        org_diff = OrgDiff()
        user_diff = UserDiff(
            to_invite=[UserRow(email="new@x.com", action="sync", enterprise_role="account_member")],
        )
        manager.execute_sync(org_diff, user_diff)
        mock_client.bulk_invite_users.assert_called_once()

    def test_sync_removes_users(self, manager, mock_client):
        manager.initialize()
        org_diff = OrgDiff()
        user_diff = UserDiff(
            to_remove=[{"user_id": "u-old", "email": "old@x.com"}],
        )
        manager.execute_sync(org_diff, user_diff)
        mock_client.delete_user.assert_called_once_with("u-old")

    def test_sync_updates_enterprise_role(self, manager, mock_client):
        manager.initialize()
        org_diff = OrgDiff()
        user_diff = UserDiff(
            role_changes=[
                (UserRow(email="a@x.com", action="sync", enterprise_role="account_admin"),
                 {"user_id": "u1", "email": "a@x.com"}),
            ],
        )
        manager.execute_sync(org_diff, user_diff)
        mock_client.update_user_enterprise_role.assert_called_once_with("u1", "account_admin")

    def test_sync_dry_run_no_mutations(self, mock_client):
        mgr = BulkManager(client=mock_client, dry_run=True)
        mgr.initialize()
        mock_client.create_organization.return_value = {"org_id": "o-new", "name": "New/Org"}
        org_diff = OrgDiff(
            to_create=[OrgRow(org_name="New/Org", action="sync",
                              cycle_acu_limit=5000, session_acu_limit=100, repos=["a/b"])],
            to_delete=[{"org_id": "o-old", "name": "Old/Org"}],
        )
        user_diff = UserDiff(
            to_invite=[UserRow(email="new@x.com", action="sync", enterprise_role="account_member")],
            to_remove=[{"user_id": "u-old", "email": "old@x.com"}],
        )
        mgr.execute_sync(org_diff, user_diff)
        mock_client.create_organization.assert_not_called()
        mock_client.delete_organization.assert_not_called()
        mock_client.bulk_invite_users.assert_not_called()
        mock_client.delete_user.assert_not_called()
        # But results should be recorded
        assert len(mgr.results) > 0
        assert all(r["status"] == "dry-run" for r in mgr.results)


# ---------------------------------------------------------------------------
# Legacy mode (backward compatibility)
# ---------------------------------------------------------------------------
class TestLegacyOrgAdditions:
    def test_create_org(self, manager, mock_client):
        manager.initialize()
        mock_client.create_organization.return_value = {"org_id": "o-new", "name": "Team/Proj"}
        org_row = OrgRow(
            org_name="Team/Proj",
            action="add", cycle_acu_limit=5000, session_acu_limit=100,
            repos=["myorg/repo1", "myorg/repo2"],
        )
        manager.process_org_additions([org_row])
        mock_client.create_organization.assert_called_once_with(
            "Team/Proj", max_cycle_acu_limit=5000, max_session_acu_limit=100,
        )

    def test_create_org_sets_acu_limit(self, manager, mock_client):
        manager.initialize()
        mock_client.create_organization.return_value = {"org_id": "o-new", "name": "T/P"}
        org_row = OrgRow(
            org_name="T/P",
            action="add", cycle_acu_limit=5000, session_acu_limit=None, repos=[],
        )
        manager.process_org_additions([org_row])
        mock_client.set_org_acu_limit.assert_called_once_with("o-new", 5000)

    def test_create_org_sets_git_permissions(self, manager, mock_client):
        manager.initialize()
        mock_client.create_organization.return_value = {"org_id": "o-new", "name": "T/P"}
        org_row = OrgRow(
            org_name="T/P",
            action="add", cycle_acu_limit=None, session_acu_limit=None,
            repos=["myorg/repo1", "myorg/repo2"],
        )
        manager.process_org_additions([org_row])
        mock_client.set_org_git_permissions.assert_called_once_with(
            "o-new",
            [
                {"git_connection_id": "gc-1", "repo_path": "myorg/repo1"},
                {"git_connection_id": "gc-1", "repo_path": "myorg/repo2"},
            ],
        )

    def test_create_org_no_acu_if_not_set(self, manager, mock_client):
        manager.initialize()
        mock_client.create_organization.return_value = {"org_id": "o-new", "name": "T/P"}
        org_row = OrgRow(
            org_name="T/P",
            action="add", cycle_acu_limit=None, session_acu_limit=None, repos=[],
        )
        manager.process_org_additions([org_row])
        mock_client.set_org_acu_limit.assert_not_called()
        mock_client.set_org_git_permissions.assert_not_called()


class TestLegacyOrgUpdates:
    def test_update_org(self, manager, mock_client):
        manager.initialize()
        org_row = OrgRow(
            org_name="Existing/Org",
            action="update", cycle_acu_limit=6000, session_acu_limit=200,
            repos=["myorg/newrepo"],
        )
        manager.process_org_updates([org_row])
        mock_client.update_organization.assert_called_once_with(
            "o1", max_cycle_acu_limit=6000, max_session_acu_limit=200,
        )
        mock_client.set_org_git_permissions.assert_called_once_with(
            "o1",
            [{"git_connection_id": "gc-1", "repo_path": "myorg/newrepo"}],
        )

    def test_update_org_not_found_records_error(self, manager, mock_client):
        manager.initialize()
        org_row = OrgRow(
            org_name="Unknown/Org",
            action="update", cycle_acu_limit=1000, session_acu_limit=None, repos=[],
        )
        manager.process_org_updates([org_row])
        mock_client.update_organization.assert_not_called()
        assert any(r["status"] == "failed" for r in manager.results)


class TestLegacyOrgRemovals:
    def test_remove_org(self, manager, mock_client):
        manager.initialize()
        org_row = OrgRow(
            org_name="Existing/Org",
            action="remove", cycle_acu_limit=None, session_acu_limit=None, repos=[],
        )
        manager.process_org_removals([org_row])
        mock_client.delete_organization.assert_called_once_with("o1")

    def test_remove_org_not_found_records_error(self, manager, mock_client):
        manager.initialize()
        org_row = OrgRow(
            org_name="Ghost/Org",
            action="remove", cycle_acu_limit=None, session_acu_limit=None, repos=[],
        )
        manager.process_org_removals([org_row])
        mock_client.delete_organization.assert_not_called()
        assert any(r["status"] == "failed" for r in manager.results)


class TestLegacyUserAdditions:
    def test_invite_users_batched(self, manager, mock_client):
        manager.initialize()
        mock_client.bulk_invite_users.return_value = [{"invited": ["a@x.com", "b@x.com"]}]
        mock_client.list_users.side_effect = [
            [{"user_id": "u1", "email": "existing@company.com", "name": "E", "role_assignments": []}],
            [{"user_id": "u-a", "email": "a@x.com"}],
            [{"user_id": "u-b", "email": "b@x.com"}],
        ]
        manager.initialize()

        users = [
            UserRow(email="a@x.com", action="add", enterprise_role="enterprise_member",
                    org_name="Existing/Org", org_role="org_member"),
            UserRow(email="b@x.com", action="add", enterprise_role="enterprise_member",
                    org_name="", org_role=""),
        ]
        manager.process_user_additions(users)
        mock_client.bulk_invite_users.assert_called()

    def test_assign_user_to_org_after_invite(self, manager, mock_client):
        manager.initialize()
        mock_client.bulk_invite_users.return_value = [{"invited": ["a@x.com"]}]
        mock_client.list_users.return_value = [{"user_id": "u-a", "email": "a@x.com"}]

        users = [
            UserRow(email="a@x.com", action="add", enterprise_role="enterprise_member",
                    org_name="Existing/Org", org_role="org_member"),
        ]
        manager.process_user_additions(users)
        mock_client.assign_user_to_org.assert_called_once_with("o1", "u-a", "org_member")


class TestLegacyUserRemovals:
    def test_remove_user_from_enterprise(self, manager, mock_client):
        manager.initialize()
        users = [
            UserRow(email="existing@company.com", action="remove", enterprise_role="",
                    org_name="", org_role=""),
        ]
        manager.process_user_removals(users)
        mock_client.delete_user.assert_called_once_with("u1")

    def test_remove_user_from_org_only(self, manager, mock_client):
        manager.initialize()
        users = [
            UserRow(email="existing@company.com", action="remove", enterprise_role="",
                    org_name="Existing/Org", org_role=""),
        ]
        manager.process_user_removals(users)
        mock_client.remove_user_from_org.assert_called_once_with("o1", "u1")
        mock_client.delete_user.assert_not_called()

    def test_remove_unknown_user_records_error(self, manager, mock_client):
        manager.initialize()
        users = [
            UserRow(email="ghost@company.com", action="remove", enterprise_role="",
                    org_name="", org_role=""),
        ]
        manager.process_user_removals(users)
        mock_client.delete_user.assert_not_called()
        assert any(r["status"] == "failed" for r in manager.results)


# ---------------------------------------------------------------------------
# Dry-run mode (legacy)
# ---------------------------------------------------------------------------
class TestDryRun:
    def test_dry_run_no_mutations(self, mock_client):
        mgr = BulkManager(client=mock_client, dry_run=True)
        mgr.initialize()

        org_rows = [
            OrgRow(org_name="New/Org", action="add",
                   cycle_acu_limit=5000, session_acu_limit=100, repos=["a/b"]),
            OrgRow(org_name="Existing/Org", action="remove",
                   cycle_acu_limit=None, session_acu_limit=None, repos=[]),
        ]
        user_rows = [
            UserRow(email="new@x.com", action="add", enterprise_role="enterprise_member",
                    org_name="New/Org", org_role="org_member"),
            UserRow(email="existing@company.com", action="remove", enterprise_role="",
                    org_name="", org_role=""),
        ]

        mgr.process_org_additions([r for r in org_rows if r.action == "add"])
        mgr.process_org_removals([r for r in org_rows if r.action == "remove"])
        mgr.process_user_additions([r for r in user_rows if r.action == "add"])
        mgr.process_user_removals([r for r in user_rows if r.action == "remove"])

        mock_client.create_organization.assert_not_called()
        mock_client.delete_organization.assert_not_called()
        mock_client.bulk_invite_users.assert_not_called()
        mock_client.delete_user.assert_not_called()
        mock_client.set_org_acu_limit.assert_not_called()
        mock_client.set_org_git_permissions.assert_not_called()
        mock_client.assign_user_to_org.assert_not_called()
        mock_client.remove_user_from_org.assert_not_called()

    def test_dry_run_records_planned_operations(self, mock_client):
        mgr = BulkManager(client=mock_client, dry_run=True)
        mgr.initialize()

        org_rows = [
            OrgRow(org_name="New/Org", action="add",
                   cycle_acu_limit=5000, session_acu_limit=100, repos=[]),
        ]
        mgr.process_org_additions(org_rows)
        assert len(mgr.results) > 0
        assert all(r["status"] == "dry-run" for r in mgr.results)


# ---------------------------------------------------------------------------
# Results reporting
# ---------------------------------------------------------------------------
class TestResultsReporting:
    def test_summary_counts(self, manager, mock_client):
        manager.initialize()
        mock_client.create_organization.return_value = {"org_id": "o-new", "name": "T/P"}
        org_row = OrgRow(
            org_name="T/P",
            action="add", cycle_acu_limit=None, session_acu_limit=None, repos=[],
        )
        manager.process_org_additions([org_row])
        summary = manager.get_summary()
        assert summary["total"] >= 1
        assert summary["succeeded"] >= 1

    def test_results_csv_output(self, manager, mock_client):
        manager.initialize()
        mock_client.create_organization.return_value = {"org_id": "o-new", "name": "T/P"}
        org_row = OrgRow(
            org_name="T/P",
            action="add", cycle_acu_limit=None, session_acu_limit=None, repos=[],
        )
        manager.process_org_additions([org_row])
        csv_output = manager.get_results_csv()
        assert "operation" in csv_output
        assert "status" in csv_output
        assert "create_org" in csv_output


# ---------------------------------------------------------------------------
# Defense-in-depth safeguard tests
# ---------------------------------------------------------------------------
class TestIsManagedOrg:
    """Unit tests for the is_managed_org() helper."""

    def test_convention_org_is_managed(self):
        assert is_managed_org("Engineering/Payments") is True

    def test_non_convention_org_is_unmanaged(self):
        assert is_managed_org("test-api") is False

    def test_empty_string_is_unmanaged(self):
        assert is_managed_org("") is False

    def test_slash_only_is_managed(self):
        # Edge case: "/" alone technically contains a slash
        assert is_managed_org("/") is True


class TestSafeguardLayer1_ComputeOrgDiff:
    """Layer 1: compute_org_diff() must never mark non-convention orgs for deletion."""

    def test_non_convention_org_preserved_during_sync(self, mock_client):
        # API has both a convention org and a non-convention (default) org
        mock_client.list_organizations.return_value = [
            {"org_id": "o1", "name": "Team/Project", "max_cycle_acu_limit": 1000, "max_session_acu_limit": 50},
            {"org_id": "o-default", "name": "my-enterprise", "max_cycle_acu_limit": None, "max_session_acu_limit": None},
        ]
        mgr = BulkManager(client=mock_client)
        mgr.initialize()

        # Desired state only includes Team/Project — "my-enterprise" is NOT in CSV
        desired = [
            OrgRow(org_name="Team/Project", action="sync",
                   cycle_acu_limit=1000, session_acu_limit=50, repos=[]),
        ]
        diff = mgr.compute_org_diff(desired)
        # Non-convention org must NOT appear in to_delete
        deleted_names = [o["name"] for o in diff.to_delete]
        assert "my-enterprise" not in deleted_names

    def test_convention_org_still_deleted(self, mock_client):
        mock_client.list_organizations.return_value = [
            {"org_id": "o1", "name": "Old/Org", "max_cycle_acu_limit": None, "max_session_acu_limit": None},
        ]
        mgr = BulkManager(client=mock_client)
        mgr.initialize()

        desired: list[OrgRow] = []  # empty CSV → convention orgs deleted
        diff = mgr.compute_org_diff(desired)
        assert len(diff.to_delete) == 1
        assert diff.to_delete[0]["name"] == "Old/Org"


class TestSafeguardLayer2_SyncDeleteOrg:
    """Layer 2: _sync_delete_org() must refuse to delete non-convention orgs."""

    def test_non_convention_org_blocked(self, mock_client):
        mgr = BulkManager(client=mock_client)
        mgr.initialize()
        # Simulate a non-convention org somehow ending up in the deletion list
        org = {"org_id": "o-default", "name": "my-enterprise"}
        mgr._sync_delete_org(org)
        # API delete must NOT have been called
        mock_client.delete_organization.assert_not_called()
        # A "skipped" result must be recorded
        assert any(
            r["operation"] == "delete_org" and r["status"] == "skipped"
            for r in mgr.results
        )

    def test_convention_org_allowed(self, mock_client):
        mgr = BulkManager(client=mock_client)
        mgr.initialize()
        org = {"org_id": "o1", "name": "Team/Project"}
        mgr._sync_delete_org(org)
        mock_client.delete_organization.assert_called_once_with("o1")

    def test_non_convention_org_blocked_in_dry_run(self, mock_client):
        mgr = BulkManager(client=mock_client, dry_run=True)
        mgr.initialize()
        org = {"org_id": "o-default", "name": "default-org"}
        mgr._sync_delete_org(org)
        mock_client.delete_organization.assert_not_called()
        assert any(
            r["status"] == "skipped" for r in mgr.results
        )


class TestSafeguardLayer3_ProcessOrgRemovals:
    """Layer 3: process_org_removals() (legacy mode) must refuse non-convention orgs."""

    def test_non_convention_org_blocked_in_legacy(self, mock_client):
        mgr = BulkManager(client=mock_client)
        mgr.initialize()
        mgr.org_map["default-org"] = "o-default"
        row = OrgRow(org_name="default-org", action="remove")
        mgr.process_org_removals([row])
        mock_client.delete_organization.assert_not_called()
        assert any(
            r["operation"] == "delete_org" and r["status"] == "skipped"
            for r in mgr.results
        )

    def test_convention_org_allowed_in_legacy(self, mock_client):
        mgr = BulkManager(client=mock_client)
        mgr.initialize()
        mgr.org_map["Team/Project"] = "o1"
        row = OrgRow(org_name="Team/Project", action="remove")
        mgr.process_org_removals([row])
        mock_client.delete_organization.assert_called_once_with("o1")

    def test_non_convention_org_blocked_in_legacy_dry_run(self, mock_client):
        mgr = BulkManager(client=mock_client, dry_run=True)
        mgr.initialize()
        mgr.org_map["enterprise-default"] = "o-def"
        row = OrgRow(org_name="enterprise-default", action="remove")
        mgr.process_org_removals([row])
        mock_client.delete_organization.assert_not_called()
        assert any(r["status"] == "skipped" for r in mgr.results)


# ---------------------------------------------------------------------------
# Non-convention org membership preservation (sync-mode user diff)
# ---------------------------------------------------------------------------
class TestNonConventionOrgMembershipPreserved:
    """compute_user_diff must never mark non-convention org memberships for removal.

    Users who belong to unmanaged orgs (those without "/" in their name) must
    keep those memberships regardless of whether the CSV lists them, so that
    the enterprise's default org users stay intact during sync.
    """

    def test_user_in_unmanaged_org_not_removed(self, mock_client):
        # Current state: user is a member of an unmanaged org "enterprise-default"
        # AND a managed org "Team/Project".
        mock_client.list_organizations.return_value = [
            {"org_id": "o-default", "name": "enterprise-default"},
            {"org_id": "o1", "name": "Team/Project"},
        ]
        mock_client.list_users.return_value = [
            {
                "user_id": "u1",
                "email": "alice@company.com",
                "role_assignments": [
                    {
                        "role": {"role_id": "account_member", "role_type": "enterprise"},
                        "org_id": None,
                    },
                    {
                        "role": {"role_id": "org_member", "role_type": "org"},
                        "org_id": "o-default",
                    },
                    {
                        "role": {"role_id": "org_member", "role_type": "org"},
                        "org_id": "o1",
                    },
                ],
            }
        ]
        mgr = BulkManager(client=mock_client)
        mgr.initialize()

        desired = [
            UserRow(
                email="alice@company.com",
                action="sync",
                enterprise_role="account_member",
                org_name="Team/Project",
                org_role="org_member",
            )
        ]
        diff = mgr.compute_user_diff(desired)
        removal_org_names = [r[2] for r in diff.org_removals]
        assert "enterprise-default" not in removal_org_names
        assert diff.org_removals == []  # nothing else to remove either

    def test_managed_org_still_removed_when_missing_from_csv(self, mock_client):
        # Baseline check: managed org memberships ARE still removed when the
        # CSV does not list them (so the preservation logic doesn't over-apply).
        mock_client.list_organizations.return_value = [
            {"org_id": "o1", "name": "Team/Project"},
            {"org_id": "o2", "name": "Team/Other"},
        ]
        mock_client.list_users.return_value = [
            {
                "user_id": "u1",
                "email": "alice@company.com",
                "role_assignments": [
                    {
                        "role": {"role_id": "account_member", "role_type": "enterprise"},
                        "org_id": None,
                    },
                    {
                        "role": {"role_id": "org_member", "role_type": "org"},
                        "org_id": "o1",
                    },
                    {
                        "role": {"role_id": "org_member", "role_type": "org"},
                        "org_id": "o2",
                    },
                ],
            }
        ]
        mgr = BulkManager(client=mock_client)
        mgr.initialize()

        desired = [
            UserRow(
                email="alice@company.com",
                action="sync",
                enterprise_role="account_member",
                org_name="Team/Project",
                org_role="org_member",
            )
        ]
        diff = mgr.compute_user_diff(desired)
        removal_org_names = [r[2] for r in diff.org_removals]
        assert "Team/Other" in removal_org_names


# ---------------------------------------------------------------------------
# --pull / export feature
# ---------------------------------------------------------------------------
class TestExportCurrentState:
    """export_current_state() builds sync-format rows from live API state."""

    def test_export_filters_unmanaged_orgs_by_default(self, mock_client):
        mock_client.list_organizations.return_value = [
            {
                "org_id": "o1",
                "name": "Engineering/Payments",
                "max_cycle_acu_limit": 5000,
                "max_session_acu_limit": 100,
            },
            {
                "org_id": "o2",
                "name": "testing-bulk",  # non-convention, unmanaged
                "max_cycle_acu_limit": None,
                "max_session_acu_limit": None,
            },
        ]
        mock_client.list_users.return_value = []
        mock_client.list_acu_limits.return_value = []
        mock_client.list_org_git_permissions.return_value = [
            {"git_connection_id": "gc-1", "repo_path": "usacognition/test-repo"},
        ]
        mgr = BulkManager(client=mock_client)
        mgr.initialize()

        org_rows, user_rows = mgr.export_current_state()
        assert len(org_rows) == 1
        assert org_rows[0].org_name == "Engineering/Payments"
        assert org_rows[0].cycle_acu_limit == 5000
        assert org_rows[0].session_acu_limit == 100
        assert org_rows[0].repos == ["usacognition/test-repo"]
        assert org_rows[0].action == "sync"
        assert user_rows == []

    def test_export_include_unmanaged_orgs_flag(self, mock_client):
        mock_client.list_organizations.return_value = [
            {
                "org_id": "o1",
                "name": "Engineering/Payments",
                "max_cycle_acu_limit": None,
                "max_session_acu_limit": None,
            },
            {
                "org_id": "o2",
                "name": "testing-bulk",
                "max_cycle_acu_limit": None,
                "max_session_acu_limit": None,
            },
        ]
        mock_client.list_users.return_value = []
        mock_client.list_acu_limits.return_value = []
        mock_client.list_org_git_permissions.return_value = []

        mgr = BulkManager(client=mock_client)
        mgr.initialize()
        org_rows, _ = mgr.export_current_state(include_unmanaged_orgs=True)

        names = sorted(r.org_name for r in org_rows)
        assert names == ["Engineering/Payments", "testing-bulk"]
        unmanaged = [r for r in org_rows if r.org_name == "testing-bulk"][0]
        assert unmanaged.org_name == "testing-bulk"

    def test_export_users_with_and_without_memberships(self, mock_client):
        mock_client.list_organizations.return_value = [
            {
                "org_id": "o1",
                "name": "Engineering/Payments",
                "max_cycle_acu_limit": None,
                "max_session_acu_limit": None,
            },
            {
                "org_id": "o2",
                "name": "Analytics/Dashboard",
                "max_cycle_acu_limit": None,
                "max_session_acu_limit": None,
            },
        ]
        mock_client.list_users.return_value = [
            {
                "user_id": "u1",
                "email": "alice@company.com",
                "role_assignments": [
                    {
                        "role": {"role_id": "account_admin", "role_type": "enterprise"},
                        "org_id": None,
                    },
                    {
                        "role": {"role_id": "org_admin", "role_type": "org"},
                        "org_id": "o1",
                    },
                    {
                        "role": {"role_id": "org_member", "role_type": "org"},
                        "org_id": "o2",
                    },
                ],
            },
            {
                "user_id": "u2",
                "email": "bob@company.com",
                "role_assignments": [
                    {
                        "role": {"role_id": "account_member", "role_type": "enterprise"},
                        "org_id": None,
                    },
                ],
            },
        ]
        mock_client.list_acu_limits.return_value = []
        mock_client.list_org_git_permissions.return_value = []

        mgr = BulkManager(client=mock_client)
        mgr.initialize()
        _, user_rows = mgr.export_current_state()

        # Alice has 2 managed memberships → 2 rows. Bob has no membership → 1 row.
        assert len(user_rows) == 3
        alice_rows = [r for r in user_rows if r.email == "alice@company.com"]
        bob_rows = [r for r in user_rows if r.email == "bob@company.com"]
        assert len(alice_rows) == 2
        assert len(bob_rows) == 1
        assert bob_rows[0].enterprise_role == "account_member"
        assert bob_rows[0].org_name == ""
        assert bob_rows[0].org_role == ""

        alice_by_org = {r.org_name: r for r in alice_rows}
        assert alice_by_org["Engineering/Payments"].org_role == "org_admin"
        assert alice_by_org["Analytics/Dashboard"].org_role == "org_member"
        assert all(r.enterprise_role == "account_admin" for r in alice_rows)

    def test_export_skips_unmanaged_org_memberships(self, mock_client):
        """A user in an unmanaged org should not have that membership emitted."""
        mock_client.list_organizations.return_value = [
            {
                "org_id": "o1",
                "name": "Engineering/Payments",
                "max_cycle_acu_limit": None,
                "max_session_acu_limit": None,
            },
            {
                "org_id": "o-default",
                "name": "testing-bulk",
                "max_cycle_acu_limit": None,
                "max_session_acu_limit": None,
            },
        ]
        mock_client.list_users.return_value = [
            {
                "user_id": "u1",
                "email": "alice@company.com",
                "role_assignments": [
                    {
                        "role": {"role_id": "account_admin", "role_type": "enterprise"},
                        "org_id": None,
                    },
                    {
                        "role": {"role_id": "org_member", "role_type": "org"},
                        "org_id": "o-default",  # unmanaged — should be skipped
                    },
                    {
                        "role": {"role_id": "org_admin", "role_type": "org"},
                        "org_id": "o1",
                    },
                ],
            }
        ]
        mock_client.list_acu_limits.return_value = []
        mock_client.list_org_git_permissions.return_value = []

        mgr = BulkManager(client=mock_client)
        mgr.initialize()
        _, user_rows = mgr.export_current_state()

        # Only the managed membership should be present.
        assert len(user_rows) == 1
        assert user_rows[0].org_name == "Engineering/Payments"
        assert user_rows[0].org_role == "org_admin"

    def test_export_acu_limits_endpoint_preferred(self, mock_client):
        mock_client.list_organizations.return_value = [
            {
                "org_id": "o1",
                "name": "Engineering/Payments",
                "max_cycle_acu_limit": 1000,  # stale org field
                "max_session_acu_limit": 20,
            },
        ]
        mock_client.list_users.return_value = []
        mock_client.list_acu_limits.return_value = [
            {"org_id": "o1", "cycle_acu_limit": 9999},
        ]
        mock_client.list_org_git_permissions.return_value = []

        mgr = BulkManager(client=mock_client)
        mgr.initialize()
        org_rows, _ = mgr.export_current_state()
        assert org_rows[0].cycle_acu_limit == 9999
        # Falls back to org field when dedicated endpoint doesn't provide it.
        assert org_rows[0].session_acu_limit == 20

    def test_write_exported_state_round_trip(self, mock_client, tmp_path):
        """Exported CSVs can be re-parsed via parse_*_file producing equal rows."""
        from user_management.bulk.spreadsheet import parse_orgs_file, parse_users_file

        mock_client.list_organizations.return_value = [
            {
                "org_id": "o1",
                "name": "Engineering/Payments",
                "max_cycle_acu_limit": 5000,
                "max_session_acu_limit": 100,
            },
        ]
        mock_client.list_users.return_value = [
            {
                "user_id": "u1",
                "email": "alice@company.com",
                "role_assignments": [
                    {
                        "role": {"role_id": "account_admin", "role_type": "enterprise"},
                        "org_id": None,
                    },
                    {
                        "role": {"role_id": "org_admin", "role_type": "org"},
                        "org_id": "o1",
                    },
                ],
            }
        ]
        mock_client.list_acu_limits.return_value = []
        mock_client.list_org_git_permissions.return_value = [
            {"git_connection_id": "gc-1", "repo_path": "org/repo1"},
            {"git_connection_id": "gc-1", "repo_path": "org/repo2"},
        ]

        mgr = BulkManager(client=mock_client)
        mgr.initialize()

        orgs_out = tmp_path / "orgs.csv"
        users_out = tmp_path / "users.csv"
        num_orgs, num_users = mgr.write_exported_state(
            orgs_out=str(orgs_out), users_out=str(users_out)
        )
        assert num_orgs == 1
        assert num_users == 1

        parsed_orgs = parse_orgs_file(str(orgs_out))
        parsed_users = parse_users_file(str(users_out))

        assert len(parsed_orgs) == 1
        assert parsed_orgs[0].org_name == "Engineering/Payments"
        assert parsed_orgs[0].action == "sync"
        assert parsed_orgs[0].cycle_acu_limit == 5000
        assert parsed_orgs[0].session_acu_limit == 100
        assert sorted(parsed_orgs[0].repos) == ["org/repo1", "org/repo2"]

        assert len(parsed_users) == 1
        assert parsed_users[0].email == "alice@company.com"
        assert parsed_users[0].enterprise_role == "account_admin"
        assert parsed_users[0].org_name == "Engineering/Payments"
        assert parsed_users[0].org_role == "org_admin"
        assert parsed_users[0].action == "sync"

    def test_exported_state_is_idempotent_via_sync(self, mock_client, tmp_path):
        """Re-feeding the exported CSVs into compute_*_diff yields no changes."""
        from user_management.bulk.spreadsheet import parse_orgs_file, parse_users_file

        mock_client.list_organizations.return_value = [
            {
                "org_id": "o1",
                "name": "Engineering/Payments",
                "max_cycle_acu_limit": 5000,
                "max_session_acu_limit": 100,
            },
            {
                "org_id": "o-default",
                "name": "testing-bulk",
                "max_cycle_acu_limit": None,
                "max_session_acu_limit": None,
            },
        ]
        mock_client.list_users.return_value = [
            {
                "user_id": "u1",
                "email": "alice@company.com",
                "role_assignments": [
                    {
                        "role": {"role_id": "account_admin", "role_type": "enterprise"},
                        "org_id": None,
                    },
                    {
                        "role": {"role_id": "org_admin", "role_type": "org"},
                        "org_id": "o1",
                    },
                    {
                        "role": {"role_id": "org_member", "role_type": "org"},
                        "org_id": "o-default",
                    },
                ],
            }
        ]
        mock_client.list_acu_limits.return_value = []
        mock_client.list_org_git_permissions.return_value = [
            {"git_connection_id": "gc-1", "repo_path": "org/repo1"},
        ]

        mgr = BulkManager(client=mock_client)
        mgr.initialize()
        orgs_out = tmp_path / "orgs.csv"
        users_out = tmp_path / "users.csv"
        mgr.write_exported_state(str(orgs_out), str(users_out))

        parsed_orgs = parse_orgs_file(str(orgs_out))
        parsed_users = parse_users_file(str(users_out))

        org_diff = mgr.compute_org_diff(parsed_orgs)
        user_diff = mgr.compute_user_diff(parsed_users)

        # The managed org exists and has no changes; the unmanaged org must
        # not be marked for deletion.
        assert org_diff.to_create == []
        assert org_diff.to_delete == []
        # Users: no invites, no removals, no role changes, no membership churn.
        assert user_diff.to_invite == []
        assert user_diff.to_remove == []
        assert user_diff.role_changes == []
        assert user_diff.org_additions == []
        assert user_diff.org_removals == []
        assert user_diff.org_role_changes == []
