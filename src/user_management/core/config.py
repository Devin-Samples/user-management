"""Env-based configuration helpers.

The Devin API key is read from ``DEVIN_API_KEY`` and supports both ``cog_``
(enterprise) and ``sk-`` (cloud) prefixes.  The base URL defaults to the
cloud endpoint and can be overridden via ``DEVIN_API_BASE_URL``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_DEVIN_BASE_URL = "https://api.devin.ai"


def load_env(path: str | Path | None = None) -> None:
    """Load environment variables from a ``.env`` file if present.

    ``path`` defaults to ``./.env`` relative to the current working directory.
    """
    if path is None:
        env_path = Path(".env")
    else:
        env_path = Path(path)
    if env_path.exists():
        load_dotenv(env_path)


def get_devin_api_key(*, required: bool = True) -> str:
    """Return ``DEVIN_API_KEY`` from the environment.

    Accepts both ``cog_...`` (enterprise) and ``sk-...`` (cloud) keys.
    If ``required`` is True (the default) and the key is missing or still
    set to a template placeholder, prints an error and exits.
    """
    key = os.environ.get("DEVIN_API_KEY", "").strip()
    if required and (not key or key in {"cog_your_key_here", "sk-your_key_here"}):
        print(
            "ERROR: DEVIN_API_KEY is not set. Copy .env.example to .env and "
            "fill in your service-user key (cog_... or sk-...).",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def get_devin_api_base_url() -> str:
    """Return ``DEVIN_API_BASE_URL`` from the environment, defaulting to cloud."""
    return os.environ.get("DEVIN_API_BASE_URL", DEFAULT_DEVIN_BASE_URL).rstrip("/")
