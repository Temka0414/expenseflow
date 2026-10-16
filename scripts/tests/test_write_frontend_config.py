from __future__ import annotations

import ast
import json
from typing import Any

from _common import REPO_ROOT
from write_frontend_config import build_frontend_config, parse_args

EXAMPLE = REPO_ROOT / "frontend" / "public" / "config.example.json"
FRONTEND_STACK = REPO_ROOT / "infra" / "expenseflow_infra" / "stacks" / "frontend_stack.py"

AUTH = {"UserPoolId": "ap-northeast-1_abc", "UserPoolClientId": "client123"}
EDGE = {"DistributionId": "E1EXAMPLE"}
DATA = {"TableName": "table", "ReceiptsBucketName": "receipts", "ReportsBucketName": "reports"}
BACKEND = {
    "ApiUrl": "https://api123.execute-api.ap-northeast-1.amazonaws.com/",
    "WebSocketUrl": "wss://ws456.execute-api.ap-northeast-1.amazonaws.com/live",
    "EventBusName": "bus",
    "ApprovalStateMachineArn": "arn:approval",
    "ReceiptStateMachineArn": "arn:receipt",
    "DashboardName": "ExpenseFlow-dev",
    "HttpApiId": "api123",
    "WebSocketApiId": "ws456",
}


def _build() -> dict[str, Any]:
    return build_frontend_config(
        region="ap-northeast-1",
        stage="dev",
        edge=EDGE,
        data=DATA,
        auth=AUTH,
        backend=BACKEND,
        default_currency="JPY",
        auto_approve_limit=5000,
        extraction_enabled=True,
    )


def _dict_keys(node: ast.expr | None) -> set[str]:
    assert isinstance(node, ast.Dict)
    return {str(key.value) for key in node.keys if isinstance(key, ast.Constant)}


def _app_config_keys() -> tuple[set[str], set[str]]:
    """The keys (top level, and under "console") of the dict `app_config()` returns in the Frontend stack.

    The source is parsed instead of imported, so the scripts need no aws-cdk-lib.
    """
    tree = ast.parse(FRONTEND_STACK.read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "app_config")
    document = next(n.value for n in ast.walk(function) if isinstance(n, ast.Return))
    assert isinstance(document, ast.Dict)
    console = next(
        value
        for key, value in zip(document.keys, document.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == "console"
    )
    return _dict_keys(document), _dict_keys(console)


def test_keys_equal_those_of_config_example_json() -> None:
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    config = _build()

    assert config.keys() == example.keys()
    assert config["console"].keys() == example["console"].keys()


def test_keys_equal_those_of_the_frontend_stack() -> None:
    keys, console_keys = _app_config_keys()
    config = _build()

    assert config.keys() == keys
    assert config["console"].keys() == console_keys


def test_values_come_from_the_stack_outputs() -> None:
    config = _build()

    assert config["apiUrl"] == "https://api123.execute-api.ap-northeast-1.amazonaws.com"  # no trailing slash
    assert config["wsUrl"] == BACKEND["WebSocketUrl"]
    assert config["userPoolId"] == config["console"]["userPoolId"] == "ap-northeast-1_abc"
    assert config["console"]["distributionId"] == "E1EXAMPLE"
    assert config["maxReceiptBytes"] == 3_500_000
    assert (config["autoApproveLimit"], config["extractionEnabled"]) == (5000, True)


def test_command_line_defaults() -> None:
    args = parse_args([])

    assert (args.stage, args.default_currency, args.no_extraction) == ("dev", "JPY", False)
    assert args.output == REPO_ROOT / "frontend" / "public" / "config.json"
