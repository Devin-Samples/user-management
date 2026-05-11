"""user-management — unified Devin enterprise user/org management.

Two modules under one CLI:

- :mod:`user_management.bulk` — CSV/XLSX is the source of truth.
- :mod:`user_management.github_sync` — GitHub Teams are the source of truth.

Both share :mod:`user_management.core` for talking to the Devin v3 API.
"""

__version__ = "0.1.0"
