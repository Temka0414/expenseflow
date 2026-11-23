# Phase 8: Operate

## Goal

You hear about problems before your users do, you know what the system costs, and you can get your data back.

## Why

Deploying is a moment. Operating is every day after. Serverless removes servers to patch, but not the need to watch **errors, cost and data**. Three small habits cover most of it: alarms that email you, a cost view you open once a month, and a tested way to restore data.

## Exact steps

### 1. Alarms that reach a human

A CloudWatch **alarm** watches one metric. When it crosses a limit, it sends a message to an **SNS topic**. The topic sends you an email. An alarm that nobody receives is decoration.

**In ExpenseFlow:** deploy with your address:

```powershell
.\scripts\deploy.ps1 -c alarmEmail=you@example.com
```

The Backend stack then creates alarms for HTTP API 5xx errors, Lambda errors, messages waiting in each dead-letter queue, and failed state machine executions. Each evaluates one 5-minute period, so a single failure notifies you.

**Then open your inbox.** AWS sends a "Subscription Confirmation" email from AWS Notifications. **No alarm email arrives until you click Confirm subscription.**

Without `-c alarmEmail=...` the alarms and the topic exist, but nobody is subscribed.

<!-- screenshot: SNS > Topics > ExpenseFlow topic > Subscriptions (status Confirmed) -->

### 2. Test an alarm once

An alarm you never tested may not work. Force one into the `ALARM` state and check that the email arrives:

```powershell
aws cloudwatch describe-alarms --alarm-name-prefix ExpenseFlow-Dev --query "MetricAlarms[].AlarmName" --output text
aws cloudwatch set-alarm-state --alarm-name <one-alarm-name> --state-value ALARM --state-reason "Testing the alarm email"
```

Pick an alarm that is `OK`: an alarm acts only when its state **changes**, so forcing `ALARM` on one that is already in `ALARM` sends nothing. The email arrives within a minute, and the alarm's **History** tab shows `Successfully executed action arn:aws:sns:...`. CloudWatch sets the alarm back to its real state at the next check, so this is safe.

**In ExpenseFlow:** every alarm uses the same SNS topic, so one email address hears all of them.

### 3. Dashboards

**CloudWatch > Dashboards >** `ExpenseFlow-dev`. Open it after each deploy and during incidents. See [phase 7](07-verify.md) for what each widget shows. One dashboard costs USD 3 per month beyond the first three, so it is free for a learner.

**In ExpenseFlow:** the dashboard is created by `infra\expenseflow_infra\constructs\monitoring.py`.

### 4. Know your cost

Everything in ExpenseFlow bills **per use**: Lambda invocations, API requests, DynamoDB reads and writes, Step Functions transitions, CloudFront traffic, log storage. Idle cost is small. These estimates are rough, not measurements from a real account:

| Item | Estimate |
|---|---|
| Idle or lightly used `dev` stage | cents to a few USD per month (alarms beyond the first 10 cost about USD 0.10 each per month, logs and X-Ray add a little) |
| One receipt read by Claude Opus 5.5 on Bedrock | about USD 0.02 to 0.06 (an estimate, the model is billed per input and output token; the dashboard has a token widget) |
| Everything else per expense | fractions of a cent |

The AI step dominates. To switch it off, deploy with `-c extractionModelId=none`. Check the Bedrock pricing page for your Region before you upload many receipts.

Where to look:

1. **Billing and Cost Management > Cost Explorer** (turn it on once; data appears after up to a day). Group by **Service**.
2. **Billing and Cost Management > Cost allocation tags**: activate the tags `Project`, `Stage` and `ManagedBy`. ExpenseFlow puts them on every resource, so Cost Explorer can then split the bill per stage. Tags take up to a day to show.
3. **Budgets** (phase 1): your early warning.

Put a monthly reminder in your calendar: open Cost Explorer, group by service, ask "does every line make sense?".

**In ExpenseFlow:** the per-receipt estimate comes from the Claude token widget on the dashboard. Compare it with your first real receipts.

### 5. Backups

**In ExpenseFlow:**

- The main DynamoDB table has **point-in-time recovery (PITR)**: restore to any second of the last 35 days.
- A restore always creates a **new table**. It does not overwrite the old one.

```powershell
aws dynamodb restore-table-to-point-in-time --source-table-name <table-name> --target-table-name <table-name>-restored --use-latest-restorable-time
```

Console: **DynamoDB > Tables > (your table) > Backups > Point-in-time recovery > Restore**.

- `prod` keeps the tables and buckets when the stack is deleted ([phase 10](10-teardown-prod.md)).
- Receipts live in a private S3 bucket. It is not versioned. Treat the receipt files as the source of truth you must not delete by accident.

Practice a restore **once**, on `dev`, so you are not learning it during an incident.

### 6. Keep dependencies fresh

Pinned versions do not update themselves. Once a month, check GitHub **Dependabot** alerts (repository **Security** tab) and update one package at a time through a pull request. Tests and `cdk synth` in CI tell you whether the update is safe.

**In ExpenseFlow:** the versions are pinned in `backend\requirements.txt`, `infra\requirements.txt` and the two `package-lock.json` files.

## ✅ Verify

- You received and confirmed the SNS email (SNS console shows the subscription as **Confirmed**).
- You opened Cost Explorer and found the Service view (it may be empty on day one).
- You know the table name and the restore command.

## ⚠️ Common errors

| Problem | Fix |
|---|---|
| Alarm fires but no email | The subscription is not confirmed (look for the email, check spam), or the topic has no subscriber because `alarmEmail` was not set. |
| Alarm **History** says `CloudWatch Alarms is not authorized to perform: SNS:Publish` | The topic policy has no Allow for CloudWatch. A TLS-only topic policy *replaces* SNS's default policy, so it must allow `cloudwatch.amazonaws.com` explicitly. ExpenseFlow did this wrong at first; the fix is in `monitoring.py`: redeploy. |
| No email after redeploying with `alarmEmail` | Alarms notify only on a state change, and failed notifications are not retried. Confirm the subscription, then test with an alarm that is `OK` (step 2). |
| Alarm stays in `Insufficient data` | Normal for a new system with no traffic. ExpenseFlow treats missing data as "not breaching", so it should turn `OK`. |
| Cost Explorer is empty | It needs up to 24 hours after you enable it. |
| The bill is higher than expected | Group by Service. Look for CloudWatch (logs, dashboards, alarms), Bedrock, or a forgotten resource in another Region. |
| Restore finished, but the app still shows old data | The app reads the **original** table name. Restore creates a new table; copy data from it, or point the app at it. |

## 🔁 Repeat for every project

- [ ] Alarm email subscribed **and confirmed**
- [ ] Dashboard bookmarked
- [ ] Budget alert on (phase 1), cost allocation tags activated
- [ ] Monthly cost review in your calendar
- [ ] Backups enabled for every data store, and one restore practiced
- [ ] Dependency updates scheduled
