"""Unit tests for spreadsheet parser — covers legacy and desired-state formats."""
import io

import pytest

from user_management.bulk.spreadsheet import (
    OrgRow,
    UserRow,
    parse_orgs_csv,
    parse_orgs_csv_sync,
    parse_orgs_file,
    parse_users_csv,
    parse_users_csv_sync,
    parse_users_file,
    SpreadsheetValidationError,
)


# ---------------------------------------------------------------------------
# Users CSV parsing (legacy — with action column)
# ---------------------------------------------------------------------------
class TestParseUsersCSV:
    def test_parse_valid_csv(self):
        csv_content = (
            "email,action,enterprise_role,org_name,org_role\n"
            "alice@company.com,add,enterprise_member,Engineering/Payments,org_member\n"
            "bob@company.com,add,enterprise_admin,,,\n"
        )
        rows = parse_users_csv(io.StringIO(csv_content))
        assert len(rows) == 2
        assert isinstance(rows[0], UserRow)
        assert rows[0].email == "alice@company.com"
        assert rows[0].action == "add"
        assert rows[0].enterprise_role == "enterprise_member"
        assert rows[0].org_name == "Engineering/Payments"
        assert rows[0].org_role == "org_member"

    def test_parse_user_without_org(self):
        csv_content = (
            "email,action,enterprise_role,org_name,org_role\n"
            "bob@company.com,add,enterprise_admin,,\n"
        )
        rows = parse_users_csv(io.StringIO(csv_content))
        assert rows[0].org_name == ""
        assert rows[0].org_role == ""

    def test_parse_remove_action(self):
        csv_content = (
            "email,action,enterprise_role,org_name,org_role\n"
            "charlie@company.com,remove,,,\n"
        )
        rows = parse_users_csv(io.StringIO(csv_content))
        assert rows[0].action == "remove"

    def test_default_org_role_when_org_name_set_but_role_missing(self):
        csv_content = (
            "email,action,enterprise_role,org_name,org_role\n"
            "eve@company.com,add,enterprise_member,Engineering/Payments,\n"
        )
        rows = parse_users_csv(io.StringIO(csv_content))
        assert rows[0].org_role == "org_member"

    def test_error_on_missing_email(self):
        csv_content = (
            "email,action,enterprise_role,org_name,org_role\n"
            ",add,enterprise_member,,\n"
        )
        with pytest.raises(SpreadsheetValidationError, match="email"):
            parse_users_csv(io.StringIO(csv_content))

    def test_error_on_invalid_action(self):
        csv_content = (
            "email,action,enterprise_role,org_name,org_role\n"
            "alice@company.com,update,enterprise_member,,\n"
        )
        with pytest.raises(SpreadsheetValidationError, match="action"):
            parse_users_csv(io.StringIO(csv_content))

    def test_error_on_invalid_email_format(self):
        csv_content = (
            "email,action,enterprise_role,org_name,org_role\n"
            "not-an-email,add,enterprise_member,,\n"
        )
        with pytest.raises(SpreadsheetValidationError, match="email"):
            parse_users_csv(io.StringIO(csv_content))

    def test_error_on_missing_action(self):
        csv_content = (
            "email,action,enterprise_role,org_name,org_role\n"
            "alice@company.com,,enterprise_member,,\n"
        )
        with pytest.raises(SpreadsheetValidationError, match="action"):
            parse_users_csv(io.StringIO(csv_content))

    def test_parse_multiple_rows(self):
        csv_content = (
            "email,action,enterprise_role,org_name,org_role\n"
            "alice@company.com,add,enterprise_member,Engineering/Payments,org_member\n"
            "bob@company.com,add,enterprise_admin,,\n"
            "charlie@company.com,remove,,,\n"
            "dave@company.com,add,enterprise_member,Analytics/Dashboard,org_admin\n"
        )
        rows = parse_users_csv(io.StringIO(csv_content))
        assert len(rows) == 4


