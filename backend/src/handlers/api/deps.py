"""Shared route building blocks: who is calling (``Depends`` providers), common parameter types and constants.

Authentication already happened in API Gateway: the HTTP API's JWT authorizer rejected requests without a valid
Cognito ID token before Lambda was invoked, and forwarded the token's claims. These providers only *interpret*
the claims (:mod:`expenseflow.auth`) and enforce group-based authorization.

A route declares what it needs with an annotated parameter, and Powertools resolves it before the route runs::

    @router.get("/approvals")
    def list_approvals(caller: Approver) -> dict[str, Any]: ...

Gotcha: an exception raised inside a dependency reaches the exception handlers wrapped in
``DependencyResolutionError``; ``app.py`` unwraps it so a 403 stays a 403 instead of becoming a 500.
"""

from __future__ import annotations

from typing import Annotated, Final, cast

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import Depends
from aws_lambda_powertools.event_handler.openapi.params import Path, Query
from aws_lambda_powertools.event_handler.request import Request  # runtime import: Powertools injects by type hint
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEventV2

from expenseflow import ids
from expenseflow.auth import Caller, caller_from_claims
from expenseflow.errors import ForbiddenError
from expenseflow.repository import MAX_PAGE_SIZE

# A child logger shares the handler (and so the appended keys) of the function's root Logger created in app.py.
logger = Logger(child=True)

# Lifetime of presigned S3 URLs and POST policies: long enough to start an upload/download, short enough that a
# leaked URL (browser history, a screenshot) is soon useless. Signing happens locally - it costs no AWS call.
PRESIGNED_URL_TTL_SECONDS: Final = 300


def current_caller(request: Request) -> Caller:
    """The authenticated caller, from the JWT claims API Gateway forwarded in ``requestContext.authorizer``.

    Every later log line of this invocation carries ``user_id``; ``inject_lambda_context(clear_state=True)``
    removes it again before the next invocation, so a warm container never logs the previous caller.
    """
    event = cast("APIGatewayProxyEventV2", request.resolved_event)
    caller = caller_from_claims(event.request_context.authorizer.jwt_claim)
    logger.append_keys(user_id=caller.user_id)
    return caller


CurrentCaller = Annotated[Caller, Depends(current_caller)]


def require_approver(caller: CurrentCaller) -> Caller:
    """Managers and admins only (the approval queue and decisions)."""
    if not caller.can_approve:
        raise ForbiddenError("only managers and admins can do this")
    return caller


def require_admin(caller: CurrentCaller) -> Caller:
    """Admins only (monthly reports)."""
    if not caller.is_admin:
        raise ForbiddenError("only admins can do this")
    return caller


Approver = Annotated[Caller, Depends(require_approver)]
Admin = Annotated[Caller, Depends(require_admin)]

# Path parameters reach Lambda exactly as sent (not URL-decoded). Accepting only ULIDs means a malformed id is a 422
# before any DynamoDB key is built from it.
ExpenseIdPath = Annotated[str, Path(pattern=ids.ID_PATTERN)]
# Page size, and the opaque `nextCursor` of the previous page (the repository rejects a tampered cursor with 400).
PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
PageCursor = Annotated[str | None, Query()]
