"""Check that this AWS account can call Claude on Amazon Bedrock. Run it ONCE, with admin credentials, before
uploading the first receipt.

Why it exists: the first call to an Anthropic model in an account subscribes the account to that model through
AWS Marketplace. The subscription needs ``aws-marketplace:Subscribe``, ``ViewSubscriptions`` and ``Unsubscribe``,
which the receipt-extraction Lambda deliberately does NOT have (least privilege). So an administrator makes one
tiny call per model here - the primary model and the refusal fallback - with exactly the client the Lambda uses
(``expenseflow.receipts``), and the script explains any failure.

Each call costs a fraction of a US cent. Requires the backend dev dependencies
(``pip install -r backend/requirements-dev.txt``, which includes ``anthropic[bedrock]``).

Example (PowerShell)::

    python scripts/check_bedrock.py --profile expenseflow --region ap-northeast-1
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import anthropic
import boto3
from anthropic import AnthropicBedrock, BetaFallbackState
from botocore.exceptions import ProfileNotFound

from _common import REPO_ROOT, ScriptError, run_script

sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))  # reuse the Lambda's own client code

from expenseflow import receipts

PING_MAX_TOKENS: Final = 128  # Opus 5.5 always thinks a little; a 200 response is all this check needs
PING_TIMEOUT_S: Final = 60.0
DISABLED: Final = "none"


@dataclass(frozen=True)
class Probe:
    role: str  # "primary" or "fallback"
    model_id: str
    client: AnthropicBedrock


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Send one tiny request to each configured Claude model on Amazon Bedrock to verify model access "
            "and trigger the one-time AWS Marketplace subscription. Use the same values as the CDK context "
            "(bedrockRegion, extractionModelId, extractionFallbackModelId)."
        ),
    )
    parser.add_argument("--profile", help="AWS CLI profile with admin rights (default: the default credential chain)")
    parser.add_argument("--region", help="Bedrock region (default: the profile's region)")
    parser.add_argument(
        "--model", help="model id (default: Claude Opus 5.5, e.g. jp.anthropic.claude-opus-5-5 in Tokyo)"
    )
    parser.add_argument(
        "--fallback-model",
        help=f"refusal fallback model id, or '{DISABLED}' to skip it (default: Claude Opus 4.8 for the same geo)",
    )
    return parser.parse_args(argv)


def _session(profile: str | None, region: str | None) -> boto3.session.Session:
    try:
        return boto3.session.Session(profile_name=profile, region_name=region)
    except ProfileNotFound as exc:
        raise ScriptError(f"{exc}. List your profiles with: aws configure list-profiles") from exc


def _bedrock_region(args: argparse.Namespace, session: boto3.session.Session) -> str:
    """Same defaulting as the Lambda (``settings.bedrock_region``): the account's region."""
    if args.region:
        return str(args.region)
    if not session.region_name:
        raise ScriptError("No AWS region configured. Pass --region (for example --region ap-northeast-1).")
    return str(session.region_name)


def _probes(region: str, model: str | None, fallback: str | None) -> list[Probe]:
    primary_id = model or receipts.model_id_for(region)
    fallback_id = fallback or receipts.model_id_for(region, receipts.FALLBACK_BASE_MODEL_ID)
    fallback_enabled = fallback_id.lower() != DISABLED
    # The primary call uses the Lambda's exact client, refusal middleware included, so it also proves that the
    # endpoint accepts the middleware's beta flag. The fallback model gets a plain client: it must answer itself.
    probes = [Probe("primary", primary_id, receipts.get_client(region, fallback_id if fallback_enabled else None))]
    if fallback_enabled:
        probes.append(Probe("fallback", fallback_id, receipts.get_client(region, None)))
    return probes


