"""ReceiptPipeline: the Step Functions state machine that processes every uploaded receipt.

Started by an EventBridge rule on the *default* bus for S3 `Object Created` events under `receipts/`:

    ParseKey -> MarkProcessing -> HasOldReceipt? --yes--> DeleteOldReceipt --.
                                                 `--no----------------------+-> ExtractReceipt
    ExtractReceipt --ok--> SaveExtraction -> Done
                   --error--> MarkFailed -> ExtractionFailed
    With `-c extractionModelId=none`, ExtractReceipt is replaced by MarkSkipped -> Done.

Design notes:

* The browser uploads straight to S3 with a presigned POST, so no API call "finishes" an upload;
  the S3 event is the signal. The object key carries the owner and expense ids
  (`receipts/{ownerId}/{expenseId}/{file}`), and `MarkProcessing` only accepts the key the API
  handed out (`pendingReceiptKey`), so foreign or replayed uploads end in `IgnoreUpload`.
* S3 events are delivered *at least once*. The first execution removes `pendingReceiptKey`, so a
  duplicate event fails the condition and stops: the pipeline is idempotent.
* Receipt bytes never pass through Step Functions (256 KiB payload limit); the Lambda reads S3.
* `SaveExtraction`/`MarkFailed` require `receiptKey` to still be this upload: if the user uploaded a
  newer receipt meanwhile, the stale result is dropped (`Superseded`).
* A failed extraction is a *handled* outcome (`ExtractionFailed` is a Succeed state): the user can
  still fill the form by hand. Only unexpected errors fail the execution and raise an alarm.
"""

from __future__ import annotations

from typing import Final

from aws_cdk import Duration
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from expenseflow_infra.config import StageConfig
from expenseflow_infra.constructs.approval_workflow import (
    STATE_ENTERED_TIME,
    acknowledge_lambda_invoke,
    jsonata,
    jsonata_state_machine,
    or_null,
    string_value,
    succeed,
    update_expense,
)
from expenseflow_infra.nag import acknowledge_wildcards

RECEIPTS_PREFIX: Final = "receipts/"
PIPELINE_TIMEOUT: Final = Duration.hours(1)
MAX_ERROR_LENGTH: Final = 500

_OBJECT_KEY: Final = "$decodeUrlComponent($states.input.detail.object.key)"
"""S3 URL-encodes object keys in events; ours are `[A-Za-z0-9/_.-]` only, decoding is defensive."""


