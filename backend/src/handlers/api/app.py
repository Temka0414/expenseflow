"""HTTP API Lambda: one function ("Lambdalith") serves every route through a Powertools resolver.

API Gateway (HTTP API) owns the edge concerns: the JWT authorizer verifies the Cognito ID token, CORS preflight is
answered by API Gateway itself (integration CORS headers would be ignored, so the resolver gets no ``cors=``) and
throttling happens per stage. Each explicit route (``GET /expenses/{id}``, ...) forwards to this function, and the
resolver dispatches on method + path to the ``routes_*`` modules.

Why one function instead of one per route: a single warm container serves all routes (fewer cold starts), and
shared code, IAM permissions and configuration live in one place. The trade-off is one, broader IAM role.

Error responses (SPEC section 5):

* request validation (pydantic, ``enable_validation=True``) -> 422 ``{"statusCode", "detail": [{"loc", "type"}]}``
* domain errors (:mod:`expenseflow.errors`) -> 400/401/403/404/409 ``{"statusCode", "message"}``
* unknown route -> Powertools' built-in 404 ``{"statusCode": 404, "message": "Not found"}``
* anything else is a bug -> logged with its stack trace, 500 ``{"statusCode": 500, "message": ...}``
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import (
    APIGatewayHttpResolver,
    DependencyResolutionError,
    Response,
    content_types,
)
from aws_lambda_powertools.event_handler.exceptions import ServiceError
from aws_lambda_powertools.event_handler.openapi.exceptions import (
    RequestUnsupportedContentType,
    RequestValidationError,
)
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.typing import LambdaContext

from expenseflow.errors import DomainError
from expenseflow.serialization import dumps
from handlers.api import routes_approvals, routes_expenses, routes_me, routes_reports

logger = Logger()
tracer = Tracer()
metrics = Metrics()

# `serializer` writes every response body. `dumps` renders DynamoDB's Decimal as a JSON number (Powertools' default
# encoder would emit "12.50" as a string). Note: with validation on, a route's return annotation makes Powertools
# serialize through pydantic first, which also turns Decimal into a string - so routes return JSON-ready values
# (`models.to_api` & co. convert numbers) and this serializer is the safety net for everything else.
app = APIGatewayHttpResolver(enable_validation=True, serializer=dumps)
for routes in (routes_me, routes_expenses, routes_approvals, routes_reports):
    app.include_router(routes.router)


def _error_response(status: int, message: str) -> Response[dict[str, Any]]:
    """The same body shape Powertools uses for its built-in errors (``NotFoundError``, ``ServiceError``, ...)."""
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body={"statusCode": status, "message": message},
    )


@app.exception_handler(DomainError)
def handle_domain_error(error: DomainError) -> Response[dict[str, Any]]:
    """A business rule said no (wrong owner, wrong status, stale version, ...): an expected client error."""
    logger.info("Request rejected", extra={"status_code": error.http_status, "reason": error.message})
    return _error_response(error.http_status, error.message)


@app.exception_handler(DependencyResolutionError)
def handle_dependency_error(error: DependencyResolutionError) -> Response[dict[str, Any]]:
    """Powertools wraps whatever a ``Depends`` provider raises; unwrap it so a 403 stays a 403."""
    cause = error.__cause__
    if isinstance(cause, DomainError):
        return handle_domain_error(cause)
    if isinstance(cause, ServiceError):
        raise cause
    return handle_unexpected_error(error)


@app.exception_handler([RequestValidationError, RequestUnsupportedContentType])
def handle_invalid_request(error: RequestValidationError | RequestUnsupportedContentType) -> Response[dict[str, Any]]:
    """Render validation failures exactly like Powertools' built-in handler would.

    Needed because exception handlers are matched along the exception's MRO: without this one, the catch-all
    ``Exception`` handler below would swallow validation errors and answer 500 instead of 422/415. Only ``loc``
    and ``type`` are returned - pydantic's messages could echo input values and reveal implementation details.
    """
    status = (
        HTTPStatus.UNSUPPORTED_MEDIA_TYPE
        if isinstance(error, RequestUnsupportedContentType)
        else HTTPStatus.UNPROCESSABLE_ENTITY
    )
    detail = [{"loc": item["loc"], "type": item["type"]} for item in error.errors()]
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body={"statusCode": status, "detail": detail},
    )


@app.exception_handler(Exception)
def handle_unexpected_error(error: Exception) -> Response[dict[str, Any]]:
    """Catch-all: log the bug with its stack trace and answer a JSON 500 that reveals nothing internal.

    Powertools' own HTTP errors (e.g. the 404 for an unknown route) are ``ServiceError`` subclasses, and the MRO
    lookup sends them here too. Re-raising a ``ServiceError`` hands it back to Powertools, which renders it.
    Without this handler an unexpected exception would fail the invocation and API Gateway would answer a bare
    ``{"message": "Internal Server Error"}``.
    """
    if isinstance(error, ServiceError):
        raise error
    logger.exception("Unhandled error")
    return _error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error")


# Decorator order: the Logger is outermost so every log line (including the tracer's and metrics') carries the
# Lambda context and the correlation id (API Gateway's request id); `log_metrics` is innermost so metrics are
# flushed inside the traced segment. `clear_state=True` drops keys appended during the previous invocation (the
# caller's `user_id`) - module state survives between invocations of a warm container. Events are not logged:
# they contain the caller's JWT claims and request bodies. The tracer does not record responses, which can contain
# presigned URLs (bearer credentials until they expire).
@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_HTTP, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