# ---------------------------------------------------------------------------
# Users CSV parsing (desired-state — no action column)
# ---------------------------------------------------------------------------
class TestParseUsersCSVSync:
    def test_parse_sync_csv(self):
        csv_content = (
            "email,enterprise_role,org_name,org_role\n"
            "alice@company.com,account_member,Engineering/Payments,org_member\n"
            "bob@company.com,account_admin,,\n"
        )
        rows = parse_users_csv_sync(io.StringIO(csv_content))
        assert len(rows) == 2
        assert rows[0].action == "sync"
        assert rows[0].email == "alice@company.com"
        assert rows[0].enterprise_role == "account_member"
        assert rows[0].org_name == "Engineering/Payments"
        assert rows[1].action == "sync"
        assert rows[1].org_name == ""

    def test_sync_default_enterprise_role(self):
        csv_content = (
            "email,enterprise_role,org_name,org_role\n"
            "alice@company.com,,,\n"
        )
        rows = parse_users_csv_sync(io.StringIO(csv_content))
        assert rows[0].enterprise_role == "account_member"

    def test_sync_default_org_role(self):
        csv_content = (
            "email,enterprise_role,org_name,org_role\n"
            "alice@company.com,account_member,Engineering/Payments,\n"
        )
        rows = parse_users_csv_sync(io.StringIO(csv_content))
        assert rows[0].org_role == "org_member"

    def test_sync_error_on_missing_email(self):
        csv_content = (
            "email,enterprise_role,org_name,org_role\n"
            ",account_member,,\n"
        )
        with pytest.raises(SpreadsheetValidationError, match="email"):
            parse_users_csv_sync(io.StringIO(csv_content))

    def test_sync_error_on_invalid_email(self):
        csv_content = (
            "email,enterprise_role,org_name,org_role\n"
            "not-an-email,account_member,,\n"
        )
        with pytest.raises(SpreadsheetValidationError, match="email"):
            parse_users_csv_sync(io.StringIO(csv_content))


# ---------------------------------------------------------------------------
# Orgs CSV parsing (legacy — with action column)
# ---------------------------------------------------------------------------
class TestParseOrgsCSV:
    def test_parse_valid_csv(self):
        csv_content = (
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,add,5000,100,myorg/repo1;myorg/repo2\n"
        )
        rows = parse_orgs_csv(io.StringIO(csv_content))
        assert len(rows) == 1
        assert isinstance(rows[0], OrgRow)
        assert rows[0].org_name == "Engineering/Payments"
        assert rows[0].action == "add"
        assert rows[0].cycle_acu_limit == 5000
        assert rows[0].session_acu_limit == 100
        assert rows[0].repos == ["myorg/repo1", "myorg/repo2"]

    def test_parse_org_without_repos(self):
        csv_content = (
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Analytics/Dashboard,add,3000,50,\n"
        )
        rows = parse_orgs_csv(io.StringIO(csv_content))
        assert rows[0].repos == []

    def test_parse_org_remove(self):
        csv_content = (
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Legacy/OldApp,remove,,\n"
        )
        rows = parse_orgs_csv(io.StringIO(csv_content))
        assert rows[0].action == "remove"
        assert rows[0].cycle_acu_limit is None
        assert rows[0].session_acu_limit is None

    def test_error_on_missing_org_name(self):
        csv_content = (
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            ",add,5000,100,\n"
        )
        with pytest.raises(SpreadsheetValidationError, match="org_name"):
            parse_orgs_csv(io.StringIO(csv_content))

    def test_error_on_invalid_action(self):
        csv_content = (
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,delete,5000,100,\n"
        )
        with pytest.raises(SpreadsheetValidationError, match="action"):
            parse_orgs_csv(io.StringIO(csv_content))

    def test_valid_update_action(self):
        csv_content = (
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,update,6000,150,\n"
        )
        rows = parse_orgs_csv(io.StringIO(csv_content))
        assert rows[0].action == "update"
        assert rows[0].cycle_acu_limit == 6000

    def test_acu_limits_parsed_as_int(self):
        csv_content = (
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,add,5000,100,\n"
        )
        rows = parse_orgs_csv(io.StringIO(csv_content))
        assert isinstance(rows[0].cycle_acu_limit, int)
        assert isinstance(rows[0].session_acu_limit, int)

    def test_repos_parsed_as_semicolon_separated_list(self):
        csv_content = (
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,add,5000,100,org/r1;org/r2;org/r3\n"
        )
        rows = parse_orgs_csv(io.StringIO(csv_content))
        assert rows[0].repos == ["org/r1", "org/r2", "org/r3"]

    def test_parse_multiple_rows(self):
        csv_content = (
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,add,5000,100,myorg/repo1;myorg/repo2\n"
            "Analytics/Dashboard,add,3000,50,\n"
            "Legacy/OldApp,remove,,\n"
        )
        rows = parse_orgs_csv(io.StringIO(csv_content))
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# Orgs CSV parsing (desired-state — no action column)
# ---------------------------------------------------------------------------
class TestParseOrgsCSVSync:
    def test_parse_sync_csv(self):
        csv_content = (
            "org_name,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,5000,100,myorg/repo1;myorg/repo2\n"
        )
        rows = parse_orgs_csv_sync(io.StringIO(csv_content))
        assert len(rows) == 1
        assert rows[0].action == "sync"
        assert rows[0].org_name == "Engineering/Payments"
        assert rows[0].cycle_acu_limit == 5000
        assert rows[0].repos == ["myorg/repo1", "myorg/repo2"]

    def test_sync_no_limits(self):
        csv_content = (
            "org_name,cycle_acu_limit,session_acu_limit,repos\n"
            "Analytics/Dashboard,,,\n"
        )
        rows = parse_orgs_csv_sync(io.StringIO(csv_content))
        assert rows[0].cycle_acu_limit is None
        assert rows[0].session_acu_limit is None

    def test_sync_error_on_missing_org_name(self):
        csv_content = (
            "org_name,cycle_acu_limit,session_acu_limit,repos\n"
            ",1000,,\n"
        )
        with pytest.raises(SpreadsheetValidationError, match="org_name"):
            parse_orgs_csv_sync(io.StringIO(csv_content))


