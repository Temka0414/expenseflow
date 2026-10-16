"""Shared pytest fixtures for the CDK app.

Every test builds a NEW `App`: `Template.from_stack()` synthesizes the whole app, after which the
construct tree is frozen. Each App gets:

* the full `cdk.json` context, so tests see the same feature flags as `cdk synth` (several
  constructs behave differently without them, e.g. S3 access logs via bucket policy);
* `aws:cdk:bundling-stacks: []`, which skips asset bundling (no pip run for the Lambda layer);
* a short temporary output directory (Windows paths are limited to 260 characters).

On Windows the jsii runtime prints a harmless `ENOTEMPTY ... jsii-kernel` trace at exit; ignore it.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Stack
from cdk_nag import AwsSolutionsChecks

TEST_ACCOUNT = "123456789012"
TEST_REGION = "ap-northeast-1"
CDK_JSON = Path(__file__).resolve().parent.parent / "cdk.json"

type AppFactory = Callable[..., App]
type NagCheck = Callable[[Stack], None]


@pytest.fixture(scope="session")
def cdk_context() -> dict[str, Any]:
    """The `context` block of cdk.json: defaults + the recommended feature flags."""
    context: dict[str, Any] = json.loads(CDK_JSON.read_text(encoding="utf-8"))["context"]
    return context


@pytest.fixture(autouse=True)
def cdk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the account/Region that the CDK CLI would pass to `app.py` through these variables."""
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", TEST_ACCOUNT)
    monkeypatch.setenv("CDK_DEFAULT_REGION", TEST_REGION)


@pytest.fixture
def frontend_dist(tmp_path: Path) -> Path:
    """A fake `frontend/dist` (Vite build output), so tests never depend on a real `npm run build`."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/assets/index-abc123.js"></script></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "index-abc123.js").write_text("console.log('ExpenseFlow');", encoding="utf-8")
    return dist


@pytest.fixture
def make_app(cdk_context: dict[str, Any], frontend_dist: Path) -> Iterator[AppFactory]:
    """Factory: `make_app(stage="prod", alarmEmail="ops@example.com")` returns a fresh App.

    Keyword arguments override context keys (use `**{"some:key": value}` for keys that are not
    Python identifiers). `frontendDist` defaults to the fake build from the `frontend_dist` fixture.
    """
    outdirs: list[str] = []

    def factory(**overrides: object) -> App:
        outdir = tempfile.mkdtemp(prefix="cdk")
        outdirs.append(outdir)
        context = {
            **cdk_context,
            "aws:cdk:bundling-stacks": [],
            "frontendDist": str(frontend_dist),
            **overrides,
        }
        return App(context=context, outdir=outdir)

    yield factory
    for outdir in outdirs:
        shutil.rmtree(outdir, ignore_errors=True)


@pytest.fixture
def assert_nag_clean() -> NagCheck:
    """Assert that cdk-nag's AWS Solutions pack reports nothing unacknowledged for a stack.

    This is the same check `app.py` runs at synth time (where any finding fails `cdk synth`),
    but the report lists every violating construct path, which makes failures easy to fix.
    """

    def check(stack: Stack) -> None:
        report = AwsSolutionsChecks(verbose=True).validate_scope(stack)
        findings = [
            f"{violation.rule_name}: {[found.construct_path for found in violation.violating_resources]}"
            for violation in report.violations
        ]
        assert report.success, "unacknowledged cdk-nag findings:\n" + "\n".join(findings)

    return check
