"""Budget stack (optional, account-wide): emails you before AWS charges get out of hand."""

from __future__ import annotations

from typing import Final

from aws_cdk import Stack
from aws_cdk import aws_budgets as budgets
from constructs import Construct

from expenseflow_infra.config import PROJECT_NAME, StageConfig

ACTUAL_ALERT_PERCENT: Final = 80
FORECAST_ALERT_PERCENT: Final = 100


class BudgetStack(Stack):
    """An AWS Budgets monthly cost budget with two email alerts (deployed with `-c budgetEmail=`).

    * ACTUAL > 80 %: money already spent this month passed 80 % of the budget.
    * FORECASTED > 100 %: at the current pace AWS expects the month to end over budget. This is
      the early warning; AWS needs about 5 weeks of billing history before it forecasts.

    The budget covers the whole account, not only ExpenseFlow, so a forgotten resource from another
    experiment also triggers it. (Filtering by the `Project` tag would first require activating it
    as a cost allocation tag in the Billing console.) Budgets only *alerts*; it never stops anything.
    Billing data is refreshed a few times a day, so alerts lag real spending by hours.

    Budgets has no L2 construct: `CfnBudget` is the L1 (raw CloudFormation) resource.
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig) -> None:
        if config.budget_email is None:
            raise ValueError("BudgetStack needs a notification address (-c budgetEmail=...)")
        super().__init__(
            scope,
            construct_id,
            env=config.env,
            description=f"{PROJECT_NAME}: monthly AWS cost budget with email alerts (whole account)",
        )
        subscribers = [
            budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=config.budget_email)
        ]
        budgets.CfnBudget(
            self,
            "MonthlyCostBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{PROJECT_NAME}-monthly-cost",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=config.monthly_budget_usd, unit="USD"),
                # Credits (e.g. Free Tier credits) would offset the cost down to $0. Excluding them
                # tracks real usage, so you learn your burn rate before the credits run out.
                cost_types=budgets.CfnBudget.CostTypesProperty(include_credit=False),
            ),
            notifications_with_subscribers=[
                _alert("ACTUAL", ACTUAL_ALERT_PERCENT, subscribers),
                _alert("FORECASTED", FORECAST_ALERT_PERCENT, subscribers),
            ],
        )


def _alert(
    notification_type: str, percent: int, subscribers: list[budgets.CfnBudget.SubscriberProperty]
) -> budgets.CfnBudget.NotificationWithSubscribersProperty:
    return budgets.CfnBudget.NotificationWithSubscribersProperty(
        notification=budgets.CfnBudget.NotificationProperty(
            notification_type=notification_type,
            comparison_operator="GREATER_THAN",
            threshold=percent,
            threshold_type="PERCENTAGE",
        ),
        subscribers=subscribers,
    )
