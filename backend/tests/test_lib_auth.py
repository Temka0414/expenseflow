from __future__ import annotations

from typing import Any

import pytest

from expenseflow.auth import Caller, caller_from_claims, ensure_can_view, parse_groups
from expenseflow.errors import ForbiddenError, UnauthorizedError
from tests.builders import flatten_claims, id_token_claims


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[managers employees]", ["managers", "employees"]),  # what the HTTP API JWT authorizer really sends
        ("[employees]", ["employees"]),
        ("[]", []),
        ('["admins","employees"]', ["admins", "employees"]),  # JSON array as a string
        ("admins, employees", ["admins", "employees"]),
        ("admins", ["admins"]),
        (["managers", "employees"], ["managers", "employees"]),
        (("admins",), ["admins"]),
        ("[managers managers employees]", ["managers", "employees"]),
        ("", []),
        ("   ", []),
        (None, []),
        (42, []),
    ],
)
def test_parse_groups(raw: object, expected: list[str]) -> None:
    assert parse_groups(raw) == expected


def test_parse_groups_handles_the_flattened_claim_of_the_builder() -> None:
    claims = flatten_claims(id_token_claims(groups=("managers", "employees")))

    assert claims["cognito:groups"] == "[managers employees]"
    assert parse_groups(claims["cognito:groups"]) == ["managers", "employees"]


def test_caller_from_flattened_claims() -> None:
    claims = flatten_claims(id_token_claims("u-1", email="kenji@example.com", name="Kenji", groups=("managers",)))

    caller = caller_from_claims(claims)

    assert caller == Caller(user_id="u-1", email="kenji@example.com", name="Kenji", groups=("managers",))
    assert caller.is_manager
    assert not caller.is_admin
    assert caller.can_approve


@pytest.mark.parametrize(
    ("groups", "manager", "admin", "approve"),
    [
        ((), False, False, False),
        (("employees",), False, False, False),
        (("managers", "employees"), True, False, True),
        (("admins",), False, True, True),
    ],
)
def test_caller_roles(groups: tuple[str, ...], manager: bool, admin: bool, approve: bool) -> None:
    caller = Caller(user_id="u", email="u@example.com", name="U", groups=groups)

    assert (caller.is_manager, caller.is_admin, caller.can_approve) == (manager, admin, approve)


@pytest.mark.parametrize(
    ("claims", "expected_name"),
    [
        ({"sub": "u-1", "email": "a@example.com", "name": "Aiko"}, "Aiko"),
        ({"sub": "u-1", "email": "a@example.com"}, "a@example.com"),
        ({"sub": "u-1", "cognito:username": "aiko"}, "aiko"),
        ({"sub": "u-1"}, "u-1"),
    ],
)
def test_caller_name_falls_back(claims: dict[str, Any], expected_name: str) -> None:
    assert caller_from_claims(claims).name == expected_name


def test_caller_without_groups_claim() -> None:
    caller = caller_from_claims(flatten_claims(id_token_claims("u-1", groups=())))

    assert caller.groups == ()


@pytest.mark.parametrize("claims", [None, {}, {"sub": ""}, {"sub": "   "}, {"email": "a@example.com"}])
def test_caller_requires_a_subject(claims: dict[str, Any] | None) -> None:
    with pytest.raises(UnauthorizedError) as raised:
        caller_from_claims(claims)

    assert raised.value.http_status == 401


def test_caller_to_api_is_the_me_response() -> None:
    caller = Caller(user_id="u-1", email="a@example.com", name="Aiko", groups=("admins", "employees"))

    assert caller.to_api() == {
        "userId": "u-1",
        "email": "a@example.com",
        "name": "Aiko",
        "groups": ["admins", "employees"],
        "isManager": False,
        "isAdmin": True,
    }


def test_ensure_can_view() -> None:
    owner = Caller(user_id="owner", email="o@example.com", name="O", groups=("employees",))
    other = Caller(user_id="other", email="x@example.com", name="X", groups=("employees",))
    manager = Caller(user_id="boss", email="b@example.com", name="B", groups=("managers",))

    ensure_can_view(owner, "owner")
    ensure_can_view(manager, "owner")
    with pytest.raises(ForbiddenError):
        ensure_can_view(other, "owner")
