"""Tests for the layered email-resolution logic in github_sync.sync."""

from __future__ import annotations

from user_management.core.models import DevinUser
from user_management.github_sync.sync import (
    _is_numeric_email,
    build_email_lookup,
    resolve_gh_login_to_devin_user,
)


def _u(user_id: str, email: str | None = None, name: str | None = None) -> DevinUser:
    return DevinUser(user_id=user_id, email=email, name=name)


# ---------------------------------------------------------------------------
# build_email_lookup
# ---------------------------------------------------------------------------
class TestBuildEmailLookup:
    def test_email_takes_priority_over_name(self) -> None:
        users = [_u("u1", email="alice@x.com", name="alice@x.com")]
        m = build_email_lookup(users)
        assert m["alice@x.com"].user_id == "u1"

    def test_users_without_email_are_indexed_by_name(self) -> None:
        users = [_u("u1", name="alice@x.com")]
        m = build_email_lookup(users)
        assert m["alice@x.com"].user_id == "u1"

    def test_users_without_email_or_name_are_skipped(self) -> None:
        users = [_u("u1")]
        assert build_email_lookup(users) == {}

    def test_lowercases_keys(self) -> None:
        users = [_u("u1", email="Alice@X.com")]
        m = build_email_lookup(users)
        assert "alice@x.com" in m


# ---------------------------------------------------------------------------
# _is_numeric_email
# ---------------------------------------------------------------------------
class TestIsNumericEmail:
    def test_pure_numeric_local_part(self) -> None:
        assert _is_numeric_email("123456@cognizant.com") is True

    def test_alpha_local_part_is_not_numeric(self) -> None:
        assert _is_numeric_email("alice@example.com") is False

    def test_mixed_local_part_is_not_numeric(self) -> None:
        assert _is_numeric_email("alice123@example.com") is False


# ---------------------------------------------------------------------------
# resolve_gh_login_to_devin_user
# ---------------------------------------------------------------------------
class TestResolveGhLogin:
    def test_profile_email_takes_priority(self) -> None:
        users = [
            _u("u1", email="alice@real.com"),
            _u("u2", email="123@saml.com"),
        ]
        m = build_email_lookup(users)
        match = resolve_gh_login_to_devin_user(
            "alice",
            email_lookup=m,
            saml_map={"alice": "123@saml.com"},
            audit_map={},
            profile_emails={"alice": "alice@real.com"},
        )
        assert match is not None
        assert match.user_id == "u1"

    def test_audit_log_email_used_when_no_profile(self) -> None:
        users = [_u("u1", email="alice@real.com")]
        m = build_email_lookup(users)
        match = resolve_gh_login_to_devin_user(
            "alice",
            email_lookup=m,
            saml_map={},
            audit_map={"alice": "alice@real.com"},
            profile_emails={},
        )
        assert match is not None and match.user_id == "u1"

    def test_username_fallback(self) -> None:
        users = [_u("u1", name="alice")]
        m = build_email_lookup(users)
        match = resolve_gh_login_to_devin_user(
            "alice",
            email_lookup=m,
            saml_map={},
            audit_map={},
            profile_emails={},
        )
        assert match is not None and match.user_id == "u1"

    def test_numeric_saml_used_as_last_resort(self) -> None:
        users = [_u("u1", email="123@saml.com")]
        m = build_email_lookup(users)
        match = resolve_gh_login_to_devin_user(
            "alice",
            email_lookup=m,
            saml_map={"alice": "123@saml.com"},
            audit_map={},
            profile_emails={},
        )
        assert match is not None and match.user_id == "u1"

    def test_no_match_returns_none(self) -> None:
        assert (
            resolve_gh_login_to_devin_user(
                "ghost",
                email_lookup={},
                saml_map={},
                audit_map={},
                profile_emails={},
            )
            is None
        )

    def test_non_numeric_saml_preferred_over_username(self) -> None:
        # Both name and SAML-resolved email map to different users; SAML wins
        # because it's a higher-priority source.
        users = [
            _u("u-saml", email="alice@example.com"),
            _u("u-name", name="alice"),
        ]
        m = build_email_lookup(users)
        match = resolve_gh_login_to_devin_user(
            "alice",
            email_lookup=m,
            saml_map={"alice": "alice@example.com"},
            audit_map={},
            profile_emails={},
        )
        assert match is not None and match.user_id == "u-saml"
