"""WebSocket ``$connect`` authorizer with real (moto-issued) Cognito tokens (SPEC 6)."""

from __future__ import annotations

from typing import Any

import boto3
import pytest

from handlers import ws_authorizer
from tests.builders import ws_authorizer_event, ws_method_arn
from tests.conftest import CognitoUser, FakeLambdaContext, UserPool


def authorize(token: str | None, lambda_context: FakeLambdaContext) -> dict[str, Any]:
    return dict(ws_authorizer.handler(ws_authorizer_event(token), lambda_context))


def assert_unauthorized(token: str | None, lambda_context: FakeLambdaContext) -> None:
    with pytest.raises(Exception, match=r"^Unauthorized$") as raised:
        authorize(token, lambda_context)
    assert raised.type is Exception  # API Gateway maps exactly this to 401


def sign_in(user_pool: UserPool, user: CognitoUser, client_id: str) -> dict[str, str]:
    """Tokens for ``user`` from another app client (the conftest password is the default one)."""
    result: dict[str, str] = user_pool.idp.admin_initiate_auth(
        UserPoolId=user_pool.pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": user.email, "PASSWORD": "Correct-Horse-Battery-9"},
    )["AuthenticationResult"]
    return result


def test_access_token_allows_connect_with_the_identity_as_context(
    user_pool: UserPool, lambda_context: FakeLambdaContext
) -> None:
    manager = user_pool.create_user("kenji@example.com", groups=("employees", "managers"))

    response = authorize(manager.access_token, lambda_context)

    assert response["principalId"] == manager.sub
    assert response["policyDocument"] == {
        "Version": "2012-10-17",
        "Statement": [{"Action": "execute-api:Invoke", "Effect": "Allow", "Resource": [ws_method_arn("$connect")]}],
    }
    context = response["context"]
    assert (context["userId"], context["email"]) == (manager.sub, "kenji@example.com")
    assert sorted(context["groups"].split(",")) == ["employees", "managers"]


def test_user_without_groups_gets_an_empty_group_list(user_pool: UserPool, lambda_context: FakeLambdaContext) -> None:
    newcomer = user_pool.create_user("taro@example.com", groups=())

    assert authorize(newcomer.access_token, lambda_context)["context"]["groups"] == ""


@pytest.mark.parametrize("token", [None, "", "not-a-jwt", "a.b.c"])
def test_missing_or_garbage_tokens_are_rejected(
    token: str | None, user_pool: UserPool, lambda_context: FakeLambdaContext
) -> None:
    assert_unauthorized(token, lambda_context)


def test_id_token_is_rejected(user_pool: UserPool, lambda_context: FakeLambdaContext) -> None:
    user = user_pool.create_user("hanako@example.com")

    assert_unauthorized(user.id_token, lambda_context)


def test_revoked_token_is_rejected(user_pool: UserPool, lambda_context: FakeLambdaContext) -> None:
    user = user_pool.create_user("hanako@example.com")
    user_pool.idp.global_sign_out(AccessToken=user.access_token)

    assert_unauthorized(user.access_token, lambda_context)


def test_token_of_another_app_client_is_rejected(user_pool: UserPool, lambda_context: FakeLambdaContext) -> None:
    user = user_pool.create_user("hanako@example.com")
    other_client = user_pool.idp.create_user_pool_client(
        UserPoolId=user_pool.pool_id, ClientName="other-app", ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH"]
    )["UserPoolClient"]["ClientId"]
    tokens = sign_in(user_pool, user, other_client)

    assert boto3.client("cognito-idp").get_user(AccessToken=tokens["AccessToken"])  # genuine, just not ours
    assert_unauthorized(tokens["AccessToken"], lambda_context)


def test_token_of_another_user_pool_is_rejected(
    user_pool: UserPool, lambda_context: FakeLambdaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GetUser accepts any pool's token: an attacker's own pool must not open our WebSocket."""
    attacker = user_pool.create_user("mallory@example.com")
    other_pool = boto3.client("cognito-idp").create_user_pool(PoolName="attacker")["UserPool"]["Id"]
    monkeypatch.setenv("USER_POOL_ID", other_pool)  # our function now trusts a different pool than the token's

    assert_unauthorized(attacker.access_token, lambda_context)
