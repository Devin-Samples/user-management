"""Tests for find_github_connection — the git connection resolver."""

from __future__ import annotations

from user_management.core.models import DevinGitConnection
from user_management.github_sync.sync import find_github_connection


def _c(name: str, kind: str, gid: str = "c-1") -> DevinGitConnection:
    return DevinGitConnection(
        git_connection_id=gid, git_provider_type=kind, name=name, host="github.com"
    )


class TestFindGitHubConnection:
    def test_returns_none_when_no_github_connection(self) -> None:
        conns = [_c("gitlab.com", "gitlab_token")]
        assert find_github_connection(conns) is None

    def test_prefers_github_app_over_token(self) -> None:
        conns = [
            _c("acme", "github_token", "c-tok"),
            _c("acme", "github_app", "c-app"),
        ]
        result = find_github_connection(conns)
        assert result is not None
        assert result.git_connection_id == "c-app"

    def test_matches_by_name_when_github_org_provided(self) -> None:
        conns = [
            _c("acme", "github_app", "c-acme"),
            _c("other", "github_app", "c-other"),
        ]
        result = find_github_connection(conns, github_org="acme")
        assert result is not None
        assert result.git_connection_id == "c-acme"

    def test_returns_none_when_github_org_does_not_match(self) -> None:
        conns = [_c("acme", "github_app", "c-acme")]
        assert find_github_connection(conns, github_org="other") is None

    def test_name_match_is_case_insensitive(self) -> None:
        conns = [_c("AcMe", "github_app", "c-acme")]
        result = find_github_connection(conns, github_org="ACME")
        assert result is not None
        assert result.git_connection_id == "c-acme"
