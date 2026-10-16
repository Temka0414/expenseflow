"""Write ``frontend/public/config.json`` so ``npm run dev`` talks to a deployed stage.

The SPA has no AWS resource names baked in: it fetches ``/config.json`` when it starts. In AWS, the
Frontend stack writes that file next to ``index.html``. For local development (http://localhost:5173,
an origin the HTTP API's CORS configuration allows) this script builds the same document from the
CloudFormation stack outputs of the stage you deployed.

Example (PowerShell)::

    python scripts/write_frontend_config.py --stage dev --profile expenseflow
    cd frontend; npm run dev
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from _common import (
    REPO_ROOT,
    add_target_arguments,
    create_session,
    read_auto_approve_limit,
    run_script,
    stack_outputs,
)

OUTPUT = REPO_ROOT / "frontend" / "public" / "config.json"
MAX_RECEIPT_BYTES = 3_500_000  # the constant in infra/expenseflow_infra/config.py


def build_frontend_config(
    *,
    region: str,
    stage: str,
    edge: Mapping[str, str],
    data: Mapping[str, str],
    auth: Mapping[str, str],
    backend: Mapping[str, str],
    default_currency: str,
    auto_approve_limit: int,
    extraction_enabled: bool,
) -> dict[str, Any]:
    """The ``config.json`` document: the same keys as ``app_config()`` in infra/.../frontend_stack.py."""
    return {
        "region": region,
        "stage": stage,
        "userPoolId": auth["UserPoolId"],
        "userPoolClientId": auth["UserPoolClientId"],
        "apiUrl": backend["ApiUrl"].rstrip("/"),  # HttpApi.url ends with "/"; the SPA appends "/expenses"
        "wsUrl": backend["WebSocketUrl"],
        "maxReceiptBytes": MAX_RECEIPT_BYTES,
        "defaultCurrency": default_currency,
        "autoApproveLimit": auto_approve_limit,
        "extractionEnabled": extraction_enabled,
        "console": {  # names for the "open in the AWS console" links
            "tableName": data["TableName"],
            "receiptsBucket": data["ReceiptsBucketName"],
            "reportsBucket": data["ReportsBucketName"],
            "eventBusName": backend["EventBusName"],
            "approvalStateMachineArn": backend["ApprovalStateMachineArn"],
            "receiptStateMachineArn": backend["ReceiptStateMachineArn"],
            "dashboardName": backend["DashboardName"],
            "httpApiId": backend["HttpApiId"],
            "webSocketApiId": backend["WebSocketApiId"],
            "userPoolId": auth["UserPoolId"],
            "distributionId": edge["DistributionId"],
        },
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write frontend/public/config.json from the stack outputs of a deployed stage, so the Vite "
            "dev server (http://localhost:5173) uses that stage's Cognito user pool and APIs."
        ),
    )
    add_target_arguments(parser)
    parser.add_argument("--output", type=Path, default=OUTPUT, help="file to write (default: %(default)s)")
    parser.add_argument("--default-currency", default="JPY", help="the stage's defaultCurrency (default: %(default)s)")
    parser.add_argument(
        "--no-extraction", action="store_true", help="the stage was deployed with AI extraction switched off"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    session = create_session(args)
    print(f"Reading the outputs of the ExpenseFlow '{args.stage}' stacks in {session.region_name} ...")
    config = build_frontend_config(
        region=str(session.region_name),
        stage=args.stage,
        edge=stack_outputs(session, args.stage, "Edge"),
        data=stack_outputs(session, args.stage, "Data"),
        auth=stack_outputs(session, args.stage, "Auth"),
        backend=stack_outputs(session, args.stage, "Backend"),
        default_currency=args.default_currency.upper(),
        auto_approve_limit=read_auto_approve_limit(session, args.stage),
        extraction_enabled=not args.no_extraction,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {args.output}")
    print("Next: cd frontend; npm run dev   (then open http://localhost:5173)")
    return 0


if __name__ == "__main__":
    run_script(main)
