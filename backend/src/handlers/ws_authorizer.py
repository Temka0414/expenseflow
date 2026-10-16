"""REQUEST Lambda authorizer for the WebSocket ``$connect`` route (SPEC 6).

Browsers cannot set an ``Authorization`` header on a WebSocket, so the SPA sends its Cognito **access token** in the
query string (``wss://.../live?token=...``) and API Gateway passes it here (identity source
``route.request.querystring.token``). Only ``$connect`` can have an authorizer; once the connection is accepted,
every later message rides on it.

How the token is verified - without a JWT library:

1. ``GetUser(AccessToken=token)``: Cognito itself checks the signature, the expiry and that the token was not
   revoked (sign-out). The call is authorised by the token, not by IAM - we send it **unsigned** and the function
   needs no Cognito permission at all.
2. GetUser accepts an access token of ANY user pool and ANY app client. Only after step 1 proved the token
   genuine can we trust its payload, and we check it was issued *for us*: ``token_use == "access"``,
   ``client_id`` == our app client, ``iss`` ends with our user pool id.

The response is an IAM policy allowing ``$connect`` plus a ``context`` map that API Gateway forwards to the
``$connect`` integration (``ws_connections``) at ``requestContext.authorizer``. Raising ``Exception("Unauthorized")``
(that exact message) makes API Gateway answer ``401``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.data_classes import event_source
from aws_lambda_powertools.utilities.data_classes.api_gateway_authorizer_event import (
    APIGatewayAuthorizerRequestEvent,
    APIGatewayAuthorizerResponseWebSocket,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import BotoCoreError, ClientError

from expenseflow import aws, settings
from expenseflow.auth import parse_groups

logger = Logger()
tracer = Tracer()


class TokenRejected(Exception):
    """The token is missing, invalid, or not an access token of this app; the message says why (logged only)."""


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: str
    email: str
    groups: list[str]


@tracer.capture_method(capture_response=False)
def authenticate(token: str | None) -> Identity:
    """Verify a Cognito access token (see the module docstring) and return who it belongs to."""
    if not token:
        raise TokenRejected("no token in the query string")
    try:
        user = aws.cognito_unsigned().get_user(AccessToken=token)
    except ClientError as error:  # NotAuthorizedException: bad signature, expired, revoked or malformed
        raise TokenRejected(f"Cognito rejected the token ({error.response['Error']['Code']})") from error
    except BotoCoreError as error:
        raise TokenRejected(f"Cognito could not be reached ({type(error).__name__})") from error

    claims = _payload(token)
    if claims.get("token_use") != "access":
        raise TokenRejected("not an access token")
    if claims.get("client_id") != settings.user_pool_client_id():
        raise TokenRejected("token was issued to another app client")
    if not str(claims.get("iss", "")).endswith(f"/{settings.user_pool_id()}"):
        raise TokenRejected("token was issued by another user pool")

    attributes = {attribute["Name"]: attribute["Value"] for attribute in user.get("UserAttributes", [])}
    return Identity(
        user_id=str(claims["sub"]),
        email=attributes.get("email", ""),  # access tokens carry no e-mail claim; GetUser returns the attributes
        groups=parse_groups(claims.get("cognito:groups")),
    )


def _payload(token: str) -> dict[str, Any]:
    """Decode (NOT verify) the JWT payload: the middle, base64url-encoded segment, whose padding JWTs strip."""
    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (IndexError, ValueError) as error:  # binascii.Error and UnicodeDecodeError are ValueErrors
        raise TokenRejected("token is not a JWT") from error
    if not isinstance(payload, dict) or "sub" not in payload:
        raise TokenRejected("token payload has no subject")
    return payload


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@event_source(data_class=APIGatewayAuthorizerRequestEvent)
def handler(event: APIGatewayAuthorizerRequestEvent, context: LambdaContext) -> dict[str, Any]:
    # Not `event.query_string_parameters`: that property raises KeyError when the URL has no query string.
    token = (event.get("queryStringParameters") or {}).get("token")
    try:
        identity = authenticate(token)
    except TokenRejected as rejection:
        logger.warning("WebSocket connection rejected", extra={"reason": str(rejection)})
        raise Exception("Unauthorized") from None  # the exact message API Gateway turns into 401

    logger.info("WebSocket connection authorized", extra={"user_id": identity.user_id, "groups": identity.groups})
    # Context values must be strings, numbers or booleans: the group list travels comma-joined.
    response = APIGatewayAuthorizerResponseWebSocket.from_route_arn(
        event.method_arn,
        principal_id=identity.user_id,
        context={"userId": identity.user_id, "email": identity.email, "groups": ",".join(identity.groups)},
    )
    response.allow_route("$connect")
    return response.asdict()
