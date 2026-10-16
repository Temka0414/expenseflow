from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError

from expenseflow import ids, keys
from expenseflow.auth import Caller
from expenseflow.errors import ConflictError, DomainValidationError, ForbiddenError, NotFoundError
from expenseflow.repository import ExpenseRepository, Page
from expenseflow.serialization import decode_cursor, encode_cursor
from tests.builders import (
    ADMIN_EMAIL,
    ADMIN_ID,
    ADMIN_NAME,
    EMPLOYEE_EMAIL,
    EMPLOYEE_ID,
    EMPLOYEE_NAME,
    MANAGER_EMAIL,
    MANAGER_ID,
    MANAGER_NAME,
    expense_item,
)
from tests.conftest import FakeClock

EMPLOYEE = Caller(user_id=EMPLOYEE_ID, email=EMPLOYEE_EMAIL, name=EMPLOYEE_NAME, groups=("employees",))
MANAGER = Caller(user_id=MANAGER_ID, email=MANAGER_EMAIL, name=MANAGER_NAME, groups=("managers", "employees"))
ADMIN = Caller(user_id=ADMIN_ID, email=ADMIN_EMAIL, name=ADMIN_NAME, groups=("admins",))

DECISION = {
    "approverId": MANAGER_ID,
    "approverEmail": MANAGER_EMAIL,
    "decisionComment": "Receipt is unreadable",
    "decidedAt": "2026-10-12T00:00:00.000Z",
    "autoApproved": False,
    "escalated": True,
    "approvalRequestedAt": "2026-10-11T00:00:00.000Z",
}


@pytest.fixture
def repo(table: Any, clock: FakeClock) -> ExpenseRepository:
    return ExpenseRepository()


def _put(table: Any, **overrides: Any) -> dict[str, Any]:
    item = expense_item(**overrides)
    table.put_item(Item=item)
    return item


def _stored(table: Any, expense_id: str) -> dict[str, Any] | None:
    item: dict[str, Any] | None = table.get_item(Key=keys.expense_key(expense_id)).get("Item")
    return item


def _all_pages(fetch: Any, limit: int) -> list[list[str]]:
    """Follow cursors until the last page; returns the ids of each page."""
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        page: Page = fetch(limit=limit, cursor=cursor)
        pages.append([item["id"] for item in page.items])
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor


def _assert_touched(item: dict[str, Any], status: str, actor: Caller) -> None:
    """The invariant of SPEC section 3: whoever sets updatedAt also sets GSI2SK (and GSI2PK with the status)."""
    assert item["status"] == status
    assert item[keys.GSI2PK] == f"STATUS#{status}"
    assert item[keys.GSI2SK] == f"{item['updatedAt']}#{item['id']}"
    assert (item["lastActorId"], item["lastActorEmail"]) == (actor.user_id, actor.email)


# --- create -------------------------------------------------------------------------------------------------------


def test_create_writes_a_complete_draft(repo: ExpenseRepository, table: Any) -> None:
    item = repo.create(EMPLOYEE, {"title": "Taxi", "amount": "1280", "category": "TRANSPORT"})

    expense_id = item["id"]
    assert len(expense_id) == ids.ID_LENGTH
    assert item[keys.PK] == f"EXPENSE#{expense_id}"
    assert item[keys.SK] == "META"
    assert item[keys.GSI1PK] == f"OWNER#{EMPLOYEE_ID}"
    assert item[keys.GSI1SK] == f"{item['createdAt']}#{expense_id}"
    assert item["createdAt"] == item["updatedAt"]
    _assert_touched(item, "DRAFT", EMPLOYEE)
    assert item["entityType"] == "Expense"
    assert (item["ownerId"], item["ownerEmail"], item["ownerName"]) == (EMPLOYEE_ID, EMPLOYEE_EMAIL, EMPLOYEE_NAME)
    assert item["amount"] == Decimal("1280")
    assert item["currency"] == "JPY"  # DEFAULT_CURRENCY
    assert item["version"] == 1
    assert "merchant" not in item  # unknown optional attributes are absent, never null
    assert _stored(table, expense_id) == item


