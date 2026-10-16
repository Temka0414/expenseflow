# Phase 10: Teardown and production

## Goal

Remove everything a project created (and know what stays behind), and know what must be true before real users depend on the system.

## Why

Cloud bills come from things you forgot. **Teardown** is the last step of a learning project, and a good habit for every experiment. **Production** is different from a learning stage: data must survive a mistake, so deleting it must be hard on purpose.

## Exact steps

### 1. Tear down a stage

Run from the same folder and with the **same `-c` flags** you deployed with (the stack list depends on them):

```powershell
cd infra
npx cdk destroy --all -c stage=dev
```

CDK lists the stacks and asks for confirmation. It removes them in reverse dependency order. Add `--force` to skip the question.

**In ExpenseFlow**, a `dev` stage cleans up completely: the DynamoDB tables, the receipts, reports and site buckets (CDK empties them first), the Cognito user pool with its users, and all log groups are deleted.

### 2. Remove the optional account-wide stacks yourself

`cdk destroy --all` only knows the stacks that your flags describe. The Budget and GitHub OIDC stacks exist only when you pass their flags, so remove them explicitly with the same flag you used to create them:

```powershell
npx cdk destroy ExpenseFlow-Budget -c budgetEmail=you@example.com
npx cdk destroy ExpenseFlow-GitHubOidc -c githubRepo=<your-github-user>/expenseflow
```

**In ExpenseFlow:** only the Budget and GitHubOidc stacks work this way.

### 3. Know what stays behind

| Left behind | Why | Cost |
|---|---|---|
| `CDKToolkit` stack and the `cdk-hnb659fds-assets-...` bucket | Shared by every CDK project in this account and Region | cents (uploaded Lambda packages) |
| The API Gateway CloudWatch role | ExpenseFlow marks it `RETAIN`: the account setting may serve other APIs | none |
| The Bedrock model subscription (AWS Marketplace) | Account-level, created by `check_bedrock.py` | none by itself; Bedrock bills per use |
| The GitHub variables in your repository | They are in GitHub, not in AWS | none |

To remove the bootstrap too (only if you will never use the CDK in this Region again): empty the assets bucket in the console, then delete the `CDKToolkit` stack in **CloudFormation**.

**In ExpenseFlow:** the table lists the real leftovers of a `dev` stage.

### 4. Check that nothing is left

- **CloudFormation > Stacks**: no `ExpenseFlow-*` stacks (use the filter **Deleted** to see the history).
- **S3**, **DynamoDB**, **Lambda**, **CloudFront**: nothing named `ExpenseFlow`. CloudFront shows "Disabled" for a while before it disappears. That is normal.
- **Billing and Cost Management > Cost Explorer** tomorrow: the daily cost drops to about zero.

**In ExpenseFlow:** search for `ExpenseFlow` in each console (the stack names, tables and buckets all start with it, in any capitalization).

### 5. What `prod` does differently

`-c stage=prod` is a deliberately cautious copy of the app. **In ExpenseFlow:**

| Setting | `dev` | `prod` |
|---|---|---|
| Tables, buckets, log groups on stack delete | deleted | **kept** (retention policy `RETAIN`) |
| DynamoDB table and Cognito user pool deletion protection | off | **on** |
| CloudFormation termination protection (Edge, Data, Auth stacks) | off | **on** |
| Log retention | 1 month | 6 months |

To delete `prod` on purpose, undo the protection first:

1. For each protected stack: **CloudFormation > Stacks > (stack) > Stack actions > Edit termination protection > Deactivate**, or `aws cloudformation update-termination-protection --no-enable-termination-protection --stack-name <name>`.
2. Turn off deletion protection on the DynamoDB table (**DynamoDB > Tables > Additional settings**) and on the Cognito user pool (**Cognito > User pools > Settings**).
3. `npx cdk destroy --all -c stage=prod`.
4. Delete what was kept: in **CloudFormation > Stacks > (stack) > Resources**, the entries with status `DELETE_SKIPPED` (buckets, tables, log groups) are still there. Empty and delete them by hand. This is your last chance to export data.

### 6. Production checklist

Before real users depend on an app, go through this list. For ExpenseFlow, the honest status is shown after each item.

| Item | ExpenseFlow today |
|---|---|
| Separate AWS account for `prod` (a mistake in `dev` cannot reach it) | Not set up. Use AWS Organizations and bootstrap the new account. Organizations upgrades a Free-plan account. |
| Custom domain and certificate | **Not included.** The site uses its `https://<id>.cloudfront.net` address. A CloudFront certificate must live in `us-east-1`. |
| Email that reaches everyone | Amazon SES starts in a **sandbox**: it sends only to verified addresses. Request production access (**SES > Account dashboard > Request production access**). Cognito's built-in sign-up email is limited to about 50 emails per day. Both are fine for a demo, not for a company. |
| Alarm email | Set `-c alarmEmail=...` and confirm it ([phase 8](08-operate.md)). |
| Backups | Point-in-time recovery is on. Practice a restore. |
| Web application firewall, rate limits | **Not included.** Consider AWS WAF in front of CloudFront and the API. |
| Sign-in security | Cognito MFA (authenticator app) is optional for users. Decide whether to require it. |
| Cost | Budget alert on. Review Cost Explorer monthly. Decide on a Bedrock spend limit. |
| Compliance and data location | Receipts are personal data. Know where they are stored (your Region) and how long you keep them. |

**In ExpenseFlow:** the second column of the table is the real status of the code, not a wish.

## ✅ Verify

- After `destroy`, `aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE --query "StackSummaries[].StackName" --output text` lists no `ExpenseFlow-*` stack.
- The console search for `ExpenseFlow` in S3, DynamoDB and Lambda finds nothing.
- You can say, for each leftover, why it is there.

## ⚠️ Common errors

| Message | Fix |
|---|---|
| `Stack ... cannot be deleted while TerminationProtection is enabled` | Step 5: deactivate termination protection first (only `prod` has it). |
| `DELETE_FAILED` because a bucket is not empty | `prod` buckets are retained, not emptied. Empty the bucket in the console, then delete the stack again. A `dev` stage empties its buckets by itself, unless the helper function failed: read **Events**. |
| `DELETE_FAILED` for the DynamoDB table or user pool | Deletion protection is on. Turn it off (step 5). |
| `cdk destroy` says "no stacks match" | The `-c` flags differ from the deploy. Use the same ones, especially `-c stage=...`. |
| Destroyed the wrong stage | Ask yourself why the stage was not in the command. Always type `-c stage=<name>`. Prod has protection for exactly this day. |
| Billing shows charges after teardown | Check other Regions and Cost Explorer grouped by service. A forgotten resource from a previous experiment is the usual cause. |

## 🔁 Repeat for every project

- [ ] The exact destroy command is written in the project README
- [ ] Optional account-wide stacks (budget, OIDC) are removed too
- [ ] You know what stays behind and what it costs
- [ ] Stage rules: `dev` deletes everything; `prod` keeps data and has protection on
- [ ] Production checklist reviewed: account, domain, email limits, alarms, backups, firewall, sign-in security, cost
