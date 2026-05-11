"""Shared fixtures and configuration for tests."""
import os

import pytest
from dotenv import load_dotenv


@pytest.fixture(autouse=True, scope="session")
def load_env():
    """Load .env file for all tests."""
    load_dotenv()


@pytest.fixture
def api_key():
    """Return the API key from environment, skip if not set."""
    key = os.getenv("DEVIN_API_KEY")
    if not key or key == "cog_your_key_here":
        pytest.skip("DEVIN_API_KEY not set")
    return key


@pytest.fixture
def base_url():
    """Return the API base URL from environment."""
    return os.getenv("DEVIN_API_BASE_URL", "https://test-api.devinenterprise.com")
