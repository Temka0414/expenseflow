"""End-to-end smoke test of a DEPLOYED stage: one expense through its whole life, via the real APIs.

Run it after every deploy. Each numbered step prints what happens and which AWS service does the work:

1. Create a temporary employee and manager with Cognito *admin* APIs and sign them in.
2. Create a draft expense and upload the receipt straight to S3 with a presigned POST.
3. Wait for the ReceiptPipeline (S3 -> EventBridge -> Step Functions -> Claude on Amazon Bedrock).
4. Apply the AI suggestions and submit; the manager approves (Step Functions task-token callback).
5. Verify the expense is APPROVED and that the stream processor wrote the audit history.

The temporary users are ALWAYS deleted at the end, pass or fail (``--keep`` keeps them). The HTTP calls
use only the standard library (``urllib``), so the code shows exactly what goes over the wire; boto3 is
used for the Cognito admin calls and the stack outputs.

Example (PowerShell)::

    python scripts/smoke_test.py --stage dev --profile expenseflow
"""

from __future__ import annotations

import argparse
import itertools
import json
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from _common import (
    REPO_ROOT,
    ScriptError,
    add_target_arguments,
    create_session,
    read_auto_approve_limit,
    run_script,
    stack_outputs,
)

RECEIPT = REPO_ROOT / "samples" / "sample-receipt.pdf"
CATEGORIES = frozenset({"TRAVEL", "MEALS", "LODGING", "TRANSPORT", "SUPPLIES", "SOFTWARE", "TRAINING", "OTHER"})
POLL_SECONDS = 3

type Undo = list[tuple[str, Callable[[], str | None]]]
"""Cleanup actions, run newest first: (description, action returning an optional message)."""

_step_numbers = itertools.count(1)


def step(title: str, why: str = "") -> None:
    print(f"\n[{next(_step_numbers)}] {title}", flush=True)
    if why:
        print(f"    {why}")


def note(message: str) -> None:
    print(f"    - {message}", flush=True)


# ---------------------------------------------------------------------------------------------
# HTTP (standard library only)
# ---------------------------------------------------------------------------------------------
class ApiError(ScriptError):
    """The HTTP API answered with a status code other than the expected one."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def http(
    method: str, url: str, body: bytes | None = None, headers: Mapping[str, str] | None = None
) -> tuple[int, bytes]:
    """One HTTP exchange; 4xx/5xx statuses are returned like any other answer."""
    request = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise ScriptError(f"{method} {url} failed: {exc.reason}") from exc


class Api:
    """The ExpenseFlow HTTP API as one signed-in user.

    API Gateway's JWT authorizer checks the Cognito ID token (signature, issuer, audience, expiry)
    BEFORE the Lambda runs; the Lambda then authorizes by the token's group claims.
    """

    def __init__(self, base_url: str, id_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {id_token}"}

    def call(self, method: str, path: str, body: Any = None, expect: int = 200) -> Any:
        headers = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = http(method, self.base_url + path, data, headers)
        if status != expect:
            raise ApiError(status, f"{method} {path} returned HTTP {status}: {raw[:300].decode(errors='replace')}")
        return json.loads(raw) if raw else None


def multipart_form(fields: Mapping[str, str], filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    """Encode a ``multipart/form-data`` body for an S3 presigned POST; returns (body, Content-Type header).

    The signed policy fields come first and the file LAST: S3 ignores every form field after the file.
    """
    boundary = secrets.token_hex(16)
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    )
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    body += data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def wait_for(what: str, probe: Callable[[], Any | None], timeout: int) -> Any:
    """Poll ``probe`` until it returns something other than None.

    Almost everything after an API call is asynchronous here (streams, EventBridge, Step Functions),
    so the test polls the API the way the SPA would without its WebSocket.
    """
    deadline = time.monotonic() + timeout
    while (result := probe()) is None:
        if time.monotonic() > deadline:
            raise ScriptError(f"Timed out after {timeout}s waiting for {what}.")
        time.sleep(POLL_SECONDS)
    return result


def suggested_fields(extraction: Mapping[str, Any] | None, today: date) -> dict[str, str]:
    """merchant, category and expenseDate for the expense: the AI's reading when usable, else a test value.

    The model's output is never trusted blindly: each value is checked against the API's rules first.
    """
    result = extraction or {}
    merchant, category, day = result.get("merchant"), result.get("category"), result.get("date")
    return {
        "merchant": merchant.strip()[:120] if isinstance(merchant, str) and merchant.strip() else "Smoke Test",
        "category": category if isinstance(category, str) and category in CATEGORIES else "OTHER",
        "expenseDate": str(day) if _is_recent_date(day, today) else today.isoformat(),
    }


def _is_recent_date(value: object, today: date) -> bool:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return False
    return parsed.isoformat() == value and date(2000, 1, 1) <= parsed <= today + timedelta(days=1)


def missing_history(history: Sequence[Mapping[str, Any]]) -> list[str]:
    """What the stream processor has not yet written to the expense's audit history."""
    statuses = {e.get("to") for e in history if e.get("kind") == "STATUS"}
    wanted = [f"STATUS -> {s}" for s in ("PENDING_APPROVAL", "APPROVED") if s not in statuses]
    return wanted + ([] if any(e.get("kind") == "RECEIPT" for e in history) else ["RECEIPT"])


