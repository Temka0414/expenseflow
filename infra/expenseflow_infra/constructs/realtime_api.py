"""RealtimeApi: the WebSocket API that pushes domain events to open browser tabs.

How it works:

* The SPA opens `wss://{id}.execute-api.{region}.amazonaws.com/live?token=<Cognito access token>`.
  Browsers cannot set an `Authorization` header on a WebSocket, hence the query string.
* `$connect` runs a Lambda REQUEST authorizer first (only `$connect` can have one: once the
  connection is open, every later message on it is already authenticated). It verifies the token
  with Cognito and returns an IAM policy plus context (`userId`, `email`, `groups`).
* `ws_connections` stores the connection under each audience it should receive (`USER#...`,
  `GROUP#managers`, ...) on `$connect` and deletes it on `$disconnect`; `$default` answers
  keep-alive pings (API Gateway closes connections after 10 idle minutes and after 2 hours).
* `realtime_push` sends events by POSTing to the stage's `@connections` management endpoint.
"""

from __future__ import annotations

import json
from typing import Final

from aws_cdk import RemovalPolicy
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from expenseflow_infra.config import PROJECT_NAME, StageConfig
from expenseflow_infra.nag import acknowledge

STAGE_NAME: Final = "live"
TOKEN_QUERY_PARAMETER: Final = "token"
THROTTLE_RATE_LIMIT: Final = 50
THROTTLE_BURST_LIMIT: Final = 100

_ACCESS_LOG_FORMAT: Final = json.dumps(
    {
        "requestId": "$context.requestId",
        "time": "$context.requestTime",
        "ip": "$context.identity.sourceIp",
        "connectionId": "$context.connectionId",
        "eventType": "$context.eventType",
        "routeKey": "$context.routeKey",
        "status": "$context.status",
        "integrationError": "$context.integrationErrorMessage",
        "authorizerError": "$context.authorizer.error",
        "userId": "$context.authorizer.principalId",
    }
)
_CLOUDWATCH_ROLE_POLICY: Final = "service-role/AmazonAPIGatewayPushToCloudWatchLogs"


class RealtimeApi(Construct):
    """WebSocket API with a Lambda authorizer on `$connect` and an auto-deployed `live` stage.

    Attributes:
        api: The WebSocket API.
        stage: The `live` stage; `stage.grant_management_api_access(fn)` lets a function push.
        url: `wss://` URL the SPA connects to.
        callback_url: `https://` endpoint of the `@connections` management API.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        authorizer_function: lambda_.IFunction,
        connections_function: lambda_.IFunction,
    ) -> None:
        """Create the API.

        Args:
            config: Stage settings (log retention, names).
            authorizer_function: `ws_authorizer`, validates the Cognito access token.
            connections_function: `ws_connections`, handles `$connect`, `$disconnect` and `$default`.
        """
        super().__init__(scope, construct_id)
        authorizer = authorizers.WebSocketLambdaAuthorizer(
            "Authorizer",
            authorizer_function,
            identity_source=[f"route.request.querystring.{TOKEN_QUERY_PARAMETER}"],
        )
        self.api = apigwv2.WebSocketApi(
            self,
            "Api",
            api_name=f"{PROJECT_NAME}-{config.stage}-realtime",
            description=f"{PROJECT_NAME} ({config.stage}) live updates for the SPA",
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration(
                    "ConnectIntegration", connections_function
                ),
                authorizer=authorizer,
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration(
                    "DisconnectIntegration", connections_function
                ),
            ),
            default_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration(
                    "DefaultIntegration", connections_function
                ),
            ),
        )
        self.stage = apigwv2.WebSocketStage(
            self,
            "Stage",
            web_socket_api=self.api,
            stage_name=STAGE_NAME,
            auto_deploy=True,
            throttle=apigwv2.ThrottleSettings(
                rate_limit=THROTTLE_RATE_LIMIT, burst_limit=THROTTLE_BURST_LIMIT
            ),
        )
        self.access_logs = logs.LogGroup(
            self, "AccessLogs", retention=config.log_retention, removal_policy=config.removal_policy
        )
        self._enable_access_logs()
        self.url = self.stage.url
        self.callback_url = self.stage.callback_url

        for route_key in ("$disconnect", "$default"):
            acknowledge(
                self.api.node.find_child(f"{route_key}-Route"),
                (
                    "AwsSolutions-APIG4",
                    "API Gateway only supports an authorizer on $connect; this route runs on a "
                    "connection that $connect already authenticated.",
                ),
            )

    def _enable_access_logs(self) -> None:
        """Access logs on the stage via the L1 escape hatch (`node.default_child` is the CfnStage).

        Gotcha: unlike HTTP APIs, WebSocket (and REST) APIs write logs with an IAM role that is
        configured once per account and Region in the API Gateway *account settings*. In a fresh
        account it is not set, and deploying a stage with logging fails with "CloudWatch Logs role
        ARN must be set in account settings to enable logging". `CfnAccount` sets it.
        """
        cfn_stage = self.stage.node.default_child
        if not isinstance(cfn_stage, apigwv2.CfnStage):
            raise TypeError(f"expected the L1 CfnStage of the WebSocket stage, got {cfn_stage!r}")
        cfn_stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=self.access_logs.log_group_arn, format=_ACCESS_LOG_FORMAT
        )
        # The stage can only enable logging once the account setting exists.
        cfn_stage.node.add_dependency(self._account_logging_role())

    def _account_logging_role(self) -> apigw.CfnAccount:
        role = iam.Role(
            self,
            "ApiGatewayCloudWatchRole",
            assumed_by=iam.ServicePrincipal("apigateway.amazonaws.com"),
            description="Lets API Gateway write REST/WebSocket API logs to CloudWatch Logs (account setting)",
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(_CLOUDWATCH_ROLE_POLICY)],
        )
        # The account setting outlives this stack and may serve other APIs in the account, so the
        # role it points to is kept when the stack is deleted (as CDK's own RestApi does).
        role.apply_removal_policy(RemovalPolicy.RETAIN)
        acknowledge(
            role,
            (
                f"AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/{_CLOUDWATCH_ROLE_POLICY}]",
                "The AWS managed policy for API Gateway's account-level logging role is the documented "
                "setup; API Gateway uses it for all REST/WebSocket API logs in this account and Region.",
            ),
        )
        return apigw.CfnAccount(self, "ApiGatewayAccount", cloud_watch_role_arn=role.role_arn)
