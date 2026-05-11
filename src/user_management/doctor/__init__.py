"""Diagnostic checks for the user-management environment.

Consolidates github-permissions-devin-sync's standalone diagnostic scripts
(check_email_visibility.py, check_github_app.py) into a single
``user-management doctor`` command.
"""

from user_management.doctor.checks import (
    check_devin_auth,
    check_email_visibility,
    check_github_app,
    check_github_token,
    run_all,
)

__all__ = [
    "check_devin_auth",
    "check_email_visibility",
    "check_github_app",
    "check_github_token",
    "run_all",
]
