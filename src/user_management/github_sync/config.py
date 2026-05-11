"""YAML config + state file persistence for the github_sync module.

The pydantic models live in :mod:`user_management.core.models`; this
module just wraps YAML I/O around them. State persistence allows the
sync to track ``team_slug -> org_id`` mappings across runs so renamed
GitHub teams don't lose their existing Devin org.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import yaml

from user_management.core.models import SyncConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config(path: str | Path = "config.yaml") -> SyncConfig:
    """Parse and validate the YAML configuration file."""
    config_path = Path(path)
    if not config_path.exists():
        print(f"Error: Configuration file not found: {path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        print(f"Error: Configuration file is empty: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        return SyncConfig(**raw)
    except Exception as exc:
        print(f"Error: Invalid configuration: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------
def _state_file_path(config_path: str | Path) -> Path:
    """Derive the state file path from the config file location."""
    config_dir = Path(config_path).resolve().parent
    return config_dir / "sync-state.json"


def _normalize_org_mapping(raw_mapping: dict) -> dict[str, dict[str, str]]:
    """Normalize a raw team_org_map into the current format.

    Handles backward compatibility with the old format where values were
    plain ``org_id`` strings instead of objects.
    """
    mapping: dict[str, dict[str, str]] = {}
    for slug, value in raw_mapping.items():
        if isinstance(value, str):
            mapping[slug] = {"org_id": value, "cached_org_name": ""}
        elif isinstance(value, dict) and "org_id" in value:
            mapping[slug] = value
        else:
            logger.warning("Skipping malformed state entry for team '%s'", slug)
    return mapping


def load_state_file(
    config_path: str | Path, github_org: str
) -> dict[str, dict[str, str]]:
    """Load the ``team_slug -> {org_id, cached_org_name}`` map for one GitHub org.

    The state file is a single JSON file keyed by GitHub org name.  Also
    handles the legacy per-org ``sync-state-{github_org}.json`` for
    backward compatibility.
    """
    path = _state_file_path(config_path)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            raw_mapping = data.get(github_org, {}).get("team_org_map", {})
            if raw_mapping:
                mapping = _normalize_org_mapping(raw_mapping)
                logger.info(
                    "Loaded state file with %d team->org mappings for '%s' from %s",
                    len(mapping),
                    github_org,
                    path,
                )
                return mapping
        except Exception as exc:
            logger.warning("Failed to read state file %s: %s", path, exc)

    legacy_path = (
        Path(config_path).resolve().parent / f"sync-state-{github_org}.json"
    )
    if legacy_path.exists():
        try:
            data = json.loads(legacy_path.read_text())
            raw_mapping = data.get("team_org_map", {})
            mapping = _normalize_org_mapping(raw_mapping)
            logger.info(
                "Loaded legacy state file with %d team->org mappings from %s",
                len(mapping),
                legacy_path,
            )
            return mapping
        except Exception as exc:
            logger.warning(
                "Failed to read legacy state file %s: %s", legacy_path, exc
            )

    logger.debug("No state file found for org '%s'", github_org)
    return {}


def save_state_file(
    config_path: str | Path,
    github_org: str,
    team_org_map: dict[str, dict[str, str]],
    dry_run: bool = False,
) -> None:
    """Persist the ``team_slug -> {org_id, cached_org_name}`` map.

    Updates only the entry for the given ``github_org`` within the
    consolidated ``sync-state.json``.  Other orgs in the file are
    preserved.  Skipped in dry-run mode since no orgs are actually created.
    """
    if dry_run:
        logger.info(
            "[DRY RUN] Would save state file with %d mappings for '%s'",
            len(team_org_map),
            github_org,
        )
        return
    path = _state_file_path(config_path)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            pass
    existing[github_org] = {"team_org_map": team_org_map}
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    logger.info(
        "Saved state file with %d mappings for '%s' to %s",
        len(team_org_map),
        github_org,
        path,
    )
