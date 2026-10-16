"""E-mail notifications for domain events (SPEC 7.2): who is told, what the message says, and sending it with SES.

Recipients per event::

    Expense Submitted   -> every member of `managers` (except the submitter)
    Expense Approved    -> the owner
    Expense Rejected    -> the owner
    Approval Escalated  -> every member of `admins` (except the owner)
    Report Generated    -> every member of `admins`
    anything else       -> nobody (withdrawals and receipt results are shown live in the app instead)

Group members are read from Cognito (``ListUsersInGroup``) at send time, so promoting someone to manager takes
effect immediately - no copy of the membership to keep in sync.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from botocore.exceptions import ClientError

from expenseflow import aws, events, settings


@dataclass(frozen=True, slots=True)
class Email:
    subject: str
    text: str


_OWNER_EVENTS: Final = frozenset({events.DETAIL_TYPE_EXPENSE_APPROVED, events.DETAIL_TYPE_EXPENSE_REJECTED})
_GROUP_EVENTS: Final[Mapping[str, str]] = {
    events.DETAIL_TYPE_EXPENSE_SUBMITTED: settings.GROUP_MANAGERS,
    events.DETAIL_TYPE_APPROVAL_ESCALATED: settings.GROUP_ADMINS,
    events.DETAIL_TYPE_REPORT_GENERATED: settings.GROUP_ADMINS,
}


def recipients(detail_type: str, detail: Mapping[str, Any]) -> list[str]:
    """E-mail addresses to notify about one event (empty when nobody is)."""
    if detail_type in _OWNER_EVENTS:
        owner_email = detail.get("ownerEmail")
        return [owner_email] if owner_email else []
    group = _GROUP_EVENTS.get(detail_type)
    if group is None:
        return []
    # Nobody approves their own expense, so the owner is never asked to (even when they are a manager/admin).
    return group_member_emails(group, exclude_user_id=detail.get("ownerId"))


def group_member_emails(group: str, *, exclude_user_id: str | None = None) -> list[str]:
    """E-mail addresses of the enabled members of a Cognito group, sorted and de-duplicated.

    ``ListUsersInGroup`` returns at most 60 users per call; the paginator follows ``NextToken`` for us.
    """
    paginator = aws.client("cognito-idp").get_paginator("list_users_in_group")
    emails: set[str] = set()
    for page in paginator.paginate(UserPoolId=settings.user_pool_id(), GroupName=group):
        for user in page["Users"]:
            attributes = {attribute["Name"]: attribute["Value"] for attribute in user.get("Attributes", [])}
            if not user.get("Enabled", True) or attributes.get("sub") == exclude_user_id:
                continue
            if email := attributes.get("email"):
                emails.add(email)
    return sorted(emails)


def compose(detail_type: str, detail: Mapping[str, Any], app_url: str) -> Email:
    """The message for an event that has recipients (see :func:`recipients`)."""
    return _COMPOSERS[detail_type](detail, app_url)


def send(sender: str, recipient: str, email: Email) -> bool:
    """Send a plain-text e-mail with SES (API v2). Returns ``False`` when SES rejected the message.

    ``MessageRejected`` is what a new account's **SES sandbox** answers for any recipient that is not a verified
    identity - expected while learning, so it is reported instead of raised. Throttling and 5xx errors DO raise:
    the SQS message then becomes visible again and is retried, and after 3 receives it moves to the DLQ.
    One message per recipient keeps addresses private and lets verified recipients get theirs in the sandbox.
    """
    try:
        aws.client("sesv2").send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [recipient]},
            Content={
                "Simple": {
                    "Subject": {"Data": email.subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": email.text, "Charset": "UTF-8"}},
                }
            },
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "MessageRejected":
            return False
        raise
    return True


# --- Message texts ------------------------------------------------------------------------------------------


def _submitted(detail: Mapping[str, Any], app_url: str) -> Email:
    return Email(
        subject=f"Approval needed: {detail['title']} ({_money(detail)})",
        text=_lines(
            f"{_owner(detail)} submitted an expense for approval.",
            "",
            f"Title:  {detail['title']}",
            f"Amount: {_money(detail)}",
            "",
            f"Review it: {_expense_link(detail, app_url)}",
        ),
    )


def _approved(detail: Mapping[str, Any], app_url: str) -> Email:
    approval = "automatically, by policy" if detail.get("autoApproved") else f"by {detail.get('actorEmail')}"
    return Email(
        subject=f"Expense approved: {detail['title']}",
        text=_lines(
            f'Your expense "{detail["title"]}" ({_money(detail)}) was approved {approval}.',
            *_comment(detail),
            "",
            f"Details: {_expense_link(detail, app_url)}",
        ),
    )


def _rejected(detail: Mapping[str, Any], app_url: str) -> Email:
    return Email(
        subject=f"Expense rejected: {detail['title']}",
        text=_lines(
            f'Your expense "{detail["title"]}" ({_money(detail)}) was rejected.',
            *_comment(detail),
            "",
            f"You can edit and resubmit it: {_expense_link(detail, app_url)}",
        ),
    )


def _escalated(detail: Mapping[str, Any], app_url: str) -> Email:
    return Email(
        subject=f"Escalated: {detail['title']} is still waiting for approval",
        text=_lines(
            f'No manager has decided on "{detail["title"]}" ({_money(detail)}) since {detail.get("requestedAt")}.',
            "As an admin you can approve or reject it.",
            "",
            f"Review it: {_expense_link(detail, app_url)}",
        ),
    )


def _report_generated(detail: Mapping[str, Any], app_url: str) -> Email:
    totals = ", ".join(f"{amount:,} {currency}" for currency, amount in (detail.get("totals") or {}).items())
    return Email(
        subject=f"Expense report {detail['month']} is ready",
        text=_lines(
            f"The report of expenses approved in {detail['month']} is ready ({detail['rowCount']} rows).",
            f"Totals: {totals or 'none'}",
            "",
            f"Download it: {app_url}/reports",
        ),
    )


_COMPOSERS: Final[Mapping[str, Callable[[Mapping[str, Any], str], Email]]] = {
    events.DETAIL_TYPE_EXPENSE_SUBMITTED: _submitted,
    events.DETAIL_TYPE_EXPENSE_APPROVED: _approved,
    events.DETAIL_TYPE_EXPENSE_REJECTED: _rejected,
    events.DETAIL_TYPE_APPROVAL_ESCALATED: _escalated,
    events.DETAIL_TYPE_REPORT_GENERATED: _report_generated,
}


def _money(detail: Mapping[str, Any]) -> str:
    """``1,280 JPY`` - every event that is e-mailed concerns a submitted expense, which always has an amount."""
    return f"{detail['amount']:,} {detail['currency']}"


def _owner(detail: Mapping[str, Any]) -> str:
    return str(detail.get("ownerName") or detail.get("ownerEmail") or "An employee")


def _comment(detail: Mapping[str, Any]) -> tuple[str, ...]:
    comment = detail.get("comment")
    return ("", f"Comment: {comment}") if comment else ()


def _expense_link(detail: Mapping[str, Any], app_url: str) -> str:
    return f"{app_url}/expenses/{detail['expenseId']}"


def _lines(*lines: str) -> str:
    return "\n".join(lines) + "\n"
