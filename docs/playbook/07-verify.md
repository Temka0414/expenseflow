# Phase 7: Verify

## Goal

Prove that the deployed system works end to end, and learn where to look when it does not.

## Why

"Deploy succeeded" only means CloudFormation created the resources. It does not mean a user can sign in, upload a file and get an answer. A **smoke test** (one realistic run through the whole system) finds broken wiring in a minute. The tools in this phase (logs, traces, metrics) are the same ones you will use at 2 a.m. in production, so it is best to meet them while nothing is on fire.

## Exact steps

### 1. Run the smoke test

A smoke test uses the *real deployed* APIs, with a temporary user, and cleans up after itself.

**In ExpenseFlow:**

```powershell
python scripts\smoke_test.py
```

It narrates each step. In order, it:

1. creates a temporary employee and manager in Cognito (admin APIs) and signs them in,
2. creates a draft expense and uploads `samples\sample-receipt.pdf` straight to S3 with a presigned POST,
3. waits for the receipt pipeline (S3, EventBridge, Step Functions, Claude on Bedrock) to extract the fields,
4. applies the suggestions, submits the expense, and lets the manager approve it (Step Functions task-token callback),
5. checks that the expense is `APPROVED` and that the audit history was written.

Notes: the temporary users are always deleted (`--keep` keeps them). The approved test expense stays, because a decided expense is a permanent record. The AI step costs a small amount of Bedrock usage. `--timeout 600` waits longer for each asynchronous step.

### 2. Use it by hand once

Open the app, sign in, create an expense, upload a receipt, submit it. Seeing the real thing catches problems a script does not, such as a broken layout.

**In ExpenseFlow:** create an expense, upload `samples\sample-receipt.pdf`, submit it. Use the **Under the hood** page to see which AWS service did each step.

### 3. Find the logs

Every Lambda writes to its own **CloudWatch log group**. Powertools writes structured JSON logs with a correlation id.

Console: **Lambda > Functions**, filter by `ExpenseFlow-Dev-Backend`, open a function, then **Monitor > View CloudWatch logs**. Or: **CloudWatch > Log groups**, search `ExpenseFlow-Dev`. (ExpenseFlow creates the log groups in the CDK code, so their names start with the stack name, not with `/aws/lambda/`.)

<!-- screenshot: Lambda > Functions > (function) > Monitor > View CloudWatch logs -->

Command line (live tail):

```powershell
aws logs describe-log-groups --log-group-name-prefix ExpenseFlow-Dev --query "logGroups[].logGroupName" --output text
aws logs tail <log-group-name> --follow --since 10m
```

Retention: 1 month on `dev`, 6 months on `prod`.

**In ExpenseFlow:** the first function to look at is the receipt extraction Lambda (`extract_receipt`) after the smoke test.

### 4. Follow one request with a trace

Lambda functions and both state machines have **X-Ray tracing** on. A trace shows one request crossing API Gateway, Lambda and DynamoDB, with the time spent in each.

Console: **CloudWatch > X-Ray traces > Traces** (the menu moves now and then; search for "traces"), then filter by a time range.

<!-- screenshot: CloudWatch > X-Ray traces > Traces (one trace map) -->

**In ExpenseFlow:** run the smoke test, then open the newest trace. It shows the API call, the Lambda and its DynamoDB calls.

### 5. Watch a workflow execution

Step Functions draws every execution as a diagram and records each state's input and output.

```powershell
$arn = aws cloudformation describe-stacks --stack-name ExpenseFlow-Dev-Backend --query "Stacks[0].Outputs[?OutputKey=='ReceiptStateMachineArn'].OutputValue" --output text
aws stepfunctions list-executions --state-machine-arn $arn --max-results 5
```

Console: **Step Functions > State machines > (open it) > Executions > (one execution)**. Use the `ApprovalStateMachineArn` output for the approval workflow.

<!-- screenshot: Step Functions > State machines > receipt pipeline > Executions > Graph view -->

**In ExpenseFlow:** the receipt pipeline has one execution per uploaded receipt. The approval workflow has one per submitted expense and waits for the manager (the execution stays `Running`).

### 6. Look at the dashboard

**CloudWatch > Dashboards >** `ExpenseFlow-dev` (the `DashboardName` output). It shows API requests and errors, Lambda errors and duration, state machine executions, queue backlogs, dead-letter queues, DynamoDB capacity and Claude token usage.

**In ExpenseFlow:** the dashboard has one widget per service: API, Lambda, state machines, queues, DLQs, DynamoDB and Bedrock tokens.

### 7. Check the dead-letter queues

A **dead-letter queue (DLQ)** collects messages that failed too often. It must be empty.

Console: **SQS > Queues**, column **Messages available** should be `0` for every DLQ.

**In ExpenseFlow:** there are DLQs for the work queues and for Scheduler. Each has an alarm ([phase 8](08-operate.md)).

## ✅ Verify

- `smoke_test.py` prints `SMOKE TEST PASSED` and exits with code 0 (`$LASTEXITCODE` is `0`).
- You can find one Lambda's log group and read a log line from the smoke test.
- You can open one Step Functions execution and see green states.
- The dashboard shows data, and every DLQ is empty.

## ⚠️ Common errors

| Problem | Fix |
|---|---|
| The smoke test fails at the extraction step | Bedrock access is missing or slow. Run `python scripts\check_bedrock.py`. In the console open the receipt pipeline execution and read the failed state's error. |
| The smoke test times out waiting for a step | Raise `--timeout`. Then read the logs of the function that should have run. |
| `AccessDeniedException` in a Lambda log | The function's IAM role lacks a permission. The role is defined in the CDK code. Fix it there and redeploy. Never click it in by hand. |
| No log group yet | The function has never run. Logs appear on first invocation. |
| A trace shows no segment for a service | Not every service sends X-Ray data. That is normal. |
| Dashboard widgets say "No data" | Metrics only appear after activity. Run the smoke test and refresh. |
| The smoke test says it cannot find the stack | Stage or Region differ. Use `--stage` and `--region`, or set `$env:AWS_REGION`. |

## 🔁 Repeat for every project

- [ ] An automated smoke test exists and passes after every deploy
- [ ] You know the three places to look: logs (CloudWatch Logs), traces (X-Ray), executions (Step Functions or equivalent)
- [ ] You know where the dashboard is, and what "healthy" looks like on it
- [ ] Every DLQ is empty
- [ ] The smoke test cleans up after itself