def create_user(cognito: Any, pool_id: str, role: str, groups: Sequence[str], undo: Undo) -> tuple[str, str]:
    """Create a CONFIRMED temporary user without sending e-mail; returns ``(email, password)``."""
    email = f"smoke-{datetime.now(UTC):%Y%m%d%H%M%S}-{secrets.token_hex(3)}-{role}@example.com"
    password = f"Aa1!{secrets.token_urlsafe(24)}"  # satisfies the pool policy: 12+ characters, all classes
    attributes = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
        {"Name": "name", "Value": f"Smoke Test {role.title()}"},
    ]
    created = cognito.admin_create_user(
        UserPoolId=pool_id, Username=email, UserAttributes=attributes, MessageAction="SUPPRESS"
    )
    username = created["User"]["Username"]  # Cognito's internal name (a UUID); sign-in uses the e-mail alias
    undo.append((f"delete user {email}", lambda: cognito.admin_delete_user(UserPoolId=pool_id, Username=username)))
    cognito.admin_set_user_password(UserPoolId=pool_id, Username=username, Password=password, Permanent=True)
    for group in groups:
        cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=group)
    return email, password


def sign_in(cognito: Any, pool_id: str, client_id: str, email: str, password: str) -> str:
    """ADMIN_USER_PASSWORD_AUTH: returns the ID token."""
    response = cognito.admin_initiate_auth(
        UserPoolId=pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    if "AuthenticationResult" not in response:
        raise ScriptError(f"Cognito answered the sign-in of {email} with a {response.get('ChallengeName')} challenge.")
    return str(response["AuthenticationResult"]["IdToken"])


def discard_expense(api: Api, expense_id: str) -> str:
    """Delete the expense if it can still be deleted (DRAFT; a pending one is withdrawn first)."""
    status = api.call("GET", f"/expenses/{expense_id}")["expense"]["status"]
    if status == "PENDING_APPROVAL":
        api.call("POST", f"/expenses/{expense_id}/withdraw")
        status = "DRAFT"
    if status != "DRAFT":
        return f"kept expense {expense_id}: {status} expenses are permanent records"
    api.call("DELETE", f"/expenses/{expense_id}", expect=204)
    return f"deleted expense {expense_id} and its receipt"


# ---------------------------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------------------------
def run(args: argparse.Namespace, undo: Undo) -> None:
    """The scenario, step by step. Everything it creates is registered on ``undo`` straight away."""
    timeout: int = args.timeout
    session = create_session(args)

    step(
        "Find the deployed stage",
        "STS GetCallerIdentity shows whose credentials are in use; CloudFormation DescribeStacks returns "
        "the CfnOutputs CDK published.",
    )
    identity = session.client("sts").get_caller_identity()
    note(f"account {identity['Account']} in {session.region_name} as {identity['Arn']}")
    auth = stack_outputs(session, args.stage, "Auth")
    api_url = stack_outputs(session, args.stage, "Backend")["ApiUrl"]
    pool_id, client_id = auth["UserPoolId"], auth["UserPoolClientId"]
    limit = read_auto_approve_limit(session, args.stage)
    note(f"API {api_url}, user pool {pool_id}, auto-approve limit {limit}")

    step(
        "Create a temporary employee and manager, and sign them in",
        "Cognito admin APIs (authorized by your IAM credentials) create users without e-mail. Such users "
        "skip the PostConfirmation Lambda, so the groups are added here. ADMIN_USER_PASSWORD_AUTH is a "
        "server-side sign-in (the SPA uses SRP); the HTTP API takes the ID token (email, name, groups).",
    )
    cognito = session.client("cognito-idp")
    employee_email, employee_password = create_user(cognito, pool_id, "employee", ["employees"], undo)
    manager_email, manager_password = create_user(cognito, pool_id, "manager", ["employees", "managers"], undo)
    employee = Api(api_url, sign_in(cognito, pool_id, client_id, employee_email, employee_password))
    manager = Api(api_url, sign_in(cognito, pool_id, client_id, manager_email, manager_password))
    note(f"employee {employee_email}, manager {manager_email}")
    if args.keep:
        note(f"passwords (employee / manager): {employee_password} / {manager_password}")
    note(f"GET /me as manager: groups {manager.call('GET', '/me')['groups']}")
    employee.call("GET", "/approvals", expect=403)
    note("employee GET /approvals -> 403: the authorizer proves WHO calls, the Lambda decides WHAT they may do")

    step("Create a draft expense", "POST /expenses writes one DynamoDB item with status DRAFT.")
    expense = employee.call("POST", "/expenses", {"title": f"[smoke test] {datetime.now(UTC):%H:%M:%S}"}, expect=201)
    expense_id = expense["id"]
    undo.append((f"remove expense {expense_id}", lambda: discard_expense(employee, expense_id)))
    note(f"expense {expense_id}, status {expense['status']}, currency {expense['currency']}")

    step(
        "Upload the receipt with a presigned POST",
        "The API signs an S3 POST policy (exact key and Content-Type, size range, 300 s). The file goes "
        "from this machine straight to S3: no bytes pass through API Gateway or Lambda.",
    )
    data = RECEIPT.read_bytes()
    presigned = employee.call(
        "POST", f"/expenses/{expense_id}/receipt-upload", {"contentType": "application/pdf", "size": len(data)}
    )
    body, content_type = multipart_form(presigned["fields"], RECEIPT.name, "application/pdf", data)
    status, answer = http("POST", presigned["url"], body, {"Content-Type": content_type})
    if status not in {200, 201, 204}:
        raise ScriptError(f"S3 rejected the upload (HTTP {status}): {answer[:300].decode(errors='replace')}")
    note(f"S3 accepted {len(data)} bytes as {presigned['key']} (HTTP {status})")

    step(
        "Wait for the ReceiptPipeline",
        "S3 sends 'Object Created' to EventBridge; a rule starts the ReceiptPipeline state machine, which "
        "asks Claude on Amazon Bedrock to read the receipt and stores the result on the expense.",
    )

    def extraction_done() -> dict[str, Any] | None:
        current = employee.call("GET", f"/expenses/{expense_id}")["expense"]
        return (
            current if (current.get("extraction") or {}).get("status") in {"SUCCEEDED", "FAILED", "SKIPPED"} else None
        )

    expense = wait_for("the receipt extraction", extraction_done, timeout)
    extraction = expense["extraction"]
    if extraction["status"] == "FAILED":
        raise ScriptError(
            f"Receipt extraction FAILED: {extraction.get('error')}\n"
            "Check Bedrock access with scripts/check_bedrock.py, or deploy with -c extractionModelId=none."
        )
    if expense.get("receipt") is None:
        raise ScriptError("The extraction finished but the expense has no stored receipt.")
    note(f"extraction {extraction['status']} (model {extraction.get('model')}): {extraction.get('result')}")

    step(
        "Apply the AI suggestions",
        "Human in the loop: the AI only suggests, PATCH stores. The amount is set just above the "
        f"auto-approval limit ({limit}) so the workflow waits for the manager instead of approving by itself.",
    )
    fields = suggested_fields(extraction.get("result"), datetime.now(UTC).date())
    patch = {**fields, "amount": limit + 1, "currency": expense["currency"], "version": expense["version"]}
    employee.call("PATCH", f"/expenses/{expense_id}", patch)
    note(f"{patch['merchant']} / {patch['amount']} {patch['currency']} / {patch['expenseDate']} / {patch['category']}")

    step(
        "Submit for approval",
        "A conditional write moves DRAFT -> PENDING_APPROVAL. The DynamoDB stream triggers stream_processor, "
        "which publishes 'Expense Submitted' to the custom EventBridge bus; a rule starts the ApprovalWorkflow.",
    )
    status = employee.call("POST", f"/expenses/{expense_id}/submit")["status"]
    note(f"status {status}")

    step(
        "The manager opens the receipt and approves",
        "The workflow pauses at a .waitForTaskToken task; the decision API resumes it with SendTaskSuccess. "
        "Until the workflow has stored its task token, the API answers 409.",
    )
    link = manager.call("GET", f"/expenses/{expense_id}/receipt")["url"]
    status, downloaded = http("GET", link)
    if status != 200 or downloaded != data:
        raise ScriptError(f"The presigned GET did not return the uploaded receipt (HTTP {status}).")
    note(f"downloaded the receipt through a presigned GET ({len(downloaded)} bytes, identical)")

    def decide() -> bool | None:
        try:
            manager.call(
                "POST",
                f"/approvals/{expense_id}/decision",
                {"decision": "APPROVED", "comment": "Approved by scripts/smoke_test.py"},
                expect=202,
            )
        except ApiError as exc:
            if exc.status != 409:
                raise
            current = employee.call("GET", f"/expenses/{expense_id}")["expense"]["status"]
            if current != "PENDING_APPROVAL":
                raise ScriptError(
                    f"Expected the workflow to wait for a manager, but the expense is {current}."
                ) from exc
            return None
        return True

    wait_for("the workflow to be ready for a decision", decide, timeout)
    note("POST /approvals/<id>/decision -> 202 Accepted")

    step(
        "Wait for the decision and verify the audit history",
        "RecordDecision is a direct DynamoDB UpdateItem from Step Functions (no Lambda); stream_processor "
        "then writes one HISTORY item per status change and receipt result.",
    )

    def decided() -> dict[str, Any] | None:
        current = employee.call("GET", f"/expenses/{expense_id}")["expense"]
        return current if current["status"] != "PENDING_APPROVAL" else None

    expense = wait_for("the workflow to record the decision", decided, timeout)
    approval = expense.get("approval") or {}
    if (
        expense["status"] != "APPROVED"
        or approval.get("approverEmail") != manager_email
        or approval.get("autoApproved")
    ):
        raise ScriptError(f"Expected APPROVED by {manager_email}, got {expense['status']} with {approval}.")
    note(f"APPROVED by {approval['approverEmail']} at {expense.get('decidedAt')}")

    def history_complete() -> list[dict[str, Any]] | None:
        history = employee.call("GET", f"/expenses/{expense_id}")["history"]
        return history if not missing_history(history) else None

    for entry in wait_for("the audit history", history_complete, timeout):
        note(f"{entry.get('at')}  {entry.get('kind')} {entry.get('to') or entry.get('extractionStatus') or ''}")


def clean_up(undo: Undo, *, keep: bool) -> None:
    """Run the undo actions newest first, whatever happened; one failing action never stops the rest."""
    if not undo:
        return
    if keep:
        step("Keep the test data (--keep)", "Delete the temporary users in the Cognito console when you are done.")
        return
    step("Clean up", "Runs whether the test passed or failed.")
    for description, action in reversed(undo):
        try:
            note(action() or f"done: {description}")
        except Exception as exc:  # a failed cleanup must not hide the test result
            note(f"could not {description}: {exc}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "End-to-end test of a deployed ExpenseFlow stage: temporary users, receipt upload, AI extraction, "
            "submit and manager approval. The temporary users are always deleted."
        ),
    )
    add_target_arguments(parser)
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait per asynchronous step (default: 300)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary users (their passwords are printed)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not RECEIPT.is_file():
        raise ScriptError(f"Sample receipt {RECEIPT} not found.")
    undo: Undo = []
    try:
        run(args, undo)
    finally:
        clean_up(undo, keep=args.keep)
    print("\nSMOKE TEST PASSED: the deployed stage handled the whole expense lifecycle.")
    return 0


if __name__ == "__main__":
    run_script(main)
