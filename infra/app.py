#!/usr/bin/env python3
"""Entry point of the ExpenseFlow CDK app (`cdk.json` runs `python app.py`).

`npx cdk synth` executes this script; it builds the construct tree and writes CloudFormation
templates to `cdk.out/`. `npx cdk deploy --all` then deploys every stack in dependency order.
Select a copy of the app with `-c stage=<name>` (default `dev`; `prod` retains data on delete).
"""

from __future__ import annotations

from aws_cdk import App, Tags, Validations
from cdk_nag import AwsSolutionsChecks

from expenseflow_infra.config import PROJECT_NAME, StageConfig
from expenseflow_infra.stacks.auth_stack import AuthStack
from expenseflow_infra.stacks.backend_stack import BackendStack
from expenseflow_infra.stacks.budget_stack import BudgetStack
from expenseflow_infra.stacks.data_stack import DataStack
from expenseflow_infra.stacks.edge_stack import EdgeStack
from expenseflow_infra.stacks.frontend_stack import FrontendStack
from expenseflow_infra.stacks.github_oidc_stack import GitHubOidcStack


def build(app: App) -> None:
    """Add the stacks selected by the app's context to `app` and register cdk-nag on all of them."""
    config = StageConfig.from_app(app)
    # Tags propagate to every taggable resource. Activated as cost allocation tags (Billing console),
    # they split the bill per stage in Cost Explorer.
    for key, value in config.tags.items():
        Tags.of(app).add(key, value)

    # Passing a construct from one stack to another (e.g. `edge.site_url`) creates the cross-stack
    # reference AND the deployment order; no manual wiring is needed for those.
    edge = EdgeStack(app, config.stack_id("Edge"), config=config)
    data = DataStack(app, config.stack_id("Data"), config=config, site_url=edge.site_url)
    auth = AuthStack(app, config.stack_id("Auth"), config=config)
    backend = BackendStack(
        app,
        config.stack_id("Backend"),
        config=config,
        table=data.table,
        idempotency_table=data.idempotency_table,
        receipts_bucket=data.receipts_bucket,
        reports_bucket=data.reports_bucket,
        user_pool=auth.user_pool,
        user_pool_client=auth.user_pool_client,
        site_url=edge.site_url,
    )
    frontend = FrontendStack(
        app, config.stack_id("Frontend"), config=config, edge=edge, data=data, auth=auth, backend=backend
    )
    # Without a frontend build the stack references nothing, so make the order explicit: the SPA is
    # always published after the backend its config.json points to.
    frontend.add_stack_dependency(backend, "config.json is generated from the Backend outputs")

    # Account-wide extras (no stage in their names), only when configured.
    if config.budget_email is not None:
        BudgetStack(app, f"{PROJECT_NAME}-Budget", config=config)
    if config.github_repo is not None:
        GitHubOidcStack(app, f"{PROJECT_NAME}-GitHubOidc", config=config)

    # cdk-nag checks every template against the AWS Solutions rules during synth; any finding that is
    # neither fixed nor acknowledged (see expenseflow_infra/nag.py) fails `cdk synth` and `cdk deploy`.
    Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))


def main() -> None:
    app = App()
    build(app)
    app.synth()


if __name__ == "__main__":
    main()
