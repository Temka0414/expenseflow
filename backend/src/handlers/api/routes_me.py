"""Routes about the caller: ``GET /me`` (profile and roles) and ``GET /stats`` (approved totals per month)."""

from __future__ import annotations

from typing import Annotated, Any

from aws_lambda_powertools.event_handler.openapi.params import Query
from aws_lambda_powertools.event_handler.router import APIGatewayHttpRouter

from expenseflow import ids
from expenseflow.models import total_to_api
from expenseflow.repository import ExpenseRepository
from handlers.api.deps import CurrentCaller

router = APIGatewayHttpRouter()
repository = ExpenseRepository()


@router.get("/me")
def get_me(caller: CurrentCaller) -> dict[str, Any]:
    """Who the SPA is talking to; it shows the approval queue and reports only when the flags allow it."""
    return caller.to_api()


@router.get("/stats")
def get_stats(caller: CurrentCaller, year: Annotated[int | None, Query(ge=2000, le=2100)] = None) -> dict[str, Any]:
    """Approved amounts per month and currency; managers and admins also get the organisation-wide totals.

    The totals are pre-aggregated by the stream processor whenever an expense is approved, so this is two cheap
    ``Query`` calls instead of scanning every expense of the year.
    """
    year = year or int(ids.current_month_jst()[:4])
    mine = [total_to_api(item) for item in repository.get_totals(caller.user_id, year)]
    org = None
    if caller.can_approve:
        org = [total_to_api(item) for item in repository.get_totals(caller.user_id, year, org=True)]
    return {"year": year, "mine": mine, "org": org}
