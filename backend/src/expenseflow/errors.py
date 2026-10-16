"""Domain errors: business-rule failures that carry the HTTP status they map to.

The repository and models raise these without knowing about HTTP; the API layer registers one exception
handler for :class:`DomainError` and renders ``{"statusCode": e.http_status, "message": e.message}``.
Anything else that escapes is a bug and becomes a 500.
"""

from __future__ import annotations

from typing import ClassVar


class DomainError(Exception):
    """Base class; ``http_status`` is the response status the API returns for it."""

    http_status: ClassVar[int] = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DomainValidationError(DomainError):
    """The request is well-formed but breaks a business rule (e.g. submitting without an amount)."""

    http_status = 400


class UnauthorizedError(DomainError):
    """No usable identity (e.g. the JWT has no ``sub`` claim)."""

    http_status = 401


class ForbiddenError(DomainError):
    """The caller is known but not allowed to do this (e.g. editing someone else's expense)."""

    http_status = 403


class NotFoundError(DomainError):
    http_status = 404


class ConflictError(DomainError):
    """The resource is in the wrong state or was changed concurrently (optimistic locking)."""

    http_status = 409