def test_create_keeps_an_explicit_currency(repo: ExpenseRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_CURRENCY", "USD")

    assert repo.create(EMPLOYEE, {"title": "Taxi"})["currency"] == "USD"
    assert repo.create(EMPLOYEE, {"title": "Taxi", "currency": "EUR"})["currency"] == "EUR"


def test_create_converts_floats_exactly(repo: ExpenseRepository) -> None:
    assert str(repo.create(EMPLOYEE, {"title": "Coffee", "amount": 0.1})["amount"]) == "0.1"


def test_create_requires_a_title(repo: ExpenseRepository) -> None:
    with pytest.raises(DomainValidationError, match="title"):
        repo.create(EMPLOYEE, {"merchant": "Starbucks"})


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        ({"title": "t", "status": "APPROVED"}, "not editable"),
        ({"title": "t", "merchant": ""}, "blank values"),
        ({"title": "t", "merchant": None}, "blank values"),
    ],
)
def test_create_rejects_programming_errors(repo: ExpenseRepository, attributes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        repo.create(EMPLOYEE, attributes)


def test_create_never_overwrites_an_existing_item(
    repo: ExpenseRepository, table: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = _put(table, id="01JA2B3C4D5E6F7G8H9JKMNPQR")
    monkeypatch.setattr(ids, "new_id", lambda: existing["id"])

    with pytest.raises(ClientError) as raised:
        repo.create(MANAGER, {"title": "Hijack"})

    assert raised.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    assert _stored(table, existing["id"]) == existing


# --- get / get_with_history ------------------------------------------------------------------------------------------


def test_get(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table)

    assert repo.get(item["id"]) == item


def test_get_missing(repo: ExpenseRepository) -> None:
    with pytest.raises(NotFoundError) as raised:
        repo.get("01JA2B3C4D5E6F7G8H9JKMNPQR")

    assert raised.value.http_status == 404


def _history(expense_id: str, at: str, to: str) -> dict[str, Any]:
    return {
        keys.PK: keys.expense_pk(expense_id),
        keys.SK: keys.history_sk_status(at, to),
        "entityType": "History",
        "kind": "STATUS",
        "at": at,
        "to": to,
    }


def test_get_with_history_reads_the_item_collection(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table, status="APPROVED")
    entries = [
        _history(item["id"], "2026-10-12T00:00:00.000Z", "APPROVED"),
        _history(item["id"], "2026-10-10T00:00:00.000Z", "DRAFT"),
        _history(item["id"], "2026-10-11T00:00:00.000Z", "PENDING_APPROVAL"),
    ]
    for entry in entries:
        table.put_item(Item=entry)
    receipt = {keys.PK: keys.expense_pk(item["id"]), keys.SK: keys.history_sk_receipt("2026-10-10T00:05:00.000Z")}
    table.put_item(Item={**receipt, "kind": "RECEIPT"})
    _put(table)  # another expense

    expense, history = repo.get_with_history(item["id"])

    assert expense == item
    assert [entry["SK"].removeprefix("HISTORY#") for entry in history] == [
        "2026-10-10T00:00:00.000Z#STATUS#DRAFT",
        "2026-10-10T00:05:00.000Z#RECEIPT",
        "2026-10-11T00:00:00.000Z#STATUS#PENDING_APPROVAL",
        "2026-10-12T00:00:00.000Z#STATUS#APPROVED",
    ]


def test_get_with_history_requires_the_expense(repo: ExpenseRepository, table: Any) -> None:
    table.put_item(Item=_history("01JA2B3C4D5E6F7G8H9JKMNPQR", "2026-10-10T00:00:00.000Z", "DRAFT"))

    with pytest.raises(NotFoundError):
        repo.get_with_history("01JA2B3C4D5E6F7G8H9JKMNPQR")


# --- list_by_owner ---------------------------------------------------------------------------------------------------


def test_list_by_owner_is_newest_first_with_cursors(repo: ExpenseRepository) -> None:
    created = [repo.create(EMPLOYEE, {"title": f"Expense {n}"})["id"] for n in range(5)]
    repo.create(MANAGER, {"title": "Not mine"})

    pages = _all_pages(lambda **kwargs: repo.list_by_owner(EMPLOYEE_ID, **kwargs), limit=2)

    newest_first = created[::-1]
    assert pages == [newest_first[0:2], newest_first[2:4], newest_first[4:5]]


def test_list_by_owner_default_page(repo: ExpenseRepository) -> None:
    repo.create(EMPLOYEE, {"title": "Only one"})

    page = repo.list_by_owner(EMPLOYEE_ID)

    assert [item["title"] for item in page.items] == ["Only one"]
    assert page.next_cursor is None


def test_status_filter_fills_pages_across_query_rounds(repo: ExpenseRepository, table: Any) -> None:
    # Newest first: D P P D D D P D  (Limit counts items READ; the filter drops the P items afterwards.)
    statuses = ["DRAFT", "PENDING_APPROVAL", "PENDING_APPROVAL", "DRAFT", "DRAFT", "DRAFT", "PENDING_APPROVAL", "DRAFT"]
    created = [
        _put(table, status=status, createdAt=f"2026-10-{20 - n:02d}T00:00:00.000Z")["id"]
        for n, status in enumerate(statuses)
    ]
    drafts = [expense_id for expense_id, status in zip(created, statuses, strict=True) if status == "DRAFT"]

    pages = _all_pages(lambda **kwargs: repo.list_by_owner(EMPLOYEE_ID, status="DRAFT", **kwargs), limit=3)

    assert [expense_id for page in pages for expense_id in page] == drafts  # nothing skipped or repeated
    assert all(len(page) <= 3 for page in pages)
    assert pages[0] == drafts[:3]


def test_a_sparse_filter_returns_a_short_page_with_a_cursor(repo: ExpenseRepository, table: Any) -> None:
    for n in range(12):
        _put(table, status="APPROVED", createdAt=f"2026-10-{n + 1:02d}T00:00:00.000Z")
    oldest = _put(table, status="DRAFT", createdAt="2026-09-01T00:00:00.000Z")

    first = repo.list_by_owner(EMPLOYEE_ID, status="DRAFT", limit=2)

    # The work per request is bounded: 5 rounds x 2 items read, none matched - but more may follow.
    assert first.items == []
    assert first.next_cursor is not None
    second = repo.list_by_owner(EMPLOYEE_ID, status="DRAFT", limit=2, cursor=first.next_cursor)
    assert [item["id"] for item in second.items] == [oldest["id"]]


def test_list_by_owner_rejects_an_unknown_status(repo: ExpenseRepository) -> None:
    with pytest.raises(DomainValidationError, match="unknown status"):
        repo.list_by_owner(EMPLOYEE_ID, status="LOST")


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_list_limit_is_bounded(repo: ExpenseRepository, limit: int) -> None:
    with pytest.raises(DomainValidationError, match="limit"):
        repo.list_by_owner(EMPLOYEE_ID, limit=limit)


def test_a_cursor_of_another_owner_is_rejected(repo: ExpenseRepository) -> None:
    for n in range(3):
        repo.create(MANAGER, {"title": f"Manager expense {n}"})
    managers_cursor = repo.list_by_owner(MANAGER_ID, limit=1).next_cursor

    with pytest.raises(DomainValidationError, match="invalid cursor"):
        repo.list_by_owner(EMPLOYEE_ID, cursor=managers_cursor)


@pytest.mark.parametrize(
    "start_key",
    [
        {"PK": "EXPENSE#x", "SK": "META"},  # a table key, not a GSI1 key
        {"PK": "EXPENSE#x", "SK": "META", "GSI1PK": f"OWNER#{EMPLOYEE_ID}", "GSI1SK": 5},
        {"PK": "EXPENSE#x", "SK": "META", "GSI1PK": f"OWNER#{EMPLOYEE_ID}", "GSI1SK": "a", "extra": "b"},
    ],
)
def test_a_tampered_cursor_is_rejected(repo: ExpenseRepository, start_key: dict[str, Any]) -> None:
    with pytest.raises(DomainValidationError, match="invalid cursor"):
        repo.list_by_owner(EMPLOYEE_ID, cursor=encode_cursor(start_key))


def test_cursor_contains_the_index_and_table_keys(repo: ExpenseRepository) -> None:
    for n in range(2):
        repo.create(EMPLOYEE, {"title": f"Expense {n}"})

    cursor = repo.list_by_owner(EMPLOYEE_ID, limit=1).next_cursor

    assert set(decode_cursor(cursor) or {}) == {"PK", "SK", "GSI1PK", "GSI1SK"}


# --- list_pending_approval -------------------------------------------------------------------------------------------


def test_approval_queue_is_oldest_first_without_my_own(repo: ExpenseRepository, table: Any) -> None:
    submitted = {
        "mine": _put(table, ownerId=MANAGER_ID, status="PENDING_APPROVAL", updatedAt="2026-10-11T00:00:00.000Z"),
        "second": _put(table, status="PENDING_APPROVAL", updatedAt="2026-10-12T00:00:00.000Z"),
        "first": _put(table, ownerId=ADMIN_ID, status="PENDING_APPROVAL", updatedAt="2026-10-10T00:00:00.000Z"),
        "third": _put(table, status="PENDING_APPROVAL", updatedAt="2026-10-13T00:00:00.000Z"),
    }
    _put(table, status="DRAFT", updatedAt="2026-10-09T00:00:00.000Z")
    _put(table, status="APPROVED", updatedAt="2026-10-09T00:00:00.000Z")

    pages = _all_pages(lambda **kwargs: repo.list_pending_approval(exclude_owner_id=MANAGER_ID, **kwargs), limit=2)

    assert pages == [[submitted["first"]["id"], submitted["second"]["id"]], [submitted["third"]["id"]]]


def test_approval_queue_without_exclusion(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table, ownerId=MANAGER_ID, status="PENDING_APPROVAL")

    assert [entry["id"] for entry in repo.list_pending_approval().items] == [item["id"]]


def test_approval_queue_rejects_a_cursor_of_another_index(repo: ExpenseRepository) -> None:
    for n in range(2):
        repo.create(EMPLOYEE, {"title": f"Expense {n}"})
    gsi1_cursor = repo.list_by_owner(EMPLOYEE_ID, limit=1).next_cursor

    with pytest.raises(DomainValidationError, match="invalid cursor"):
        repo.list_pending_approval(cursor=gsi1_cursor)


# --- reports and totals ----------------------------------------------------------------------------------------------


def test_iter_approved_in_month(repo: ExpenseRepository, table: Any) -> None:
    october = [
        _put(table, status="APPROVED", updatedAt="2026-10-31T23:59:59.999Z"),
        _put(table, status="APPROVED", updatedAt="2026-10-01T00:00:00.000Z"),
    ]
    _put(table, status="APPROVED", updatedAt="2026-09-30T23:59:59.999Z")
    _put(table, status="APPROVED", updatedAt="2026-11-01T00:00:00.000Z")
    _put(table, status="PENDING_APPROVAL", updatedAt="2026-10-15T00:00:00.000Z")

    rows = list(repo.iter_approved_in_month("2026-10"))

    assert [row["id"] for row in rows] == [october[1]["id"], october[0]["id"]]


def _total(pk: str, month: str, currency: str, amount: str, count: int) -> dict[str, Any]:
    return {
        keys.PK: pk,
        keys.SK: keys.total_sk(month, currency),
        "entityType": "MonthlyTotal",
        "month": month,
        "currency": currency,
        "approvedAmount": Decimal(amount),
        "approvedCount": count,
    }


def test_get_totals_for_a_user_and_the_org(repo: ExpenseRepository, table: Any) -> None:
    mine = keys.user_total_pk(EMPLOYEE_ID)
    for total in (
        _total(mine, "2026-10", "JPY", "5000", 2),
        _total(mine, "2026-02", "USD", "12.5", 1),
        _total(mine, "2026-02", "JPY", "800", 1),
        _total(mine, "2025-12", "JPY", "999", 1),
        _total(keys.user_total_pk(MANAGER_ID), "2026-10", "JPY", "7", 1),
        _total(keys.ORG_PK, "2026-10", "JPY", "5007", 3),
    ):
        table.put_item(Item=total)

    own = repo.get_totals(EMPLOYEE_ID, 2026)
    org = repo.get_totals(EMPLOYEE_ID, 2026, org=True)

    assert [item[keys.SK] for item in own] == ["TOTAL#2026-02#JPY", "TOTAL#2026-02#USD", "TOTAL#2026-10#JPY"]
    assert [(item[keys.SK], item["approvedAmount"]) for item in org] == [("TOTAL#2026-10#JPY", Decimal("5007"))]
    assert repo.get_totals(EMPLOYEE_ID, 2024) == []


# --- update_fields ---------------------------------------------------------------------------------------------------


def test_update_sets_and_removes_fields(repo: ExpenseRepository, table: Any, clock: FakeClock) -> None:
    created = repo.create(EMPLOYEE, {"title": "Taxi", "merchant": "Old", "description": "To remove"})
    clock.advance(timedelta(minutes=5))

    updated = repo.update_fields(
        created["id"],
        EMPLOYEE,
        updates={"merchant": "Nihon Kotsu", "amount": "99.50"},
        removals=["description"],
        expected_version=1,
    )

    assert updated["merchant"] == "Nihon Kotsu"
    assert updated["amount"] == Decimal("99.50")
    assert "description" not in updated
    assert updated["version"] == 2
    assert updated["updatedAt"] > created["updatedAt"]
    assert updated["createdAt"] == created["createdAt"]
    assert updated[keys.GSI1SK] == created[keys.GSI1SK]
    _assert_touched(updated, "DRAFT", EMPLOYEE)
    assert _stored(table, created["id"]) == updated


def test_update_without_version_skips_the_lock(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table, version=Decimal(7))

    assert repo.update_fields(item["id"], EMPLOYEE, updates={"title": "Renamed"})["version"] == 8


def test_editing_a_rejected_expense_reopens_it_as_a_clean_draft(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table, status="REJECTED", **DECISION)

    updated = repo.update_fields(item["id"], EMPLOYEE, updates={"description": "Added a better receipt"})

    _assert_touched(updated, "DRAFT", EMPLOYEE)
    assert not set(DECISION) & set(updated)
    assert updated["description"] == "Added a better receipt"


def test_update_by_someone_else_is_forbidden(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table)

    with pytest.raises(ForbiddenError, match="only the owner can edit") as raised:
        repo.update_fields(item["id"], MANAGER, updates={"title": "Mine now"})

    assert raised.value.http_status == 403
    assert _stored(table, item["id"]) == item


def test_ownership_is_checked_before_status(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table, status="APPROVED")

    with pytest.raises(ForbiddenError):
        repo.update_fields(item["id"], ADMIN, updates={"title": "x"})


def test_update_of_a_missing_expense_does_not_create_one(repo: ExpenseRepository, table: Any) -> None:
    with pytest.raises(NotFoundError):
        repo.update_fields("01JA2B3C4D5E6F7G8H9JKMNPQR", EMPLOYEE, updates={"title": "Ghost"})

    assert _stored(table, "01JA2B3C4D5E6F7G8H9JKMNPQR") is None  # UpdateItem is an upsert without the condition


@pytest.mark.parametrize("status", ["PENDING_APPROVAL", "APPROVED"])
def test_update_in_a_locked_status_conflicts(repo: ExpenseRepository, table: Any, status: str) -> None:
    item = _put(table, status=status)

    with pytest.raises(ConflictError, match=f"cannot edit an expense in status {status}") as raised:
        repo.update_fields(item["id"], EMPLOYEE, updates={"title": "Too late"})

    assert raised.value.http_status == 409
    assert "DRAFT or REJECTED" in raised.value.message


def test_update_with_a_stale_version_conflicts(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table, version=Decimal(3))

    with pytest.raises(ConflictError, match=r"changed by someone else \(version 3, you sent 2\)"):
        repo.update_fields(item["id"], EMPLOYEE, updates={"title": "Stale"}, expected_version=2)

    assert _stored(table, item["id"]) == item


@pytest.mark.parametrize(
    ("updates", "removals", "message"),
    [
        ({}, ["title"], "not removable"),
        ({}, ["currency"], "not removable"),
        ({}, ["status"], "not removable"),
        ({"ownerId": "me"}, [], "not editable"),
        ({"merchant": "x"}, ["merchant"], "both updated and removed"),
        ({"merchant": ""}, [], "blank values"),
    ],
)
def test_update_rejects_programming_errors(
    repo: ExpenseRepository, table: Any, updates: dict[str, Any], removals: list[str], message: str
) -> None:
    item = _put(table)

    with pytest.raises(ValueError, match=message):
        repo.update_fields(item["id"], EMPLOYEE, updates=updates, removals=removals)


# --- set_pending_receipt ---------------------------------------------------------------------------------------------


def test_set_pending_receipt_is_bookkeeping_only(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table)
    key = keys.receipt_object_key(EMPLOYEE_ID, item["id"], "01JA2B3C4D5E6F7G8H9JKMNPQS", ".png")

    updated = repo.set_pending_receipt(item["id"], EMPLOYEE, key=key, content_type="image/png")

    assert updated == {**item, "pendingReceiptKey": key, "pendingReceiptContentType": "image/png"}
    assert updated["version"] == item["version"]  # the client's optimistic-locking version stays valid


@pytest.mark.parametrize(
    ("overrides", "caller", "error"),
    [
        ({"status": "PENDING_APPROVAL"}, EMPLOYEE, ConflictError),
        ({"status": "REJECTED"}, EMPLOYEE, ConflictError),
        ({}, MANAGER, ForbiddenError),
    ],
)
def test_set_pending_receipt_failures(
    repo: ExpenseRepository, table: Any, overrides: dict[str, Any], caller: Caller, error: type[Exception]
) -> None:
    item = _put(table, **overrides)

    with pytest.raises(error):
        repo.set_pending_receipt(item["id"], caller, key="receipts/x/y/z.png", content_type="image/png")

    assert _stored(table, item["id"]) == item


def test_set_pending_receipt_on_a_missing_expense(repo: ExpenseRepository) -> None:
    with pytest.raises(NotFoundError):
        repo.set_pending_receipt("01JA2B3C4D5E6F7G8H9JKMNPQR", EMPLOYEE, key="k", content_type="image/png")


# --- submit ----------------------------------------------------------------------------------------------------------


def test_submit_moves_a_complete_draft_into_the_approval_queue(repo: ExpenseRepository, table: Any) -> None:
    item = _put(
        table,
        workflowExecutionArn="arn:aws:states:us-east-1:123456789012:execution:ApprovalWorkflow:old",
        approvalTaskToken="stale-token",
        **DECISION,
    )

    submitted = repo.submit(item["id"], EMPLOYEE)

    _assert_touched(submitted, "PENDING_APPROVAL", EMPLOYEE)
    assert submitted["submittedAt"] == submitted["updatedAt"]
    assert submitted["version"] == 2
    stale = {"workflowExecutionArn", "approvalTaskToken", *DECISION}
    assert not stale & set(submitted), "a new approval round never shows the previous one"
    assert _stored(table, item["id"]) == submitted
    queue = repo.list_pending_approval()
    assert [entry["id"] for entry in queue.items] == [item["id"]]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"merchant": None, "category": None}, "missing or invalid merchant, category"),
        ({"amount": None}, "missing or invalid amount"),
        ({"amount": Decimal(0)}, "missing or invalid amount"),
        ({"expenseDate": None}, "missing or invalid expenseDate"),
    ],
)
def test_submit_requires_every_field(
    repo: ExpenseRepository, table: Any, overrides: dict[str, Any], message: str
) -> None:
    item = _put(table, **overrides)

    with pytest.raises(DomainValidationError, match=message) as raised:
        repo.submit(item["id"], EMPLOYEE)

    assert raised.value.http_status == 400
    assert _stored(table, item["id"]) == item


