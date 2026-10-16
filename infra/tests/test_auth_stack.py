"""Auth stack: Cognito user pool, groups, SPA client and the PostConfirmation trigger."""

from __future__ import annotations

import pytest
from aws_cdk import CfnElement, Duration, Stack
from aws_cdk.assertions import Match, Template
from constructs import IConstruct

from expenseflow_infra.config import StageConfig
from expenseflow_infra.constructs.python_function import PythonFunction
from expenseflow_infra.stacks.auth_stack import AuthStack
from tests.conftest import AppFactory, NagCheck


def _auth(make_app: AppFactory, **context: object) -> AuthStack:
    app = make_app(**context)
    config = StageConfig.from_app(app)
    return AuthStack(app, config.stack_id("Auth"), config=config)


def _logical_id(construct: IConstruct) -> str:
    """Logical id of the CloudFormation resource behind an L2 construct (its `default_child`)."""
    resource = construct.node.default_child
    assert isinstance(resource, CfnElement)
    return Stack.of(construct).get_logical_id(resource)


def test_user_pool(make_app: AppFactory) -> None:
    template = Template.from_stack(_auth(make_app))

    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "UserPoolTier": "ESSENTIALS",
            "UsernameAttributes": ["email"],
            "AutoVerifiedAttributes": ["email"],
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": False},
            "Policies": {
                "PasswordPolicy": {
                    "MinimumLength": 12,
                    "RequireLowercase": True,
                    "RequireUppercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": True,
                    "TemporaryPasswordValidityDays": 3,
                }
            },
            "MfaConfiguration": "OPTIONAL",
            "EnabledMfas": ["SOFTWARE_TOKEN_MFA"],
            "AccountRecoverySetting": {"RecoveryMechanisms": [{"Name": "verified_email", "Priority": 1}]},
            "DeletionProtection": "INACTIVE",
            "Schema": Match.array_with([Match.object_like({"Name": "name", "Required": False})]),
            "LambdaConfig": {"PostConfirmation": Match.any_value()},
        },
    )


def test_groups_and_precedence(make_app: AppFactory) -> None:
    template = Template.from_stack(_auth(make_app))

    template.resource_count_is("AWS::Cognito::UserPoolGroup", 3)
    for group_name, precedence in (("admins", 0), ("managers", 10), ("employees", 20)):
        template.has_resource_properties(
            "AWS::Cognito::UserPoolGroup", {"GroupName": group_name, "Precedence": precedence}
        )


def test_spa_client_is_public_and_uses_srp(make_app: AppFactory) -> None:
    template = Template.from_stack(_auth(make_app))

    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "GenerateSecret": False,
            "AllowedOAuthFlowsUserPoolClient": False,
            "ExplicitAuthFlows": Match.array_equals(
                ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
            ),
            "PreventUserExistenceErrors": "ENABLED",
            "EnableTokenRevocation": True,
            "IdTokenValidity": 60,
            "AccessTokenValidity": 60,
            "RefreshTokenValidity": 43200,
            "TokenValidityUnits": {"IdToken": "minutes", "AccessToken": "minutes", "RefreshToken": "minutes"},
        },
    )


def test_post_confirmation_function(make_app: AppFactory) -> None:
    stack = _auth(make_app)
    template = Template.from_stack(stack)

    template.resource_count_is("AWS::Lambda::Function", 1)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "handlers.post_confirmation.handler",
            "Runtime": "python3.13",
            "Architectures": ["arm64"],
            "Timeout": 5,
            "MemorySize": 128,
            "Layers": Match.absent(),
            "ReservedConcurrentExecutions": Match.absent(),
            "TracingConfig": {"Mode": "Active"},
            "LoggingConfig": {
                "LogFormat": "JSON",
                "ApplicationLogLevel": "INFO",
                "SystemLogLevel": "WARN",
                "LogGroup": {"Ref": _logical_id(stack.post_confirmation.log_group)},
            },
            "Environment": {
                "Variables": {
                    "STAGE": "dev",
                    "POWERTOOLS_SERVICE_NAME": "post-confirmation",
                    "POWERTOOLS_METRICS_NAMESPACE": "ExpenseFlow",
                    "DEFAULT_GROUP": "employees",
                }
            },
        },
    )
    template.has_resource(
        "AWS::Logs::LogGroup", {"Properties": {"RetentionInDays": 30}, "DeletionPolicy": "Delete"}
    )
    # Cognito may invoke the function, but only on behalf of this pool.
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Principal": "cognito-idp.amazonaws.com",
            "SourceArn": {"Fn::GetAtt": [_logical_id(stack.user_pool), "Arn"]},
        },
    )


def test_execution_role_writes_only_to_its_own_log_group(make_app: AppFactory) -> None:
    stack = _auth(make_app)
    template = Template.from_stack(stack)

    template.has_resource_properties("AWS::IAM::Role", {"ManagedPolicyArns": Match.absent()})
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        {
                            "Effect": "Allow",
                            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                            "Resource": {
                                "Fn::GetAtt": [_logical_id(stack.post_confirmation.log_group), "Arn"]
                            },
                        }
                    ]
                )
            }
        },
    )


def test_add_to_group_permission_avoids_the_trigger_cycle(make_app: AppFactory) -> None:
    stack = _auth(make_app)
    # Synthesis runs CloudFormation validation (rule F3004 rejects circular dependencies).
    template = Template.from_stack(stack)
    pool_id = _logical_id(stack.user_pool)

    policies = template.find_resources(
        "AWS::IAM::Policy",
        {
            "Properties": {
                "PolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "cognito-idp:AdminAddUserToGroup",
                            "Resource": {"Fn::GetAtt": [pool_id, "Arn"]},
                        }
                    ]
                }
            }
        },
    )
    assert len(policies) == 1
    [policy_id] = policies
    function = next(iter(template.find_resources("AWS::Lambda::Function").values()))
    assert policy_id not in function.get("DependsOn", [])


def test_prod_protects_the_user_pool(make_app: AppFactory) -> None:
    template = Template.from_stack(_auth(make_app, stage="prod"))

    template.has_resource(
        "AWS::Cognito::UserPool",
        {"DeletionPolicy": "Retain", "Properties": Match.object_like({"DeletionProtection": "ACTIVE"})},
    )


def test_outputs(make_app: AppFactory) -> None:
    template = Template.from_stack(_auth(make_app))

    assert set(template.find_outputs("*")) >= {"UserPoolId", "UserPoolClientId"}


def test_python_function_rejects_memory_outside_the_quota_safe_range(make_app: AppFactory) -> None:
    app = make_app()
    stack = Stack(app, "Probe")
    with pytest.raises(ValueError, match="memory_size"):
        PythonFunction(
            stack,
            "TooBig",
            config=StageConfig.from_app(app),
            handler="handlers.post_confirmation.handler",
            service="probe",
            description="probe",
            timeout=Duration.seconds(3),
            memory_size=2048,
            layer=None,
        )


def test_cdk_nag_passes(make_app: AppFactory, assert_nag_clean: NagCheck) -> None:
    assert_nag_clean(_auth(make_app))
    assert_nag_clean(_auth(make_app, stage="prod"))
