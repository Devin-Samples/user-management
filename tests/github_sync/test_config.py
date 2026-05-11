"""Tests for github_sync YAML config loading and state file persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from user_management.github_sync.config import (
    load_config,
    load_state_file,
    save_state_file,
)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------
class TestLoadConfig:
    def test_single_org_shorthand(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "github_org: my-org\n"
            "team_mappings:\n"
            "  - github_team_slug: backend\n"
            "    devin_org_id: org-1\n"
        )
        cfg = load_config(cfg_path)
        assert len(cfg.github_orgs) == 1
        assert cfg.github_orgs[0].github_org == "my-org"
        assert len(cfg.github_orgs[0].team_mappings) == 1

    def test_multi_org_list(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "github_orgs:\n"
            "  - github_org: org-a\n"
            "  - github_org: org-b\n"
        )
        cfg = load_config(cfg_path)
        assert {o.github_org for o in cfg.github_orgs} == {"org-a", "org-b"}

    def test_auto_mode_when_no_team_mappings(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("github_org: my-org\n")
        cfg = load_config(cfg_path)
        assert cfg.is_auto_mode is True

    def test_legacy_mode_when_mappings_present(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "github_org: my-org\n"
            "team_mappings:\n"
            "  - github_team_slug: backend\n"
            "    devin_org_id: org-1\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.is_auto_mode is False

    def test_missing_file_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            load_config(tmp_path / "missing.yaml")

    def test_empty_file_exits(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "empty.yaml"
        cfg_path.write_text("")
        with pytest.raises(SystemExit):
            load_config(cfg_path)

    def test_missing_github_org_exits(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text("default_member_role: member\n")
        with pytest.raises(SystemExit):
            load_config(cfg_path)


# ---------------------------------------------------------------------------
# state file
# ---------------------------------------------------------------------------
class TestStateFile:
    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("github_org: my-org\n")
        team_map = {"team-a": {"org_id": "org-a", "cached_org_name": "my-org-team-a"}}
        save_state_file(cfg_path, "my-org", team_map)
        loaded = load_state_file(cfg_path, "my-org")
        assert loaded == team_map

    def test_missing_state_returns_empty(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("github_org: my-org\n")
        assert load_state_file(cfg_path, "my-org") == {}

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("github_org: my-org\n")
        save_state_file(
            cfg_path,
            "my-org",
            {"team-a": {"org_id": "org-a", "cached_org_name": "x"}},
            dry_run=True,
        )
        assert not (tmp_path / "sync-state.json").exists()

    def test_multiple_orgs_in_one_file(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("github_org: my-org\n")
        save_state_file(
            cfg_path,
            "org-a",
            {"team-a": {"org_id": "org-1", "cached_org_name": "x"}},
        )
        save_state_file(
            cfg_path,
            "org-b",
            {"team-b": {"org_id": "org-2", "cached_org_name": "y"}},
        )
        assert load_state_file(cfg_path, "org-a") == {
            "team-a": {"org_id": "org-1", "cached_org_name": "x"}
        }
        assert load_state_file(cfg_path, "org-b") == {
            "team-b": {"org_id": "org-2", "cached_org_name": "y"}
        }

    def test_legacy_string_value_format_is_normalized(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("github_org: my-org\n")
        # Hand-write the consolidated file with the legacy string-valued format
        state_path = tmp_path / "sync-state.json"
        state_path.write_text(
            json.dumps({"my-org": {"team_org_map": {"team-a": "org-a"}}})
        )
        loaded = load_state_file(cfg_path, "my-org")
        assert loaded == {"team-a": {"org_id": "org-a", "cached_org_name": ""}}

    def test_legacy_per_org_state_file_is_loaded(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("github_org: my-org\n")
        # Old per-org file naming
        legacy = tmp_path / "sync-state-my-org.json"
        legacy.write_text(
            json.dumps(
                {
                    "team_org_map": {
                        "team-a": {"org_id": "org-a", "cached_org_name": "x"},
                    }
                }
            )
        )
        loaded = load_state_file(cfg_path, "my-org")
        assert loaded == {"team-a": {"org_id": "org-a", "cached_org_name": "x"}}
