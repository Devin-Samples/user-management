"""Exception hierarchy for the Devin v3 API client.

Both modules import these directly from :mod:`user_management.core` (or this
module).  The exceptions wrap typed HTTP error codes so callers don't need to
inspect status codes themselves.
"""

from __future__ import annotations


class APIError(Exception):
    """Base exception for Devin API errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: str = ""):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)


class AuthError(APIError):
    """401 Unauthorized — bad or missing API key."""


class PermissionError(APIError):
    """403 Forbidden — key lacks required permission."""


class NotFoundError(APIError):
    """404 Not Found."""


class ValidationError(APIError):
    """422 Unprocessable Entity — invalid request body."""


class RateLimitError(APIError):
    """429 Too Many Requests — rate limit exhausted after retries."""
