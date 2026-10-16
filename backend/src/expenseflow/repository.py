"""Expense data access: every read and write the HTTP API performs on the single table (SPEC sections 3 and 5).

Each state transition is ONE conditional write. DynamoDB evaluates the ``ConditionExpression`` atomically with
the write, so "only the owner may submit, and only from DRAFT" holds even when two requests race - there is no
read-check-write window. Three details matter:

* **UpdateItem is an upsert.** Without a condition it would create a half-empty item for an unknown id. Every
  update here compares ``ownerId``; a missing item has no ``ownerId``, so the condition also guards existence.
* **Why did it fail?** With ``ReturnValuesOnConditionCheckFailure="ALL_OLD"`` the ``ConditionalCheckFailedException``
  carries the item as it was, so we can answer 404, 403, 409 or 400 precisely without a second read. (The item
  arrives in low-level wire format - ``{"S": "..."}`` - even through the boto3 resource.)
* **Optimistic locking.** Every write that changes ``updatedAt`` increments ``version``. A client that sends the
  version it last saw gets 409 instead of silently overwriting someone else's change.

Conditions are written as explicit expression strings (the same text you would type in the console or in a
Step Functions task) with ``#name`` placeholders, because many attribute names here (``status``, ``comment``,
``name``) are DynamoDB reserved words. Queries use boto3's ``Key``/``Attr`` builders, which escape names for us.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from expenseflow import aws, ids, keys, settings
from expenseflow.auth import Caller
from expenseflow.errors import ConflictError, DomainError, DomainValidationError, ForbiddenError, NotFoundError
from expenseflow.models import (
    DECISION_ATTRIBUTES,
    EDITABLE_ATTRIBUTES,
    REMOVABLE_ATTRIBUTES,
    REQUIRED_FOR_SUBMIT,
    ExpenseStatus,
    missing_required_fields,
)
from expenseflow.serialization import decode_cursor, encode_cursor, to_decimal

type Item = dict[str, Any]

DEFAULT_PAGE_SIZE: Final = 25
MAX_PAGE_SIZE: Final = 100
# A filtered query can need several round trips to fill a page (see `_paginate`); bound the work per request.
_MAX_QUERY_ROUNDS: Final = 5
_CONDITIONAL_CHECK_FAILED: Final = "ConditionalCheckFailedException"
# Everything describing the current approval round. Cleared when an expense is (re)submitted or goes back to DRAFT,
# so a new round never shows the previous approver, comment or task token.
_APPROVAL_ROUND_ATTRIBUTES: Final = (*DECISION_ATTRIBUTES, "approvalRequestedAt")
_SUBMIT_CLEARS: Final = ("workflowExecutionArn", "approvalTaskToken", *_APPROVAL_ROUND_ATTRIBUTES)
_WITHDRAW_CLEARS: Final = ("approvalTaskToken", "workflowExecutionArn", "escalated", "approvalRequestedAt")
_RECEIPT_KEY_ATTRIBUTES: Final = ("receiptKey", "pendingReceiptKey")


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a listing; ``next_cursor`` is ``None`` on the last page."""

    items: list[Item]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class Withdrawal:
    """Result of :meth:`ExpenseRepository.withdraw`: the updated expense and the approval workflow to stop."""

    expense: Item
    workflow_execution_arn: str | None


