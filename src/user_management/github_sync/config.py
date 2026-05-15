"""Load and validate configuration from config.yaml and environment variables."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from user_management.github_sync.models import SyncConfig


def load_env() -> None:
    """Load environment variables from .env file if present."""
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path)


def get_github_token(env_var_name: str = "GITHUB_TOKEN") -> str:
    """Return the GitHub personal access token from the environment.

    Args:
        env_var_name: Name of the environment variable to read (default: GITHUB_TOKEN)
    """
    token = os.environ.get(env_var_name, "")
    if not token:
        print(f"Error: {env_var_name} environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return token


def get_devin_api_token() -> str:
    """Return the Devin API service-user token from the environment."""
    token = os.environ.get("DEVIN_API_TOKEN", "") or os.environ.get("DEVIN_API_KEY", "")
    if not token:
        print("Error: DEVIN_API_TOKEN (or DEVIN_API_KEY) environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return token


def get_devin_api_base_url() -> str:
    """Return the Devin API base URL (default: https://api.devin.ai)."""
    return os.environ.get("DEVIN_API_BASE_URL", "https://api.devin.ai")


def load_config(path: str = "config.yaml") -> SyncConfig:
    """Parse and validate the YAML configuration file.

    Args:
        path: Filesystem path to the config YAML file.

    Returns:
        A validated ``SyncConfig`` instance.
    """
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
        config = SyncConfig(**raw)
    except Exception as exc:
        print(f"Error: Invalid configuration: {exc}", file=sys.stderr)
        sys.exit(1)

    return config
