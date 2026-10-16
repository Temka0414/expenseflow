"""ExpenseHttpApi: the REST-style JSON API of the SPA (API Gateway HTTP API + Cognito JWT authorizer).

Why an *HTTP API* rather than a *REST API*? HTTP APIs cost about 70% less, add less latency and
validate Cognito JWTs natively. REST APIs add features ExpenseFlow does not need (usage plans, API
keys, request validation, WAF, caching).

One Lambda function (a Powertools "Lambdalith") serves every route; Powertools' router dispatches
inside the function. The routes are still declared one by one in API Gateway instead of a catch-all
`$default`/`{proxy+}` route because:

* unknown paths are rejected by API Gateway (404) without invoking or paying for Lambda;
* CORS preflight (`OPTIONS`) requests carry no token. With explicit routes and no OPTIONS route,
  API Gateway answers them itself from the CORS configuration; a catch-all route would send them
  to the JWT authorizer, which rejects them, and the browser would block every call.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final

from aws_cdk import Duration
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from expenseflow_infra.config import PROJECT_NAME, StageConfig

GET: Final = apigwv2.HttpMethod.GET
POST: Final = apigwv2.HttpMethod.POST
PATCH: Final = apigwv2.HttpMethod.PATCH
DELETE: Final = apigwv2.HttpMethod.DELETE

ROUTES: Final[dict[str, tuple[apigwv2.HttpMethod, ...]]] = {
    "/me": (GET,),
    "/expenses": (GET, POST),
    "/expenses/{id}": (GET, PATCH, DELETE),
    "/expenses/{id}/receipt-upload": (POST,),
    "/expenses/{id}/receipt": (GET,),
    "/expenses/{id}/submit": (POST,),
    "/expenses/{id}/withdraw": (POST,),
    "/approvals": (GET,),
    "/approvals/{id}/decision": (POST,),
    "/stats": (GET,),
    "/reports": (GET, POST),
    "/reports/{month}": (GET,),
}
"""Every route of the API contract (path -> methods), all handled by the API function."""

THROTTLE_BURST_LIMIT: Final = 50
THROTTLE_RATE_LIMIT: Final = 100
"""Requests per second for the whole stage, far below the account limit (10,000 rps) that every API
in the Region shares: a runaway client can then never starve other APIs or run up the bill."""

_ACCESS_LOG_FORMAT: Final = json.dumps(
    {
        "requestId": "$context.requestId",
        "time": "$context.requestTime",
        "ip": "$context.identity.sourceIp",
        "method": "$context.httpMethod",
        "routeKey": "$context.routeKey",
        "path": "$context.path",
        "status": "$context.status",
        "responseLength": "$context.responseLength",
        "latencyMs": "$context.responseLatency",
        "integrationLatencyMs": "$context.integrationLatency",
        "integrationError": "$context.integrationErrorMessage",
        "authorizerError": "$context.authorizer.error",
        "userId": "$context.authorizer.claims.sub",
    }
)
"""One JSON object per request, queryable with CloudWatch Logs Insights (`filter status >= 500`)."""


class ExpenseHttpApi(Construct):
    """HTTP API with a Cognito JWT authorizer on every route, CORS, throttling and access logs.

    Attributes:
        api: The HTTP API.
        url: Base URL of the API without a trailing slash (`https://{id}.execute-api.{region}.amazonaws.com`).
        access_logs: Log group receiving one JSON line per request.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        handler: lambda_.IFunction,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        allowed_origins: Sequence[str],
    ) -> None:
        """Create the API.

        Args:
            config: Stage settings (log retention, names).
            handler: The API Lambda function serving every route.
            user_pool: Issuer of the JWTs the authorizer accepts.
            user_pool_client: The SPA's app client; tokens must be issued to it (JWT audience).
            allowed_origins: Browser origins allowed by CORS (the site URL and the Vite dev server).
        """
        super().__init__(scope, construct_id)
        # API Gateway validates the token's signature (against the pool's public keys), expiry,
        # issuer and audience before Lambda runs: invalid tokens never cost a Lambda invocation.
        # The authorizer does not check `token_use`, so the SPA can send the ID token, which
        # carries the `email`, `name` and `cognito:groups` claims the API needs.
        authorizer = authorizers.HttpUserPoolAuthorizer(
            "CognitoAuthorizer", user_pool, user_pool_clients=[user_pool_client]
        )
        self.api = apigwv2.HttpApi(
            self,
            "Api",
            api_name=f"{PROJECT_NAME}-{config.stage}-api",
            description=f"{PROJECT_NAME} ({config.stage}) JSON API for the SPA",
            # CORS lives here only: API Gateway answers preflight requests itself and adds the
            # headers to every response (CORS headers returned by the integration are ignored).
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=list(allowed_origins),
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PATCH,
                    apigwv2.CorsHttpMethod.DELETE,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["authorization", "content-type"],
                # Lets the SPA read the request id and quote it in error reports.
                expose_headers=["apigw-requestid"],
                max_age=Duration.days(1),
            ),
            default_authorizer=authorizer,
        )
        # One permission for the whole API instead of one per route: each route-scoped statement
        # grows the function's resource policy, which is limited to 20 KB.
        integration = integrations.HttpLambdaIntegration(
            "ApiIntegration", handler, scope_permission_to_route=False
        )
        for path, methods in ROUTES.items():
            self.api.add_routes(path=path, methods=list(methods), integration=integration)

        self.access_logs = logs.LogGroup(
            self, "AccessLogs", retention=config.log_retention, removal_policy=config.removal_policy
        )
        self._configure_default_stage()
        self.url = self.api.api_endpoint

    def _configure_default_stage(self) -> None:
        """Throttling and access logs on the `$default` stage, through the L1 escape hatch.

        The L2 `HttpApi` creates the auto-deployed `$default` stage but exposes neither setting.
        `node.default_child` is the generated L1 `CfnStage`, whose properties map 1:1 to
        CloudFormation, so they can be set directly. (Unlike REST and WebSocket APIs, HTTP APIs
        deliver access logs without an account-level CloudWatch role.)
        """
        stage = self.api.default_stage
        cfn_stage = stage.node.default_child if stage is not None else None
        if not isinstance(cfn_stage, apigwv2.CfnStage):
            raise TypeError(f"expected the L1 CfnStage of the default stage, got {cfn_stage!r}")
        cfn_stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=self.access_logs.log_group_arn, format=_ACCESS_LOG_FORMAT
        )
        cfn_stage.default_route_settings = apigwv2.CfnStage.RouteSettingsProperty(
            throttling_burst_limit=THROTTLE_BURST_LIMIT, throttling_rate_limit=THROTTLE_RATE_LIMIT
        )
