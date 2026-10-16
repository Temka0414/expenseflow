"""Auth stack: Cognito user pool (sign-up, sign-in, MFA), role groups and the SPA's app client."""

from __future__ import annotations

from typing import Final

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from constructs import Construct

from expenseflow_infra.config import PROJECT_NAME, StageConfig
from expenseflow_infra.constructs.python_function import PythonFunction
from expenseflow_infra.nag import acknowledge

EMPLOYEES_GROUP: Final = "employees"
MANAGERS_GROUP: Final = "managers"
ADMINS_GROUP: Final = "admins"
DEFAULT_GROUP: Final = EMPLOYEES_GROUP
"""Every user who confirms a self sign-up joins this group (PostConfirmation trigger)."""

# Lower precedence wins when a user is in several groups (it decides which group's IAM role ends up
# in the token as `cognito:preferred_role`). The app itself checks all groups in `cognito:groups`.
_GROUP_PRECEDENCE: Final = {ADMINS_GROUP: 0, MANAGERS_GROUP: 10, EMPLOYEES_GROUP: 20}


class AuthStack(Stack):
    """Users sign up with their email address; Cognito issues the JWTs that every API call carries.

    Attributes:
        user_pool: The user pool (JWT issuer for the HTTP API authorizer and the WebSocket authorizer).
        user_pool_client: The SPA's app client (the JWT audience).
        post_confirmation: Lambda that puts each newly confirmed user into the `employees` group.
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig) -> None:
        super().__init__(
            scope,
            construct_id,
            env=config.env,
            description=f"{PROJECT_NAME} ({config.stage}): Cognito user pool, groups and app client",
            termination_protection=config.is_prod,
        )
        self.user_pool = self._user_pool(config)
        for group_name, precedence in _GROUP_PRECEDENCE.items():
            cognito.UserPoolGroup(
                self,
                f"{group_name.capitalize()}Group",
                user_pool=self.user_pool,
                group_name=group_name,
                precedence=precedence,
                description=f"{PROJECT_NAME} {group_name}",
            )
        self.user_pool_client = self._web_client()
        self.post_confirmation = self._post_confirmation_trigger(config)

        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)

    def _user_pool(self, config: StageConfig) -> cognito.UserPool:
        pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{PROJECT_NAME}-{config.stage}-users",
            # Essentials: the default plan for new pools (10,000 monthly active users free);
            # set explicitly because CDK omits `UserPoolTier` otherwise.
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            # Cognito emails a code at sign-up; until it is confirmed the user cannot sign in.
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            # Changing the email keeps the old (verified) one active until the new one is confirmed.
            keep_original=cognito.KeepOriginalAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                # `name` is shown in the UI and on expense reports; optional, so the API falls back to
                # the email address. (Required attributes can never be changed after creation.)
                fullname=cognito.StandardAttribute(required=False, mutable=True),
            ),
            # cdk-nag COG1 wants every character class: the L2 only writes the flags set explicitly.
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            # Users may add an authenticator app (TOTP). SMS would cost money per message and needs
            # an SNS origination setup, so it stays off.
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            # The CDK default tries a phone number first; this pool has no phone attribute.
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            deletion_protection=config.is_prod,
            removal_policy=config.removal_policy,
        )
        acknowledge(
            pool,
            (
                "AwsSolutions-COG2",
                "MFA is optional so that sign-up stays simple for a learning project; users can enable "
                "TOTP themselves. Set mfa=Mfa.REQUIRED for real deployments.",
            ),
            (
                "AwsSolutions-COG8",
                "Threat protection needs the Plus feature plan, which has no free tier; Essentials "
                "includes 10,000 monthly active users for free, which keeps this project within budget.",
            ),
        )
        return pool

    def _web_client(self) -> cognito.UserPoolClient:
        """App client for the browser SPA (a *public* client: it cannot keep a secret)."""
        return self.user_pool.add_client(
            "WebClient",
            generate_secret=False,
            # The SPA signs in with Cognito's API (SRP) rather than the hosted login pages, so no
            # OAuth flows, scopes or callback URLs are enabled. (CDK's default enables them.)
            disable_o_auth=True,
            auth_flows=cognito.AuthFlow(
                # Secure Remote Password: the password itself never leaves the browser.
                user_srp=True,
                # Server-side password sign-in for scripts/smoke_test.py. Calling it requires the IAM
                # permission cognito-idp:AdminInitiateAuth, so it is not usable from a browser.
                admin_user_password=True,
            ),
            # "User does not exist" and "wrong password" get the same answer: no account probing.
            prevent_user_existence_errors=True,
            # Sign-out revokes the refresh token (and the access tokens issued from it). API Gateway's
            # JWT authorizer checks only signature and expiry, so a revoked token keeps working
            # there until it expires - one reason to keep token lifetimes at 1 hour.
            enable_token_revocation=True,
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

    def _post_confirmation_trigger(self, config: StageConfig) -> PythonFunction:
        trigger = PythonFunction(
            self,
            "PostConfirmationFunction",
            config=config,
            handler="handlers.post_confirmation.handler",
            service="post-confirmation",
            description="Cognito PostConfirmation trigger: adds new users to the default group",
            # Cognito waits at most 5 seconds for a trigger. No layer: stdlib + boto3 start fastest.
            timeout=Duration.seconds(5),
            memory_size=128,
            layer=None,
            environment={"DEFAULT_GROUP": DEFAULT_GROUP},
        )
        self.user_pool.add_trigger(cognito.UserPoolOperation.POST_CONFIRMATION, trigger.function)

        # Circular dependency trap: the pool references the function (its trigger), and a grant such
        # as `user_pool.grant(fn, ...)` adds the pool ARN to the role's *default* policy, which the
        # function depends on - Pool -> Function -> DefaultPolicy -> Pool, rejected at synth. A
        # standalone policy attached to the role is not a dependency of the function, which breaks
        # the cycle while still granting the action on this pool only (least privilege).
        iam.Policy(
            self,
            "PostConfirmationAddToGroupPolicy",
            roles=[trigger.role],
            statements=[
                iam.PolicyStatement(
                    actions=["cognito-idp:AdminAddUserToGroup"],
                    resources=[self.user_pool.user_pool_arn],
                )
            ],
        )
        return trigger