@pytest.mark.parametrize(
    ("overrides", "caller", "error", "message"),
    [
        ({"status": "PENDING_APPROVAL"}, EMPLOYEE, ConflictError, "cannot submit an expense in status PENDING"),
        ({"status": "REJECTED"}, EMPLOYEE, ConflictError, "status REJECTED"),
        ({}, MANAGER, ForbiddenError, "only the owner can submit"),
        ({"merchant": None}, MANAGER, ForbiddenError, "only the owner"),  # 403 wins over 400
    ],
)
def test_submit_failures(
    repo: ExpenseRepository,
    table: Any,
    overrides: dict[str, Any],
    caller: Caller,
    error: type[Exception],
    message: str,
) -> None:
    item = _put(table, **overrides)

    with pytest.raises(error, match=message):
        repo.submit(item["id"], caller)


def test_submit_a_missing_expense(repo: ExpenseRepository) -> None:
    with pytest.raises(NotFoundError):
        repo.submit("01JA2B3C4D5E6F7G8H9JKMNPQR", EMPLOYEE)


# --- withdraw --------------------------------------------------------------------------------------------------------


def test_withdraw_returns_the_draft_and_the_execution_to_stop(repo: ExpenseRepository, table: Any) -> None:
    arn = "arn:aws:states:us-east-1:123456789012:execution:ApprovalWorkflow:e1"
    item = _put(
        table,
        status="PENDING_APPROVAL",
        version=Decimal(4),
        submittedAt="2026-10-11T00:00:00.000Z",
        workflowExecutionArn=arn,
        approvalTaskToken="token",
        approvalRequestedAt="2026-10-11T00:00:01.000Z",
        escalated=True,
    )

    withdrawal = repo.withdraw(item["id"], EMPLOYEE)

    assert withdrawal.workflow_execution_arn == arn
    expense = withdrawal.expense
    _assert_touched(expense, "DRAFT", EMPLOYEE)
    assert expense["version"] == 5
    assert not {"workflowExecutionArn", "approvalTaskToken", "escalated", "approvalRequestedAt"} & set(expense)
    assert expense["submittedAt"] == "2026-10-11T00:00:00.000Z"
    assert _stored(table, item["id"]) == expense  # the derived item is exactly what DynamoDB stored