class ReceiptPipeline(Construct):
    """Marks the upload, deletes the replaced receipt and stores Claude's extraction on the expense.

    Attributes:
        state_machine: The state machine; its input is the whole S3 `Object Created` event.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        table: dynamodb.ITableV2,
        receipts_bucket: s3.IBucket,
        extract_function: lambda_.IFunction | None,
    ) -> None:
        """Create the pipeline.

        Args:
            config: Stage settings (logs).
            table: The single table holding the expense items.
            receipts_bucket: Bucket the receipts are uploaded to.
            extract_function: `extract_receipt` Lambda (Claude on Bedrock), or None when AI
                extraction is disabled: receipts are then stored and marked SKIPPED.
        """
        super().__init__(scope, construct_id)
        self._table = table
        self._receipts_bucket = receipts_bucket
        self.state_machine = jsonata_state_machine(
            self,
            "StateMachine",
            config=config,
            definition=self._definition(extract_function),
            timeout=PIPELINE_TIMEOUT,
            comment="ExpenseFlow receipts: attach each upload to its expense, extract the fields with Claude",
        )
        acknowledge_wildcards(
            self.state_machine,
            "DeleteOldReceipt deletes objects whose keys are only known at run time; the grant covers "
            "the receipts/ prefix of the receipts bucket only.",
            resources=[receipts_bucket.arn_for_objects(f"{RECEIPTS_PREFIX}*")],
        )
        if extract_function is not None:
            acknowledge_lambda_invoke(self.state_machine, extract_function)

    def _definition(self, extract_function: lambda_.IFunction | None) -> sfn.IChainable:
        superseded = succeed(
            self, "Superseded", comment="A newer receipt replaced this one meanwhile: drop the stale result."
        )
        done = succeed(self, "Done", comment="Receipt attached to the expense.")
        process = (
            self._extract(extract_function, done, superseded)
            if extract_function is not None
            else self._mark_skipped(superseded).next(done)
        )

        delete_old = self._delete_old_receipt()
        # Best effort: a leftover object only costs storage and never shows up in the app.
        delete_old.add_catch(process, errors=[sfn.Errors.ALL])
        delete_old.next(process)
        has_old_receipt = (
            sfn.Choice.jsonata(self, "HasOldReceipt", comment="Did this upload replace an earlier receipt?")
            .when(
                sfn.Condition.jsonata(jsonata("$oldReceiptKey != null and $oldReceiptKey != $key")),
                delete_old,
            )
            .otherwise(process)
        )

        ignore_upload = succeed(
            self,
            "IgnoreUpload",
            comment="Not the upload the API handed out (or a duplicate S3 event): nothing to do.",
        )
        return self._parse_key().next(self._mark_processing(ignore_upload)).next(has_old_receipt)

    def _parse_key(self) -> sfn.Pass:
        return sfn.Pass.jsonata(
            self,
            "ParseKey",
            comment="receipts/{ownerId}/{expenseId}/{file}: the key itself says which expense it belongs to.",
            # All assignments of one state see the *previous* variable values, so each expression
            # starts from the input instead of reusing `$key`.
            assign={
                "key": jsonata(_OBJECT_KEY),
                "bucket": jsonata("$states.input.detail.bucket.name"),
                "ownerId": jsonata(f"$split({_OBJECT_KEY}, '/')[1]"),
                "expenseId": jsonata(f"$split({_OBJECT_KEY}, '/')[2]"),
            },
        )

    def _mark_processing(self, ignore_upload: sfn.IChainable) -> tasks.DynamoUpdateItem:
        return update_expense(
            self,
            "MarkProcessing",
            table=self._table,
            comment=(
                "Promote the pending upload to the expense's receipt and reset earlier extraction "
                "results. UPDATED_OLD returns the replaced receiptKey (if any) and the content type."
            ),
            update_expression=(
                "SET extractionStatus = :processing, receiptKey = :key, "
                "receiptContentType = pendingReceiptContentType, receiptUploadedAt = :now "
                "REMOVE pendingReceiptKey, pendingReceiptContentType, extractionResult, extractionError, "
                "extractionModel, extractedAt"
            ),
            condition_expression="pendingReceiptKey = :key AND ownerId = :owner",
            values={
                ":processing": tasks.DynamoAttributeValue.from_string("PROCESSING"),
                ":key": string_value("$key"),
                ":now": string_value(STATE_ENTERED_TIME),
                ":owner": string_value("$ownerId"),
            },
            return_values=tasks.DynamoReturnValues.UPDATED_OLD,
            assign={
                "oldReceiptKey": jsonata(or_null("$states.result.Attributes.receiptKey.S")),
                "contentType": jsonata("$states.result.Attributes.pendingReceiptContentType.S"),
            },
            on_condition_failed=ignore_upload,
        )

    def _delete_old_receipt(self) -> tasks.CallAwsService:
        task = tasks.CallAwsService.jsonata(
            self,
            "DeleteOldReceipt",
            comment=(
                "Delete the receipt this upload replaced. The bucket is versioned, so this adds a delete "
                "marker and the lifecycle rule removes the old version after 30 days."
            ),
            service="s3",
            action="deleteObject",
            iam_action="s3:DeleteObject",
            iam_resources=[self._receipts_bucket.arn_for_objects(f"{RECEIPTS_PREFIX}*")],
            parameters={"Bucket": self._receipts_bucket.bucket_name, "Key": jsonata("$oldReceiptKey")},
        )
        # DeleteObject is idempotent, so retrying any error is safe.
        task.add_retry(
            errors=[sfn.Errors.ALL],
            interval=Duration.seconds(1),
            max_attempts=2,
            backoff_rate=2,
            jitter_strategy=sfn.JitterType.FULL,
        )
        return task

    def _extract(
        self, extract_function: lambda_.IFunction, done: sfn.IChainable, superseded: sfn.IChainable
    ) -> sfn.IChainable:
        extract = tasks.LambdaInvoke.jsonata(
            self,
            "ExtractReceipt",
            comment="Claude on Amazon Bedrock reads the receipt image/PDF and suggests the expense fields.",
            lambda_function=extract_function,
            payload=sfn.TaskInput.from_object(
                {
                    "bucket": jsonata("$bucket"),
                    "key": jsonata("$key"),
                    "contentType": jsonata("$contentType"),
                    "expenseId": jsonata("$expenseId"),
                }
            ),
            # The Lambda's return value; the rest of `$states.result` is invocation metadata.
            outputs=jsonata("$states.result.Payload"),
        )
        # Error names are the Python exception class names raised by the function. The default
        # Lambda service retrier added by CDK stays in front of these two.
        extract.add_retry(
            errors=["ModelThrottledError"],
            interval=Duration.seconds(10),
            max_attempts=5,
            backoff_rate=2,
            max_delay=Duration.seconds(120),
            jitter_strategy=sfn.JitterType.FULL,
        )
        extract.add_retry(
            errors=["ModelUnavailableError"],
            interval=Duration.seconds(5),
            max_attempts=3,
            backoff_rate=2,
            jitter_strategy=sfn.JitterType.FULL,
        )
        mark_failed = self._mark_failed(superseded)
        mark_failed.next(
            succeed(
                self,
                "ExtractionFailed",
                comment="Handled: the receipt is attached, the user fills in the fields by hand.",
            )
        )
        # The Catch output (`$states.errorOutput`: {Error, Cause}) becomes MarkFailed's input.
        extract.add_catch(mark_failed, errors=[sfn.Errors.ALL])
        return extract.next(self._save_extraction(superseded)).next(done)

    def _save_extraction(self, superseded: sfn.IChainable) -> tasks.DynamoUpdateItem:
        return update_expense(
            self,
            "SaveExtraction",
            table=self._table,
            comment="Store Claude's suggestions as a JSON string; the user applies them in the UI.",
            update_expression=(
                "SET extractionStatus = :succeeded, extractionResult = :result, extractionModel = :model, "
                "extractedAt = :now, updatedAt = :now, GSI2SK = :gsi2sk, #version = #version + :one "
                "REMOVE extractionError"
            ),
            condition_expression="receiptKey = :key",
            names={"#version": "version"},
            values={
                ":succeeded": tasks.DynamoAttributeValue.from_string("SUCCEEDED"),
                ":result": string_value("$string($states.input)"),
                ":model": string_value("$states.input.model"),
                ":now": string_value(STATE_ENTERED_TIME),
                ":gsi2sk": string_value(f"{STATE_ENTERED_TIME} & '#' & $expenseId"),
                ":one": tasks.DynamoAttributeValue.number_from_string("1"),
                ":key": string_value("$key"),
            },
            on_condition_failed=superseded,
        )

    def _mark_failed(self, superseded: sfn.IChainable) -> tasks.DynamoUpdateItem:
        return update_expense(
            self,
            "MarkFailed",
            table=self._table,
            comment="Record why extraction failed (error name and cause) so the UI can show it.",
            update_expression="SET extractionStatus = :failed, extractionError = :error, extractedAt = :now",
            condition_expression="receiptKey = :key",
            values={
                ":failed": tasks.DynamoAttributeValue.from_string("FAILED"),
                ":error": string_value(
                    f"$substring($states.input.Error & ': ' & $states.input.Cause, 0, {MAX_ERROR_LENGTH})"
                ),
                ":now": string_value(STATE_ENTERED_TIME),
                ":key": string_value("$key"),
            },
            on_condition_failed=superseded,
        )

    def _mark_skipped(self, superseded: sfn.IChainable) -> tasks.DynamoUpdateItem:
        return update_expense(
            self,
            "MarkSkipped",
            table=self._table,
            comment="AI extraction is disabled for this stage (-c extractionModelId=none).",
            update_expression="SET extractionStatus = :skipped, extractedAt = :now",
            condition_expression="receiptKey = :key",
            values={
                ":skipped": tasks.DynamoAttributeValue.from_string("SKIPPED"),
                ":now": string_value(STATE_ENTERED_TIME),
                ":key": string_value("$key"),
            },
            on_condition_failed=superseded,
        )
