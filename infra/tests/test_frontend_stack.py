"""Frontend stack: two BucketDeployments of the Vite build plus the generated `config.json`."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Stack
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk.assertions import Annotations, Match, Template

from app import build
from tests.conftest import TEST_REGION, AppFactory, NagCheck

FRONTEND = "ExpenseFlow-Dev-Frontend"
DEPLOYMENT = "Custom::CDKBucketDeployment"
# Source.json_data replaces each deploy-time token with a marker; the deployment substitutes the
# marker with the JSON-quoted value, so the staged file only becomes valid JSON at deploy time.
MARKER = re.compile(r"<<marker:0xbaba:\d+>>")

CONFIG_KEYS = {
    "region",
    "stage",
    "userPoolId",
    "userPoolClientId",
    "apiUrl",
    "wsUrl",
    "maxReceiptBytes",
    "defaultCurrency",
    "autoApproveLimit",
    "extractionEnabled",
    "console",
}
# config.json "console" key -> the stack whose output provides the value.
CONSOLE_SOURCES = {
    "tableName": "Data",
    "receiptsBucket": "Data",
    "reportsBucket": "Data",
    "eventBusName": "Backend",
    "approvalStateMachineArn": "Backend",
    "receiptStateMachineArn": "Backend",
    "dashboardName": "Backend",
    "httpApiId": "Backend",
    "webSocketApiId": "Backend",
    "userPoolId": "Auth",
    "distributionId": "Edge",
}


@pytest.fixture
def vite_dist(tmp_path: Path) -> Path:
    """A Vite build that also contains a stale local `config.json` and a source map."""
    dist = tmp_path / "web"
    files = {
        "index.html": '<!doctype html><script type="module" src="/assets/index-abc123.js"></script>',
        "config.json": '{"apiUrl": "http://localhost:3000"}',
        "favicon.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
        "assets/index-abc123.js": "console.log('ExpenseFlow');",
        "assets/index-abc123.js.map": "{}",
    }
    for name, content in files.items():
        (dist / name).parent.mkdir(parents=True, exist_ok=True)
        (dist / name).write_text(content, encoding="utf-8")
    return dist


def _app(make_app: AppFactory, **context: object) -> App:
    app = make_app(**context)
    build(app)
    return app


def _stack(app: App, stack_id: str = FRONTEND) -> Stack:
    stack = app.node.find_child(stack_id)
    assert isinstance(stack, Stack)
    return stack


def _deployments(template: Template) -> tuple[str, Mapping[str, Any], str, Mapping[str, Any]]:
    """(logical id, resource) of the hashed-assets deployment, then of the entry deployment."""
    resources = template.find_resources(DEPLOYMENT)
    assert len(resources) == 2, sorted(resources)
    (assets_id,) = (logical_id for logical_id in resources if logical_id.startswith("DeployAssets"))
    (entry_id,) = (logical_id for logical_id in resources if logical_id.startswith("DeployEntry"))
    return assets_id, resources[assets_id], entry_id, resources[entry_id]


def _asset_dirs(app: App, deployment_id: str) -> list[Path]:
    """Staged directory of each source of a deployment (in source order) inside the cloud assembly."""
    deployment = _stack(app).node.find_child(deployment_id)
    return [
        Path(app.outdir) / child.asset_path
        for child in deployment.node.children
        if isinstance(child, s3_assets.Asset)
    ]


def _staged_files(app: App, deployment_id: str) -> list[set[str]]:
    return [
        {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        for root in _asset_dirs(app, deployment_id)
    ]


def _config_document(app: App) -> dict[str, Any]:
    """The staged config.json (second source of the entry deployment), markers kept as strings."""
    staged = (_asset_dirs(app, "DeployEntry")[1] / "config.json").read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(MARKER.sub(lambda match: json.dumps(match.group()), staged))
    return document


def _producer_stack(marker: str, markers: Mapping[str, Any]) -> str:
    """The stack whose output a deploy-time marker reads (weak reference: Fn::GetStackOutput)."""
    (stack_name,) = re.findall(r'"StackName": "([^"]+)"', json.dumps(markers[marker]))
    return str(stack_name)


def test_assets_and_entry_files_use_separate_cache_policies(make_app: AppFactory, vite_dist: Path) -> None:
    template = Template.from_stack(_stack(_app(make_app, frontendDist=str(vite_dist))))
    _, assets, _, entry = _deployments(template)

    assert assets["Properties"]["SystemMetadata"] == {"cache-control": "public, max-age=31536000, immutable"}
    assert assets["Properties"]["Prune"] is False
    assert "DistributionId" not in assets["Properties"]

    assert entry["Properties"]["SystemMetadata"] == {"cache-control": "no-cache"}
    assert entry["Properties"]["Prune"] is False
    assert entry["Properties"]["DistributionPaths"] == ["/index.html", "/config.json"]
    assert entry["Properties"]["DistributionId"]["Fn::GetStackOutput"]["StackName"] == "ExpenseFlow-Dev-Edge"


def test_entry_deployment_waits_for_the_assets(make_app: AppFactory, vite_dist: Path) -> None:
    template = Template.from_stack(_stack(_app(make_app, frontendDist=str(vite_dist))))
    assets_id, _, _, entry = _deployments(template)

    assert assets_id in entry["DependsOn"]


def test_each_deployment_uploads_only_its_files(make_app: AppFactory, vite_dist: Path) -> None:
    app = _app(make_app, frontendDist=str(vite_dist))
    app.synth()

    # Hashed assets: everything except index.html, the local config.json and source maps.
    assert _staged_files(app, "DeployAssets") == [{"favicon.svg", "assets/index-abc123.js"}]
    # Entry: only index.html from the build, plus the generated config.json.
    assert _staged_files(app, "DeployEntry") == [{"index.html"}, {"config.json"}]


def test_config_json_matches_the_spa_contract(make_app: AppFactory, vite_dist: Path) -> None:
    app = _app(make_app, frontendDist=str(vite_dist))
    _, _, _, entry = _deployments(Template.from_stack(_stack(app)))
    config = _config_document(app)
    markers = entry["Properties"]["SourceMarkers"][1]

    assert set(config) == CONFIG_KEYS
    assert config["region"] == TEST_REGION
    assert config["stage"] == "dev"
    assert config["maxReceiptBytes"] == 3_500_000
    assert config["defaultCurrency"] == "JPY"
    assert config["autoApproveLimit"] == 5000
    assert config["extractionEnabled"] is True
    # Values of other stacks are filled in at deploy time from their outputs.
    for key, producer in {"userPoolId": "Auth", "userPoolClientId": "Auth", "apiUrl": "Backend"}.items():
        assert _producer_stack(config[key], markers) == f"ExpenseFlow-Dev-{producer}", key
    assert set(config["console"]) == set(CONSOLE_SOURCES)
    for key, producer in CONSOLE_SOURCES.items():
        assert _producer_stack(config["console"][key], markers) == f"ExpenseFlow-Dev-{producer}", key


def test_websocket_url_is_the_live_stage(make_app: AppFactory, vite_dist: Path) -> None:
    app = _app(make_app, frontendDist=str(vite_dist))
    _, _, _, entry = _deployments(Template.from_stack(_stack(app)))
    ws_url = entry["Properties"]["SourceMarkers"][1][_config_document(app)["wsUrl"]]
    parts = ws_url["Fn::Join"][1]

    assert parts[0] == '"wss://'
    assert parts[-1] == '/live"'


def test_config_reflects_disabled_extraction_and_settings(make_app: AppFactory, vite_dist: Path) -> None:
    app = _app(
        make_app,
        frontendDist=str(vite_dist),
        extractionModelId="none",
        defaultCurrency="USD",
        autoApproveLimit="100",
    )
    Template.from_stack(_stack(app))
    config = _config_document(app)

    assert config["extractionEnabled"] is False
    assert config["defaultCurrency"] == "USD"
    assert config["autoApproveLimit"] == 100


def test_shared_handler_logs_to_a_log_group_with_retention(make_app: AppFactory, vite_dist: Path) -> None:
    template = Template.from_stack(_stack(_app(make_app, frontendDist=str(vite_dist))))
    (log_group_id,) = template.find_resources("AWS::Logs::LogGroup", {"Properties": {"RetentionInDays": 30}})

    # Both deployments share one handler Lambda (same memory size), which writes to that log group.
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.has_resource_properties(
        "AWS::Lambda::Function", {"LoggingConfig": {"LogGroup": {"Ref": log_group_id}}}
    )


def test_frontend_stack_passes_cdk_nag(
    make_app: AppFactory, vite_dist: Path, assert_nag_clean: NagCheck
) -> None:
    stack = _stack(_app(make_app, frontendDist=str(vite_dist)))
    # Synthesize first: some acknowledgments are computed once cross-stack references are final.
    Template.from_stack(stack)

    assert_nag_clean(stack)


def test_missing_build_skips_the_upload_but_keeps_a_deployable_stack(
    make_app: AppFactory, tmp_path: Path, assert_nag_clean: NagCheck
) -> None:
    stack = _stack(_app(make_app, frontendDist=str(tmp_path / "no-build")))
    template = Template.from_stack(stack)

    assert list(template.to_json()["Resources"].values()) == [
        {"Type": "AWS::CloudFormation::WaitConditionHandle"}
    ]
    warnings = Annotations.from_stack(stack).find_warning("*", Match.string_like_regexp(".*npm run build.*"))
    assert len(warnings) == 1, warnings
    assert_nag_clean(stack)
