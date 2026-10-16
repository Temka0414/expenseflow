"""The audit trail (HISTORY items) and the monthly approved totals, written by the stream processor (SPEC 3, 7.1).

Nobody writes these from the API. The API only changes the expense's META item; DynamoDB Streams then delivers
the before/after images of that change to the stream processor, which records it here. This "derived data from
the stream" pattern keeps the API write a single conditional update, while the audit trail can never be skipped.

**Exactly-once on top of an at-least-once stream.** A stream record can be delivered more than once (Lambda retries
a failed batch, bisects it, or re-reads from the last checkpoint), so every write here must be safe to repeat:

* A HISTORY sort key identifies the state change itself - ``HISTORY#{updatedAt}#STATUS#{to}`` or
  ``HISTORY#{extractedAt}#RECEIPT`` - and is written with ``attribute_not_exists(SK)``. A replayed record hits
  the existing item, the condition fails, and we treat that as "already done".
* Totals use ``ADD``, which is *not* idempotent: replaying it would count an approval twice. So the totals are
  updated in ONE ``TransactWriteItems`` together with the guarded HISTORY put. Transactions are all-or-nothing:
  when the HISTORY item already exists, the whole transaction is cancelled and the totals stay untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

from botocore.exceptions import ClientError

from expenseflow import aws, ids, keys
from expenseflow.models import ExpenseStatus, HistoryKind

type Item = dict[str, Any]

# Actor recorded for changes made by the workflows rather than by a person (SPEC 3: `system`/`system`).
SYSTEM_ACTOR: Final = "system"

_CREATE_ONLY: Final = f"attribute_not_exists({keys.SK})"
_DECIDED: Final = frozenset({ExpenseStatus.APPROVED, ExpenseStatus.REJECTED})
# Several of these names are DynamoDB reserved words (e.g. MONTH), so the update uses #placeholders throughout.
_ADD_TO_TOTAL: Final = (
    "SET #entityType = :entityType, #month = :month, #currency = :currency "
    "ADD #approvedAmount :amount, #approvedCount :one"
)
_TOTAL_NAMES: Final = {
    f"#{name}": name for name in ("entityType", "month", "currency", "approvedAmount", "approvedCount")
}


def status_entry(new_image: Mapping[str, Any], old_image: Mapping[str, Any]) -> Item:
    """The HISTORY item for a status change (``old_image`` is empty for a newly created expense)."""
    expense_id = new_image["id"]
    at = new_image["updatedAt"]
    to_status = new_image["status"]
    return _present(
        {
            keys.PK: keys.expense_pk(expense_id),
            keys.SK: keys.history_sk_status(at, to_status),
            "entityType": keys.ENTITY_HISTORY,
            "expenseId": expense_id,
            "kind": HistoryKind.STATUS.value,
            "at": at,
            "from": old_image.get("status"),
            "to": to_status,
            "actorId": new_image.get("lastActorId"),
            "actorEmail": new_image.get("lastActorEmail"),
            "comment": new_image.get("decisionComment") if to_status in _DECIDED else None,
        }
    )


def receipt_entry(new_image: Mapping[str, Any]) -> Item:
    """The HISTORY item for a finished receipt extraction (SUCCEEDED, FAILED or SKIPPED) by the ReceiptPipeline."""
    expense_id = new_image["id"]
    at = new_image["extractedAt"]
    return _present(
        {
            keys.PK: keys.expense_pk(expense_id),
            keys.SK: keys.history_sk_receipt(at),
            "entityType": keys.ENTITY_HISTORY,
            "expenseId": expense_id,
            "kind": HistoryKind.RECEIPT.value,
            "at": at,
            "extractionStatus": new_image["extractionStatus"],
            "actorId": SYSTEM_ACTOR,
            "actorEmail": SYSTEM_ACTOR,
            "comment": new_image.get("extractionError"),  # only present when the extraction FAILED
        }
    )


def record_status_change(new_image: Mapping[str, Any], old_image: Mapping[str, Any]) -> bool:
    """Write the HISTORY item for a status change (+ the monthly totals for an approval).

    Returns ``False`` when this change had already been recorded (a replayed stream record).
    """
    entry = status_entry(new_image, old_image)
    if new_image["status"] == ExpenseStatus.APPROVED:
        return _put_with_totals(entry, new_image)
    return _put_once(entry)


def record_receipt_processed(new_image: Mapping[str, Any]) -> bool:
    """Write the HISTORY item for a finished extraction; ``False`` when it already existed."""
    return _put_once(receipt_entry(new_image))


def _put_once(entry: Item) -> bool:
    try:
        aws.table().put_item(Item=entry, ConditionExpression=_CREATE_ONLY)
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _put_with_totals(entry: Item, expense: Mapping[str, Any]) -> bool:
    """HISTORY put + ADD to the owner's and the organisation's monthly total, all or nothing.

    The month is that of ``decidedAt`` (UTC, like the GSI2 "approved in month" query), so ``GET /stats`` and the
    monthly CSV report agree. The resource's client (``table.meta.client``) accepts plain Python values such as
    ``Decimal`` in ``TransactItems``, exactly like ``table.put_item`` does.
    """
    table = aws.table()
    month = ids.month_of(expense.get("decidedAt") or expense["updatedAt"])
    currency, amount = expense["currency"], expense["amount"]
    transact_items = [
        {"Put": {"TableName": table.name, "Item": entry, "ConditionExpression": _CREATE_ONLY}},
        _add_to_total(table.name, keys.user_total_pk(expense["ownerId"]), month, currency, amount),
        _add_to_total(table.name, keys.ORG_PK, month, currency, amount),
    ]
    try:
        table.meta.client.transact_write_items(TransactItems=transact_items)
    except ClientError as error:
        if _history_already_exists(error):
            return False
        raise
    return True


def _add_to_total(table_name: str, pk: str, month: str, currency: str, amount: Decimal) -> dict[str, Any]:
    return {
        "Update": {
            "TableName": table_name,
            "Key": {keys.PK: pk, keys.SK: keys.total_sk(month, currency)},
            "UpdateExpression": _ADD_TO_TOTAL,
            "ExpressionAttributeNames": _TOTAL_NAMES,
            "ExpressionAttributeValues": {
                ":entityType": keys.ENTITY_MONTHLY_TOTAL,
                ":month": month,
                ":currency": currency,
                ":amount": amount,
                ":one": 1,
            },
        }
    }


def _history_already_exists(error: ClientError) -> bool:
    """A cancelled transaction lists one reason per item, in request order; item 0 is the guarded HISTORY put.

    Any other reason (e.g. ``TransactionConflict`` with a concurrent writer, throttling) is NOT "already done":
    the caller re-raises and the stream retries the record.
    """
    if error.response["Error"]["Code"] != "TransactionCanceledException":
        return False
    reasons = error.response.get("CancellationReasons") or [{}]
    first_reason: Mapping[str, Any] = reasons[0]
    return first_reason.get("Code") == "ConditionalCheckFailed"


def _present(item: Mapping[str, Any]) -> Item:
    """Drop unknown (``None``) attributes: optional attributes are absent, never null (SPEC 3)."""
    return {name: value for name, value in item.items() if value is not None}