# ---------------------------------------------------------------------------
# Auto-detection: file-level entry points
# ---------------------------------------------------------------------------
class TestAutoDetection:
    def test_users_file_detects_legacy(self, tmp_path):
        csv_file = tmp_path / "users.csv"
        csv_file.write_text(
            "email,action,enterprise_role,org_name,org_role\n"
            "alice@company.com,add,enterprise_member,Engineering/Payments,org_member\n"
        )
        rows = parse_users_file(str(csv_file))
        assert len(rows) == 1
        assert rows[0].action == "add"

    def test_users_file_detects_sync(self, tmp_path):
        csv_file = tmp_path / "users.csv"
        csv_file.write_text(
            "email,enterprise_role,org_name,org_role\n"
            "alice@company.com,account_member,Engineering/Payments,org_member\n"
        )
        rows = parse_users_file(str(csv_file))
        assert len(rows) == 1
        assert rows[0].action == "sync"

    def test_orgs_file_detects_legacy(self, tmp_path):
        csv_file = tmp_path / "orgs.csv"
        csv_file.write_text(
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,add,5000,100,myorg/repo1\n"
        )
        rows = parse_orgs_file(str(csv_file))
        assert len(rows) == 1
        assert rows[0].action == "add"

    def test_orgs_file_detects_sync(self, tmp_path):
        csv_file = tmp_path / "orgs.csv"
        csv_file.write_text(
            "org_name,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,5000,100,myorg/repo1\n"
        )
        rows = parse_orgs_file(str(csv_file))
        assert len(rows) == 1
        assert rows[0].action == "sync"


# ---------------------------------------------------------------------------
# File-based parsing (CSV + XLSX)
# ---------------------------------------------------------------------------
class TestParseFiles:
    def test_parse_users_csv_file(self, tmp_path):
        csv_file = tmp_path / "users.csv"
        csv_file.write_text(
            "email,action,enterprise_role,org_name,org_role\n"
            "alice@company.com,add,enterprise_member,Engineering/Payments,org_member\n"
        )
        rows = parse_users_file(str(csv_file))
        assert len(rows) == 1
        assert rows[0].email == "alice@company.com"

    def test_parse_orgs_csv_file(self, tmp_path):
        csv_file = tmp_path / "orgs.csv"
        csv_file.write_text(
            "org_name,action,cycle_acu_limit,session_acu_limit,repos\n"
            "Engineering/Payments,add,5000,100,myorg/repo1\n"
        )
        rows = parse_orgs_file(str(csv_file))
        assert len(rows) == 1
        assert rows[0].org_name == "Engineering/Payments"

    def test_parse_users_xlsx_file(self, tmp_path):
        """Test XLSX parsing using openpyxl to create a real file."""
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["email", "action", "enterprise_role", "org_name", "org_role"])
        ws.append(["alice@company.com", "add", "enterprise_member", "Engineering/Payments", "org_member"])
        xlsx_path = tmp_path / "users.xlsx"
        wb.save(str(xlsx_path))

        rows = parse_users_file(str(xlsx_path))
        assert len(rows) == 1
        assert rows[0].email == "alice@company.com"
        assert rows[0].org_name == "Engineering/Payments"

    def test_parse_orgs_xlsx_file(self, tmp_path):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["org_name", "action", "cycle_acu_limit", "session_acu_limit", "repos"])
        ws.append(["Engineering/Payments", "add", 5000, 100, "myorg/repo1;myorg/repo2"])
        xlsx_path = tmp_path / "orgs.xlsx"
        wb.save(str(xlsx_path))

        rows = parse_orgs_file(str(xlsx_path))
        assert len(rows) == 1
        assert rows[0].repos == ["myorg/repo1", "myorg/repo2"]

    def test_unsupported_file_extension(self, tmp_path):
        txt_file = tmp_path / "data.txt"
        txt_file.write_text("some data")
        with pytest.raises(ValueError, match="Unsupported"):
            parse_users_file(str(txt_file))