def _ping(probe: Probe) -> str:
    """One minimal Messages request; returns a one-line summary or raises an ``ExtractionError`` subclass."""
    started = time.perf_counter()
    try:
        with BetaFallbackState():
            response = probe.client.beta.messages.create(  # type: ignore[misc]  # see expenseflow.receipts
                model=probe.model_id,
                max_tokens=PING_MAX_TOKENS,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": "Reply with the single word OK."}],
                timeout=PING_TIMEOUT_S,
            )
    except anthropic.APIError as error:
        raise receipts.classify_api_error(error) from error
    seconds = time.perf_counter() - started
    usage = response.usage
    return (
        f"answered in {seconds:.1f} s (stop_reason={response.stop_reason}, "
        f"{usage.input_tokens} input / {usage.output_tokens} output tokens)"
    )


def _access_denied_advice(probe: Probe, region: str) -> str:
    return (
        "Access was denied. Check, in this order:\n"
        "  1. Your credentials may call bedrock:InvokeModel (an administrator can).\n"
        f"  2. Model access: Amazon Bedrock console ({region}) > Model catalog > open {probe.model_id} and follow\n"
        "     the access steps (Anthropic models can ask for a one-time use-case form on bedrock-runtime).\n"
        "  3. AWS Marketplace: the first call subscribes the account. It needs aws-marketplace:Subscribe,\n"
        "     ViewSubscriptions and Unsubscribe plus a valid payment method, and can take up to 15 minutes -\n"
        "     wait a few minutes and run this script again.\n"
        "  4. Accounts on the AWS Free plan cannot use Marketplace models: upgrade the account to a Paid plan."
    )


def _advice(error: receipts.ExtractionError, probe: Probe, region: str) -> str:
    """What to do next, per failure type (the same classes the Lambda reports to Step Functions)."""
    if isinstance(error, receipts.ModelThrottledError):
        return (
            "Bedrock throttled the request. New accounts start with low tokens-per-minute quotas: wait a minute and "
            "retry, or request an increase in the Service Quotas console (service 'Amazon Bedrock')."
        )
    if isinstance(error, receipts.ModelUnavailableError):
        return "A network problem or a temporary Bedrock error. Check your connection/proxy and run the script again."
    if error.status_code == 401:
        return "The credentials were rejected. Sign in again (aws login / aws sso login) and pass the same --profile."
    if error.status_code == 403:
        return _access_denied_advice(probe, region)
    if error.status_code == 404 or "model identifier" in str(error).lower():
        return (
            f"{probe.model_id} is not available in {region}. bedrock-runtime needs a geo inference profile id "
            f"(here: {receipts.model_id_for(region)}). Pass --model/--region, then use the same values as CDK context."
        )
    if probe.role == "primary" and ("beta" in str(error).lower() or "fallback" in str(error).lower()):
        return (
            "The endpoint rejected the refusal-fallback beta flag. Deploy with -c extractionFallbackModelId=none "
            "(and run this script with --fallback-model none) to switch the fallback off."
        )
    return "Bedrock rejected the request. The message above names the problem."


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile  # the Anthropic SDK signs with boto3's default credential chain
    session = _session(args.profile, args.region)
    region = _bedrock_region(args, session)

    identity = session.client("sts", region_name=region).get_caller_identity()  # fails fast without credentials
    print(f"AWS account {identity['Account']} as {identity['Arn']}")
    print(f"Bedrock region: {region}\n")

    failures = 0
    for probe in _probes(region, args.model, args.fallback_model):
        print(f"[{probe.role}] {probe.model_id}: ", end="", flush=True)
        try:
            print(f"OK - {_ping(probe)}")
        except receipts.ExtractionError as error:
            failures += 1
            request_id = f" (request id {error.request_id})" if error.request_id else ""
            print(f"FAILED - {type(error).__name__}: {error}{request_id}")
            print(f"{_advice(error, probe, region)}\n")

    if failures:
        return 1
    print("\nClaude is ready. Use the same model ids in the CDK context (extractionModelId/extractionFallbackModelId).")
    return 0


if __name__ == "__main__":
    run_script(main)