def test_withdraw_before_the_workflow_claimed_it(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table, status="PENDING_APPROVAL")

    assert repo.withdraw(item["id"], EMPLOYEE).workflow_execution_arn is None


@pytest.mark.parametrize(
    ("overrides", "caller", "error"),
    [
        ({"status": "DRAFT"}, EMPLOYEE, ConflictError),
        ({"status": "APPROVED"}, EMPLOYEE, ConflictError),
        ({"status": "PENDING_APPROVAL"}, MANAGER, ForbiddenError),
    ],
)
def test_withdraw_failures(
    repo: ExpenseRepository, table: Any, overrides: dict[str, Any], caller: Caller, error: type[Exception]
) -> None:
    item = _put(table, **overrides)

    with pytest.raises(error):
        repo.withdraw(item["id"], caller)

    assert _stored(table, item["id"]) == item


def test_withdraw_a_missing_expense(repo: ExpenseRepository) -> None:
    with pytest.raises(NotFoundError):
        repo.withdraw("01JA2B3C4D5E6F7G8H9JKMNPQR", EMPLOYEE)


# --- delete_draft ----------------------------------------------------------------------------------------------------


def _collection(table: Any, expense_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = table.query(
        KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": keys.expense_pk(expense_id)}
    )["Items"]
    return items


def test_delete_draft_removes_the_whole_collection(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table, receiptKey="receipts/u/e/a.jpg", pendingReceiptKey="receipts/u/e/b.png")
    for minute in range(30):  # more than one BatchWriteItem (25 requests) worth
        table.put_item(Item=_history(item["id"], f"2026-10-10T00:{minute:02d}:00.000Z", "DRAFT"))
    other = _put(table)

    receipt_keys = repo.delete_draft(item["id"], EMPLOYEE)

    assert receipt_keys == ["receipts/u/e/a.jpg", "receipts/u/e/b.png"]
    assert _collection(table, item["id"]) == []
    assert _stored(table, other["id"]) == other


def test_delete_draft_without_receipts(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table)

    assert repo.delete_draft(item["id"], EMPLOYEE) == []


def test_delete_draft_reports_a_shared_key_once(repo: ExpenseRepository, table: Any) -> None:
    item = _put(table, receiptKey="receipts/u/e/a.jpg", pendingReceiptKey="receipts/u/e/a.jpg")

    assert repo.delete_draft(item["id"], EMPLOYEE) == ["receipts/u/e/a.jpg"]


@pytest.mark.parametrize(
    ("overrides", "caller", "error"),
    [
        ({"status": "PENDING_APPROVAL"}, EMPLOYEE, ConflictError),
        ({"status": "REJECTED"}, EMPLOYEE, ConflictError),
        ({}, MANAGER, ForbiddenError),
    ],
)
def test_delete_draft_failures_delete_nothing(
    repo: ExpenseRepository, table: Any, overrides: dict[str, Any], caller: Caller, error: type[Exception]
) -> None:
    item = _put(table, **overrides)
    table.put_item(Item=_history(item["id"], "2026-10-10T00:00:00.000Z", "DRAFT"))

    with pytest.raises(error):
        repo.delete_draft(item["id"], caller)

    assert len(_collection(table, item["id"])) == 2


def test_delete_a_missing_draft(repo: ExpenseRepository) -> None:
    with pytest.raises(NotFoundError):
        repo.delete_draft("01JA2B3C4D5E6F7G8H9JKMNPQR", EMPLOYEE)


# --- infrastructure errors ----------------------------------------------------------------------------------------


def test_errors_other_than_failed_conditions_pass_through(clock: FakeClock) -> None:
    repo = ExpenseRepository()  # no `table` fixture: the table does not exist

    with pytest.raises(ClientError) as raised:
        repo.submit("01JA2B3C4D5E6F7G8H9JKMNPQR", EMPLOYEE)

    assert raised.value.response["Error"]["Code"] == "ResourceNotFoundException"


def test_an_explicit_table_can_be_injected(table: Any) -> None:
    item = _put(table)

    assert ExpenseRepository(table=table).get(item["id"]) == item
