"""cdk-nag helpers: record *why* a finding is accepted, right next to the code that causes it.

cdk-nag 3 runs as a CDK policy-validation plugin registered on the App
(`Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))`). It evaluates every
template during synth and FAILS the synth on any finding that is not acknowledged, warnings included.
Each finding is therefore either fixed in code or acknowledged with an honest, specific reason; the
reasons end up in `cdk.out/validation-report.json` for reviewers.

Acknowledgments cover the construct and all of its descendants, so attach them as low in the tree as
possible. Granular findings need their full `Rule[Finding]` id, e.g. `AwsSolutions-IAM5[Resource::*]`;
the plain rule id does not cover them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Final

import jsii
from aws_cdk import Acknowledgment, Stack, Validations
from constructs import IConstruct, IValidation

LAMBDA_BASIC_EXECUTION_ROLE: Final = (
    "AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]"
)
"""Finding id for roles using the AWS managed logging policy (CDK-internal helper Lambdas use it)."""


def acknowledge(construct: IConstruct, *pairs: tuple[str, str]) -> None:
    """Acknowledge cdk-nag findings on `construct` and its children, given as `(rule_id, reason)` pairs."""
    if not pairs:
        raise ValueError("acknowledge() needs at least one (rule_id, reason) pair")
    Validations.of(construct).acknowledge(
        *(Acknowledgment(id=rule_id, reason=reason) for rule_id, reason in pairs)
    )


def acknowledge_wildcards(
    scope: IConstruct, reason: str, *, actions: Iterable[str] = (), resources: Iterable[str] = ()
) -> None:
    """Acknowledge cdk-nag's IAM5 findings for wildcard `actions`/`resources` granted inside `scope`.

    cdk-nag names each finding after the CloudFormation value it found, for example
    `AwsSolutions-IAM5[Resource::<TableCD117FA1.Arn>/index/*]`, and an acknowledgment must repeat
    that id exactly. Gotcha: a reference to *another stack* only takes its final form
    (`Fn::GetStackOutput`) while CDK prepares the app for synthesis, so the ids cannot be computed
    while the tree is being built. CDK runs construct validations after that preparation and before
    policy-validation plugins such as cdk-nag, so the ids are computed in one (`_WildcardAcknowledgment`).
    """
    scope.node.add_validation(_WildcardAcknowledgment(scope, reason, tuple(actions), tuple(resources)))


@jsii.implements(IValidation)
class _WildcardAcknowledgment:
    """Construct validation that acknowledges IAM5 findings once all references are resolved."""

    def __init__(
        self, scope: IConstruct, reason: str, actions: tuple[str, ...], resources: tuple[str, ...]
    ) -> None:
        self._scope = scope
        self._reason = reason
        self._actions = actions
        self._resources = resources

    def validate(self) -> list[str]:
        stack = Stack.of(self._scope)
        resources = (_render_like_cdk_nag(stack.resolve(resource)) for resource in self._resources)
        findings = [
            *(f"AwsSolutions-IAM5[Action::{action}]" for action in self._actions if "*" in action),
            *(f"AwsSolutions-IAM5[Resource::{resource}]" for resource in resources if "*" in resource),
        ]
        if findings:
            acknowledge(self._scope, *((finding, self._reason) for finding in dict.fromkeys(findings)))
        return []  # never a validation error: this only records acknowledgments


def _render_like_cdk_nag(value: object) -> str:
    """How cdk-nag spells a resolved CloudFormation value in a finding id (its `flattenCfnReference`)."""
    match value:
        case str():
            return value.replace("${", "<").replace("}", ">")
        case {"Fn::Join": [delimiter, items]}:
            return str(delimiter).join(_render_like_cdk_nag(item) for item in items)
        case {"Fn::Sub": argument} | {"Fn::ImportValue": argument}:
            return _render_like_cdk_nag(argument)
        case {"Fn::GetAtt": [resource, attribute]}:
            return f"<{_render_like_cdk_nag(resource)}.{_render_like_cdk_nag(attribute)}>"
        case {"Ref": name}:
            return f"<{_render_like_cdk_nag(name)}>"
        case _:
            # Any other intrinsic, e.g. a reference to another stack: {"Fn::GetStackOutput": {...}}.
            return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
