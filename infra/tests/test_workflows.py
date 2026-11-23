"""Guards for the GitHub Actions workflows: what a public repository's Actions log must never show.

The workflows cannot run locally, so these plain-text checks keep the safety settings from being
dropped by accident. Each one was a real problem found before the first CI deploy. Comments are
stripped first, so a comment that mentions a setting cannot stand in for the setting itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _code(name: str) -> str:
    """The workflow without comment lines and trailing ` # ...` comments."""
    lines = (WORKFLOWS / name).read_text(encoding="utf-8").splitlines()
    return "\n".join(line.split(" #", 1)[0].rstrip() for line in lines if not line.lstrip().startswith("#"))


def _split_jobs(code: str) -> tuple[str, dict[str, str]]:
    """The workflow-level part and each job's body, keyed by job id (2-space indented under `jobs:`)."""
    head, _, rest = code.partition("\njobs:\n")
    jobs: dict[str, str] = {}
    current = ""
    for line in rest.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            current = line.strip().rstrip(":")
            jobs[current] = ""
        elif current:
            jobs[current] += line + "\n"
    return head, jobs


@pytest.fixture(scope="module")
def deploy() -> str:
    return _code("deploy.yml")


def test_deploy_keeps_ai_off_unless_a_model_is_configured(deploy: str) -> None:
    # Without the flag the CDK derives a Bedrock model and turns AI receipt reading on.
    assert "EXTRACTION_MODEL_ID: ${{ vars.EXTRACTION_MODEL_ID || 'none' }}" in deploy
    assert '-c "extractionModelId=$EXTRACTION_MODEL_ID"' in deploy


def test_deploy_hides_the_account_id_and_role_id(deploy: str) -> None:
    # A variable is printed unmasked with the step inputs; the role ARN contains the account ID.
    assert "role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}" in deploy
    assert "vars.AWS_DEPLOY_ROLE_ARN" not in deploy
    assert "mask-aws-account-id: true" in deploy
    # The credentials step logs the role's unique id before the account ID is masked.
    assert "AWS_DEPLOY_ROLE_ID: ${{ secrets.AWS_DEPLOY_ROLE_ID }}" in deploy
    assert 'echo "::add-mask::$AWS_DEPLOY_ROLE_ID"' in deploy


def test_deploy_passes_the_alarm_email_only_as_a_secret(deploy: str) -> None:
    assert "ALARM_EMAIL: ${{ secrets.ALARM_EMAIL }}" in deploy
    assert "vars.ALARM_EMAIL" not in deploy


def test_deploy_publishes_no_templates_and_no_stack_outputs(deploy: str) -> None:
    # Templates contain the account ID (and the alarm e-mail); outputs are the deployed URLs and ids.
    assert "cdk.out" not in deploy
    _, jobs = _split_jobs(deploy)
    assert "upload-artifact" not in jobs["deploy"]
    assert "--require-approval never --no-color 2>&1 \\\n" in deploy
    assert "| grep --line-buffered -vE '^[[:space:]]*ExpenseFlow-[A-Za-z0-9-]+\\.[A-Za-z0-9]+ = '" in deploy
    # pipefail: a failed deploy must still fail the step through the grep.
    assert "        shell: bash\n" in jobs["deploy"]


@pytest.mark.parametrize(("workflow", "trusted_job"), [("deploy.yml", "deploy"), ("pages.yml", "deploy")])
def test_only_one_job_may_request_oidc_tokens(workflow: str, trusted_job: str) -> None:
    # The AWS deploy role trusts any main-branch job that can mint a token and has no environment, so jobs
    # that run third-party npm code (the frontend builds) must not have id-token: write.
    head, jobs = _split_jobs(_code(workflow))
    assert "id-token" not in head
    for job, body in jobs.items():
        if job == trusted_job:
            assert "\n      id-token: write\n" in body
        else:
            assert "id-token" not in body, f"{workflow}: job {job!r} must not request OIDC tokens"


def test_the_pages_deploy_job_uses_its_environment() -> None:
    # Its OIDC subject is then ...:environment:github-pages, which the AWS deploy role does not trust.
    _, jobs = _split_jobs(_code("pages.yml"))
    assert "    environment:\n      name: github-pages\n" in jobs["deploy"]


def test_the_aws_deploy_job_has_no_environment(deploy: str) -> None:
    # An environment changes the OIDC subject, and the role's trust policy would no longer match.
    _, jobs = _split_jobs(deploy)
    assert "environment:" not in jobs["deploy"]
