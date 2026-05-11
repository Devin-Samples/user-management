"""Individual diagnostic checks.

Each check returns ``True`` on success, ``False`` on failure.  They are
designed to be runnable independently from the CLI or stitched together
via :func:`run_all`.
"""

from __future__ import annotations

from user_management.core.client import DevinAPIClient
from user_management.core.config import (
    get_devin_api_base_url,
    get_devin_api_key,
)


def check_devin_auth() -> bool:
    """Verify the Devin API key and base URL are usable."""
    print("== Devin auth ==")
    api_key = get_devin_api_key(required=False)
    base_url = get_devin_api_base_url()
    if not api_key:
        print("  X DEVIN_API_KEY is not set")
        return False
    print(f"  base URL: {base_url}")
    client = DevinAPIClient(api_key=api_key, base_url=base_url)
    try:
        creds = client.verify_credentials()
    except Exception as exc:
        print(f"  X GET /v3/self failed: {exc}")
        return False
    print(
        f"  OK service_user={creds.get('service_user_name')} "
        f"(id={creds.get('service_user_id')})"
    )
    return True


def run_all() -> bool:
    """Run all checks. Returns True iff every check passes."""
    results = [
        check_devin_auth(),
    ]
    return all(results)