class ExpenseRepository:
    """Expenses and their item collections (META + HISTORY), monthly totals and the approval queue.

    Methods return items as stored (``Decimal`` numbers); map them with ``models.to_api`` & co. for responses.
    Failed business rules raise :mod:`expenseflow.errors` exceptions, which the API turns into HTTP statuses.
    """

    def __init__(self, table: Any | None = None) -> None:
        # Resolved lazily (aws.table() is cached) so a module-level repository creates no client at import time.
        self._table = table

    @property
    def table(self) -> Any:
        return self._table if self._table is not None else aws.table()

    # --- Reads -----------------------------------------------------------------------------------------------

    def get(self, expense_id: str) -> Item:
        """The expense (META item); :class:`NotFoundError` when it does not exist.

        ``ConsistentRead`` (twice the read cost, base table only) guarantees the response reflects every write
        that was acknowledged before - e.g. the GET the SPA sends right after a PATCH.
        """
        item = self.table.get_item(Key=keys.expense_key(expense_id), ConsistentRead=True).get("Item")
        if item is None:
            raise NotFoundError(f"expense {expense_id} not found")
        return item

    def get_with_history(self, expense_id: str) -> tuple[Item, list[Item]]:
        """The expense and its HISTORY entries (oldest first), read with ONE query on the item collection."""
        items = self._query_all(
            KeyConditionExpression=Key(keys.PK).eq(keys.expense_pk(expense_id)),
            ConsistentRead=True,
        )
        expense: Item | None = None
        history: list[Item] = []
        for item in items:
            if item[keys.SK] == keys.META:
                expense = item
            elif item[keys.SK].startswith(keys.HISTORY_PREFIX):
                history.append(item)
        if expense is None:
            raise NotFoundError(f"expense {expense_id} not found")
        return expense, history  # sort keys start with the timestamp, so the query already returned them in order

    def list_by_owner(
        self,
        owner_id: str,
        *,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> Page:
        """The owner's expenses, newest first (GSI1), optionally only those in ``status``."""
        partition = keys.owner_gsi1pk(owner_id)
        return self._paginate(
            index=keys.GSI1,
            key_condition=Key(keys.GSI1PK).eq(partition),
            filter_condition=None if status is None else Attr("status").eq(_status(status)),
            forward=False,
            limit=limit,
            start_key=_start_key(cursor, keys.GSI1_KEY_ATTRIBUTES, keys.GSI1PK, partition),
            key_attributes=keys.GSI1_KEY_ATTRIBUTES,
        )

    def list_pending_approval(
        self,
        *,
        exclude_owner_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> Page:
        """The approval queue, oldest submission first (GSI2), without the caller's own expenses."""
        partition = keys.status_gsi2pk(ExpenseStatus.PENDING_APPROVAL)
        return self._paginate(
            index=keys.GSI2,
            key_condition=Key(keys.GSI2PK).eq(partition),
            filter_condition=None if exclude_owner_id is None else Attr("ownerId").ne(exclude_owner_id),
            forward=True,
            limit=limit,
            start_key=_start_key(cursor, keys.GSI2_KEY_ATTRIBUTES, keys.GSI2PK, partition),
            key_attributes=keys.GSI2_KEY_ATTRIBUTES,
        )

    def iter_approved_in_month(self, month: str) -> Iterator[Item]:
        """Every expense approved in ``month`` (``YYYY-MM``, UTC), oldest first - the monthly report's rows.

        APPROVED is terminal, so the GSI2 sort key ``{updatedAt}#{id}`` of an approved expense is its approval time.
        """
        return self._query_all(
            IndexName=keys.GSI2,
            KeyConditionExpression=Key(keys.GSI2PK).eq(keys.status_gsi2pk(ExpenseStatus.APPROVED))
            & Key(keys.GSI2SK).begins_with(keys.status_gsi2sk_month_prefix(month)),
        )

    def get_totals(self, owner_id: str, year: int, *, org: bool = False) -> list[Item]:
        """Monthly approved totals of ``year`` (one item per month and currency, chronological).

        ``org=True`` reads the organisation-wide partition (``ORG``) instead of the owner's (``USER#{ownerId}``).
        Map items with ``models.total_to_api``.
        """
        partition = keys.ORG_PK if org else keys.user_total_pk(owner_id)
        return list(
            self._query_all(
                KeyConditionExpression=Key(keys.PK).eq(partition)
                & Key(keys.SK).begins_with(keys.total_sk_year_prefix(year)),
            )
        )

    # --- Writes ----------------------------------------------------------------------------------------------

    def create(self, caller: Caller, attributes: Mapping[str, Any]) -> Item:
        """Create a DRAFT owned by ``caller`` from editable ``attributes`` (``title`` required; currency defaults)."""
        values = _clean_attributes(attributes)
        if "title" not in values:
            raise DomainValidationError("title is required")
        expense_id = ids.new_id()
        now = ids.now_iso()
        item: Item = {
            **keys.expense_key(expense_id),
            keys.GSI1PK: keys.owner_gsi1pk(caller.user_id),
            keys.GSI1SK: keys.owner_gsi1sk(now, expense_id),
            keys.GSI2PK: keys.status_gsi2pk(ExpenseStatus.DRAFT),
            keys.GSI2SK: keys.status_gsi2sk(now, expense_id),
            "entityType": keys.ENTITY_EXPENSE,
            "id": expense_id,
            "ownerId": caller.user_id,
            "ownerEmail": caller.email,
            "ownerName": caller.name,
            "currency": settings.default_currency(),
            **values,
            "status": ExpenseStatus.DRAFT.value,
            "createdAt": now,
            "updatedAt": now,
            "version": 1,
            "lastActorId": caller.user_id,
            "lastActorEmail": caller.email,
        }
        # Never overwrite blindly: PutItem replaces an existing item with the same key. ULIDs do not collide in
        # practice, but the condition makes "create" mean create.
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        return item

    def update_fields(
        self,
        expense_id: str,
        caller: Caller,
        *,
        updates: Mapping[str, Any],
        removals: Collection[str] = (),
        expected_version: int | None = None,
    ) -> Item:
        """PATCH: set ``updates``, remove ``removals``; DRAFT stays DRAFT and REJECTED is re-opened as DRAFT.

        Only the owner may edit. With ``expected_version`` the write also requires ``version = expected_version``.
        Re-opening a rejected expense clears the previous decision (approver, comment, decidedAt, ...).
        """
        values = _clean_attributes(updates)
        _check_names(removals, REMOVABLE_ATTRIBUTES, "removable")
        if overlap := values.keys() & set(removals):
            raise ValueError(f"attributes both updated and removed: {sorted(overlap)}")
        now = ids.now_iso()
        return self._update(
            expense_id,
            caller,
            action="edit",
            allowed=(ExpenseStatus.DRAFT, ExpenseStatus.REJECTED),
            expected_version=expected_version,
            sets={**values, **_touch(expense_id, caller, now, ExpenseStatus.DRAFT)},
            removes=(*sorted(set(removals)), *_APPROVAL_ROUND_ATTRIBUTES),
        )

    def set_pending_receipt(self, expense_id: str, caller: Caller, *, key: str, content_type: str) -> Item:
        """Remember the S3 key the browser is about to upload to (owner, DRAFT only).

        The ReceiptPipeline only accepts an upload whose key matches ``pendingReceiptKey``, so objects uploaded
        with an old or forged presigned policy are ignored. This is upload bookkeeping, not an edit of the
        expense: ``updatedAt``/``version`` stay unchanged, so the client's optimistic-locking version stays valid.
        """
        return self._update(
            expense_id,
            caller,
            action="upload a receipt for",
            allowed=(ExpenseStatus.DRAFT,),
            sets={"pendingReceiptKey": key, "pendingReceiptContentType": content_type},
            bump_version=False,
        )

    def submit(self, expense_id: str, caller: Caller) -> Item:
        """DRAFT -> PENDING_APPROVAL (owner only, every required field present and amount > 0).

        The required-field rule is part of the condition, so it is checked against the stored item atomically:
        a concurrent PATCH that removes the merchant cannot slip in between "validate" and "submit".
        The DynamoDB stream then turns this change into the ``Expense Submitted`` event (transactional outbox).
        """
        now = ids.now_iso()
        return self._update(
            expense_id,
            caller,
            action="submit",
            allowed=(ExpenseStatus.DRAFT,),
            sets={"submittedAt": now, **_touch(expense_id, caller, now, ExpenseStatus.PENDING_APPROVAL)},
            removes=_SUBMIT_CLEARS,
            extra_condition=_required_fields_condition,
            extra_diagnosis=_missing_fields_error,
        )

    def withdraw(self, expense_id: str, caller: Caller) -> Withdrawal:
        """PENDING_APPROVAL -> DRAFT (owner only); returns the expense and the workflow execution to stop.

        ``ReturnValues="ALL_OLD"`` hands back the item *before* the update, which still holds the
        ``workflowExecutionArn`` the update removes. The new item is derived from it: the update is fully
        determined (constant SETs, REMOVEs and ``version + 1``), so no second read is needed.
        """
        now = ids.now_iso()
        sets = _touch(expense_id, caller, now, ExpenseStatus.DRAFT)
        old = self._update(
            expense_id,
            caller,
            action="withdraw",
            allowed=(ExpenseStatus.PENDING_APPROVAL,),
            sets=sets,
            removes=_WITHDRAW_CLEARS,
            return_values="ALL_OLD",
        )
        expense = {name: value for name, value in old.items() if name not in _WITHDRAW_CLEARS}
        expense.update(sets, version=old["version"] + 1)
        return Withdrawal(expense=expense, workflow_execution_arn=old.get("workflowExecutionArn"))

    def delete_draft(self, expense_id: str, caller: Caller) -> list[str]:
        """Delete a DRAFT and its whole item collection (owner only); returns the receipt S3 keys to delete.

        The META item is deleted first, conditionally - it is the gate. Only then are the HISTORY items removed
        with ``batch_writer``, which groups deletes into BatchWriteItem calls of 25 and re-sends unprocessed items.
        """
        expression = _Expression()
        condition = _guard(expression, caller.user_id, (ExpenseStatus.DRAFT,))
        try:
            response = self.table.delete_item(
                Key=keys.expense_key(expense_id),
                ConditionExpression=condition,
                ReturnValues="ALL_OLD",
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
                **expression.parameters(),
            )
        except ClientError as error:
            raise _failure(error, expense_id, caller, "delete", (ExpenseStatus.DRAFT,)) from None
        old: Item = response["Attributes"]
        remaining = self._query_all(KeyConditionExpression=Key(keys.PK).eq(keys.expense_pk(expense_id)))
        with self.table.batch_writer() as batch:
            for item in remaining:
                batch.delete_item(Key={keys.PK: item[keys.PK], keys.SK: item[keys.SK]})
        return list(dict.fromkeys(old[name] for name in _RECEIPT_KEY_ATTRIBUTES if old.get(name)))

    # --- Internals -------------------------------------------------------------------------------------------

    def _update(
        self,
        expense_id: str,
        caller: Caller,
        *,
        action: str,
        allowed: Collection[ExpenseStatus],
        sets: Mapping[str, Any],
        removes: Iterable[str] = (),
        expected_version: int | None = None,
        bump_version: bool = True,
        extra_condition: Callable[[_Expression], str] | None = None,
        extra_diagnosis: Callable[[Item], DomainError | None] | None = None,
        return_values: str = "ALL_NEW",
    ) -> Item:
        """A conditional UpdateItem on the META item, owned by ``caller`` and in one of the ``allowed`` statuses."""
        expression = _Expression()
        update = expression.update(sets, removes, bump_version=bump_version)
        condition = _guard(expression, caller.user_id, allowed, expected_version)
        if extra_condition is not None:
            condition = f"{condition} AND {extra_condition(expression)}"
        try:
            response = self.table.update_item(
                Key=keys.expense_key(expense_id),
                UpdateExpression=update,
                ConditionExpression=condition,
                ReturnValues=return_values,
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
                **expression.parameters(),
            )
        except ClientError as error:
            raise _failure(error, expense_id, caller, action, allowed, expected_version, extra_diagnosis) from None
        attributes: Item = response["Attributes"]
        return attributes

    def _paginate(
        self,
        *,
        index: str,
        key_condition: ConditionBase,
        filter_condition: ConditionBase | None,
        forward: bool,
        limit: int,
        start_key: Item | None,
        key_attributes: tuple[str, ...],
    ) -> Page:
        """Fill one page from a GSI query, honouring ``limit`` even when a filter drops items.

        Gotcha: ``Limit`` caps the items DynamoDB *reads*; ``FilterExpression`` runs afterwards. A filtered query
        with ``Limit=25`` can return 3 items (or none) plus a ``LastEvaluatedKey``. We keep querying (a bounded
        number of rounds) until the page is full, and when the last round matched more than we need, the cursor
        is built from the last item we return - a page token is just the key of the last item seen.
        """
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise DomainValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        request: dict[str, Any] = {
            "IndexName": index,
            "KeyConditionExpression": key_condition,
            "ScanIndexForward": forward,
            "Limit": limit,
        }
        if filter_condition is not None:
            request["FilterExpression"] = filter_condition
        items: list[Item] = []
        last_key = start_key
        for _ in range(_MAX_QUERY_ROUNDS):
            if last_key is not None:
                request["ExclusiveStartKey"] = last_key
            response = self.table.query(**request)
            items.extend(response["Items"])
            last_key = response.get("LastEvaluatedKey")
            if last_key is None or len(items) >= limit:
                break
        if len(items) > limit:
            items = items[:limit]
            last_key = {name: items[-1][name] for name in key_attributes}
        return Page(items=items, next_cursor=encode_cursor(last_key))

    def _query_all(self, **request: Any) -> Iterator[Item]:
        """Yield every item of a query, following ``LastEvaluatedKey`` (a response holds at most 1 MB)."""
        while True:
            response = self.table.query(**request)
            yield from response["Items"]
            if "LastEvaluatedKey" not in response:
                return
            request["ExclusiveStartKey"] = response["LastEvaluatedKey"]


# --- Expression building ------------------------------------------------------------------------------------


class _Expression:
    """Collects ``ExpressionAttributeNames``/``Values`` while update and condition expressions are written.

    Every attribute name goes through a ``#name`` placeholder (reserved words such as ``status`` cannot appear
    literally) and every value through a ``:label`` placeholder. DynamoDB rejects placeholders that are defined
    but unused, so only what an expression actually references is collected.
    """

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._values: dict[str, Any] = {}

    def name(self, attribute: str) -> str:
        placeholder = f"#{attribute}"
        self._names[placeholder] = attribute
        return placeholder

    def value(self, label: str, value: Any) -> str:
        placeholder = f":{label}"
        self._values[placeholder] = value
        return placeholder

    def update(self, sets: Mapping[str, Any], removes: Iterable[str], *, bump_version: bool) -> str:
        """``SET a = :new_a, ..., version = version + :one REMOVE b, ...``."""
        clauses = [f"{self.name(name)} = {self.value(f'new_{name}', value)}" for name, value in sets.items()]
        if bump_version:
            version = self.name("version")
            clauses.append(f"{version} = {version} + {self.value('one', 1)}")
        text = "SET " + ", ".join(clauses)
        if removed := list(dict.fromkeys(removes)):
            text += " REMOVE " + ", ".join(self.name(name) for name in removed)
        return text

    def parameters(self) -> dict[str, Any]:
        parameters: dict[str, Any] = {"ExpressionAttributeNames": self._names}
        if self._values:
            parameters["ExpressionAttributeValues"] = self._values
        return parameters


def _guard(
    expression: _Expression,
    owner_id: str,
    allowed: Collection[ExpenseStatus],
    expected_version: int | None = None,
) -> str:
    """``ownerId = :owner AND status IN (...) [AND version = :expected_version]``."""
    statuses = ", ".join(expression.value(f"allowed_{status.lower()}", status.value) for status in allowed)
    parts = [
        f"{expression.name('ownerId')} = {expression.value('owner', owner_id)}",
        f"{expression.name('status')} IN ({statuses})",
    ]
    if expected_version is not None:
        parts.append(f"{expression.name('version')} = {expression.value('expected_version', expected_version)}")
    return " AND ".join(parts)


def _required_fields_condition(expression: _Expression) -> str:
    """Every field needed for submission exists, and ``amount > 0``."""
    parts = [f"attribute_exists({expression.name(name)})" for name in REQUIRED_FOR_SUBMIT if name != "amount"]
    parts.append(f"{expression.name('amount')} > {expression.value('zero', 0)}")
    return " AND ".join(parts)


def _touch(expense_id: str, caller: Caller, now: str, status: ExpenseStatus) -> dict[str, Any]:
    """The attributes every status-setting write updates together (status, GSI2 keys, timestamps, actor)."""
    return {
        "status": status.value,
        keys.GSI2PK: keys.status_gsi2pk(status),
        keys.GSI2SK: keys.status_gsi2sk(now, expense_id),
        "updatedAt": now,
        "lastActorId": caller.user_id,
        "lastActorEmail": caller.email,
    }


# --- Failure diagnosis --------------------------------------------------------------------------------------

_deserializer: Final = TypeDeserializer()


def _failure(
    error: ClientError,
    expense_id: str,
    caller: Caller,
    action: str,
    allowed: Collection[ExpenseStatus],
    expected_version: int | None = None,
    extra_diagnosis: Callable[[Item], DomainError | None] | None = None,
) -> Exception:
    """Translate a failed conditional write into the domain error that explains it (other errors pass through)."""
    if error.response.get("Error", {}).get("Code") != _CONDITIONAL_CHECK_FAILED:
        return error
    raw = error.response.get("Item")
    if raw is None:
        return NotFoundError(f"expense {expense_id} not found")
    old = {name: _deserializer.deserialize(value) for name, value in raw.items()}
    if old.get("ownerId") != caller.user_id:
        return ForbiddenError(f"only the owner can {action} this expense")
    if old.get("status") not in allowed:
        expected = " or ".join(allowed)
        return ConflictError(f"cannot {action} an expense in status {old.get('status')} (must be {expected})")
    if expected_version is not None and old.get("version") != expected_version:
        return ConflictError(
            f"expense was changed by someone else (version {old.get('version')}, you sent {expected_version});"
            " reload it and try again"
        )
    if extra_diagnosis is not None and (diagnosed := extra_diagnosis(old)) is not None:
        return diagnosed
    return ConflictError("expense changed while it was being updated; try again")


def _missing_fields_error(item: Item) -> DomainError | None:
    missing = missing_required_fields(item)
    if not missing:
        return None
    return DomainValidationError(f"cannot submit: missing or invalid {', '.join(missing)}")


# --- Input checks -------------------------------------------------------------------------------------------


def _clean_attributes(attributes: Mapping[str, Any]) -> Item:
    """Validate editable attributes for a write; ``amount`` becomes an exact ``Decimal``.

    ``None``/``""`` are rejected: optional attributes are *absent* when unknown (pass them as removals instead),
    which is what ``attribute_exists`` conditions and ``if_not_exists`` rely on.
    """
    _check_names(attributes, EDITABLE_ATTRIBUTES, "editable")
    if blank := sorted(name for name, value in attributes.items() if value is None or value == ""):
        raise ValueError(f"blank values must be removals, not updates: {blank}")
    return {name: to_decimal(value) if name == "amount" else value for name, value in attributes.items()}


def _check_names(names: Iterable[str], allowed: Collection[str], kind: str) -> None:
    if unknown := sorted(set(names) - set(allowed)):
        raise ValueError(f"not {kind} expense attributes: {unknown}")


def _status(value: str) -> ExpenseStatus:
    try:
        return ExpenseStatus(value)
    except ValueError:
        raise DomainValidationError(f"unknown status {value!r}") from None


def _start_key(
    cursor: str | None,
    key_attributes: tuple[str, ...],
    partition_attribute: str,
    partition_value: str,
) -> Item | None:
    """Decode a page cursor and check it belongs to the partition being queried.

    A cursor is client input. Checking its shape (exactly the index's key attributes, all strings) and partition
    turns a tampered or stale token into a clean 400 instead of a DynamoDB ``ValidationException`` (500).
    """
    start = decode_cursor(cursor)
    if start is None:
        return None
    valid = (
        set(start) == set(key_attributes)
        and all(isinstance(value, str) for value in start.values())
        and start[partition_attribute] == partition_value
    )
    if not valid:
        raise DomainValidationError("invalid cursor")
    return start
