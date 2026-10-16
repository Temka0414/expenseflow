"""WebSocket route integration for ``$connect``, ``$disconnect`` and ``$default`` (SPEC 6).

A WebSocket API routes each frame by ``requestContext.routeKey``; one function handles all three routes here
(Powertools has no WebSocket resolver, and three branches do not need one).

* ``$connect``: the authorizer already accepted the token; its ``context`` arrives at ``requestContext.authorizer``.
  We store the connection under each audience it may receive (``connections.register``). Returning a non-2xx
  status here would refuse the connection.
* ``$disconnect``: best effort - API Gateway may not deliver it (the TTL and the 410 clean-up cover that).
* ``$default``: any other frame. The SPA sends ``{"action": "ping"}`` every few minutes because API Gateway closes
  connections that stay idle for 10 minutes; receiving the frame is all a keep-alive needs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.data_classes import APIGatewayWebSocketEvent, event_source
from aws_lambda_powertools.utilities.typing import LambdaContext

from expenseflow import connections
from expenseflow.auth import parse_groups

logger = Logger()
tracer = Tracer()


def connect(connection_id: str, authorizer: Mapping[str, Any]) -> list[str]:
    """Register a new connection for its user's audiences; returns the audiences."""
    user_id = authorizer.get("userId")
    if not user_id:
        # Only possible if the route lost its authorizer - fail closed instead of storing an anonymous connection.
        raise RuntimeError("$connect without authorizer context; is the Lambda authorizer attached to the route?")
    audiences = connections.audiences_for(user_id, parse_groups(authorizer.get("groups")))
    connections.register(connection_id, user_id, audiences)
    return audiences


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST, log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@event_source(data_class=APIGatewayWebSocketEvent)
def handler(event: APIGatewayWebSocketEvent, context: LambdaContext) -> dict[str, int]:
    request = event.request_context
    route, connection_id = request.route_key, request.connection_id
    logger.append_keys(connection_id=connection_id, route=route)
    if route == "$connect":
        audiences = connect(connection_id, event["requestContext"].get("authorizer") or {})
        logger.info("connected", extra={"audiences": audiences})
    elif route == "$disconnect":
        deleted = connections.unregister(connection_id)
        logger.info("disconnected", extra={"items_deleted": deleted})
    return {"statusCode": 200}
