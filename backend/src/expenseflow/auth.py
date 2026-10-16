"""The authenticated caller, built from the Cognito JWT claims API Gateway has already verified.

The HTTP API's JWT authorizer validates the token (signature via the pool's JWKS, issuer, audience, expiry)
*before* Lambda runs and hands the claims over in ``requestContext.authorizer.jwt.claims``. The function
therefore trusts the claims and never re-validates the token - it only interprets them.

Gotcha: API Gateway flattens every claim to a string. An array claim like ``cognito:groups`` arrives as
``"[managers employees]"`` (brackets, space separated, no quotes), not as a JSON list. :func:`parse_groups`
accepts that and every other shape we may meet (real list, JSON string, comma list, missing).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from expenseflow import settings
from expenseflow.errors import ForbiddenError, UnauthorizedError

_GROUP_SEPARATORS: Final = re.compile(r"[\s,]+")


def parse_groups(raw: object) -> list[str]:
    """Normalise a ``cognito:groups`` claim to a de-duplicated list of group names (order preserved).

    >>> parse_groups("[managers employees]")
    ['managers', 'employees']
    >>> parse_groups('["admins","employees"]'), parse_groups("a, b"), parse_groups(None)
    (['admins', 'employees'], ['a', 'b'], [])
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return _unique(_split_group_string(raw))
    if isinstance(raw, (list, tuple, set, frozenset)):
        return _unique(str(item) for item in raw)
    return []


def _split_group_string(text: str) -> Iterable[str]:
    text = text.strip()
    if text.startswith("[") and '"' in text:  # a JSON array serialised as a string
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, list):
            return (str(item) for item in decoded)
    return _GROUP_SEPARATORS.split(text.removeprefix("[").removesuffix("]"))


def _unique(names: Iterable[str]) -> list[str]:
    stripped = (name.strip().strip('"') for name in names)
    return list(dict.fromkeys(name for name in stripped if name))


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is calling, and what their groups allow."""

    user_id: str  # Cognito `sub`: stable and unique, unlike e-mail addresses which users can change
    email: str
    name: str
    groups: tuple[str, ...] = ()

    @property
    def is_manager(self) -> bool:
        return settings.GROUP_MANAGERS in self.groups

    @property
    def is_admin(self) -> bool:
        return settings.GROUP_ADMINS in self.groups

    @property
    def can_approve(self) -> bool:
        """Managers and admins work the approval queue (and may read anyone's expenses)."""
        return self.is_manager or self.is_admin

    def to_api(self) -> dict[str, Any]:
        """``GET /me`` response body."""
        return {
            "userId": self.user_id,
            "email": self.email,
            "name": self.name,
            "groups": list(self.groups),
            "isManager": self.is_manager,
            "isAdmin": self.is_admin,
        }


def caller_from_claims(claims: Mapping[str, Any] | None) -> Caller:
    """Build the :class:`Caller` from JWT claims; :class:`UnauthorizedError` when there is no ``sub``.

    Cognito ID tokens carry ``email`` and (when the attribute is set) ``name``; we fall back to the e-mail
    address, then the username, so the UI always has something to display.
    """
    claims = claims or {}
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        raise UnauthorizedError("token has no subject (sub) claim")
    email = str(claims.get("email") or "").strip()
    name = str(claims.get("name") or "").strip() or email or str(claims.get("cognito:username") or user_id)
    return Caller(
        user_id=user_id,
        email=email,
        name=name,
        groups=tuple(parse_groups(claims.get("cognito:groups"))),
    )


def ensure_can_view(caller: Caller, owner_id: str) -> None:
    """Owners see their own expenses; managers and admins see everyone's. Raises :class:`ForbiddenError`."""
    if caller.user_id != owner_id and not caller.can_approve:
        raise ForbiddenError("you can only view your own expenses")
