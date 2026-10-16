# Phase 6: Deploy

## Goal

Create the real system in your AWS account with one command, and collect the **outputs** (URLs and IDs) that you need afterwards.

## Why

`cdk deploy` turns the templates from phase 5 into real resources, in the right order, and rolls everything back if something fails. The **outputs** are how one tool (a script, a frontend, a colleague) finds what another tool created, without anyone copying IDs by hand.

## Exact steps

### 1. Pre-flight check

Do this before every deploy. It takes ten seconds and prevents the most common mistake: deploying to the wrong account or Region.

```powershell
aws sts get-caller-identity
$env:AWS_REGION
```

Is it the right account, and the right Region? Is that Region bootstrapped ([phase 4](04-iac-bootstrap.md))?

**In ExpenseFlow**, also run the AI access check once ([deploy guide](../expenseflow/deploy.md) explains it): `python scripts\check_bedrock.py`. The very first call to a Claude model in an account subscribes the account through AWS Marketplace, which needs rights the application's Lambda deliberately does not have.

### 2. Deploy

Generic:

```powershell
npx cdk deploy --all
```

**In ExpenseFlow:** use the script. It checks your tools and identity, builds the frontend, then runs `cdk deploy --all`:

```powershell
.\scripts\deploy.ps1
```

Extra arguments go to `cdk deploy`, for example a second copy of the app: `.\scripts\deploy.ps1 -c stage=prod`.

What you will see:

1. CDK synthesizes (and runs cdk-nag again).
2. For changes to IAM roles or security groups it **asks for confirmation**. Read the table and answer `y`. Do not answer blindly. It is the one moment where you see exactly which permissions you are about to create.
3. Stacks deploy in dependency order. The first deploy takes several minutes, mostly for CloudFront.
4. At the end it prints each stack's **Outputs**.

### 3. Read the outputs

Command line:

```powershell
aws cloudformation describe-stacks --stack-name ExpenseFlow-Dev-Edge --query "Stacks[0].Outputs" --output table
```

Console: **CloudFormation > Stacks > ExpenseFlow-Dev-Edge > Outputs**.

<!-- screenshot: CloudFormation > Stacks > ExpenseFlow-Dev-Edge > Outputs (SiteUrl) -->

**In ExpenseFlow:** the outputs you will use:

| Stack | Output | Used for |
|---|---|---|
| `ExpenseFlow-Dev-Edge` | `SiteUrl` | the address of the website |
| `ExpenseFlow-Dev-Auth` | `UserPoolId`, `UserPoolClientId` | sign-in; `add_user_to_group.py` |
| `ExpenseFlow-Dev-Backend` | `ApiUrl`, `WebSocketUrl` | the web app's API calls and live updates |
| `ExpenseFlow-Dev-Backend` | `DashboardName` | CloudWatch dashboard ([phase 7](07-verify.md)) |
| `ExpenseFlow-Dev-Data` | `TableName`, `ReceiptsBucketName` | debugging in the console |

The helper scripts read these outputs themselves, so you rarely copy them.

### 4. Hand the outputs to the application

Never bake URLs into the code. Either write them into a config file at deploy time, or read them when the app starts.

**In ExpenseFlow:** the Frontend stack writes a `config.json` next to `index.html` during the deployment. The browser reads `/config.json` before it starts. That is why **one build works for any stage**. For local development against your deployed backend, `python scripts\write_frontend_config.py` writes the same file to `frontend\public\config.json` (not committed to Git).

### 5. Deploy again

Change the code, then run the same command. CloudFormation changes only what is different. Make it a habit:

```powershell
npx cdk diff        # what would change?
.\scripts\deploy.ps1
```

**In ExpenseFlow:** `.\scripts\deploy.ps1` again. Pass the same `-c` flags as the first time.

## ✅ Verify

- The deploy command ends without an error and prints each stack's outputs.
- **CloudFormation > Stacks** shows every stack as `CREATE_COMPLETE` or `UPDATE_COMPLETE`.
- In ExpenseFlow: open the `SiteUrl` in a browser. You see a sign-in page.

## ⚠️ Common errors

| Message | Fix |
|---|---|
| `... uses assets, so the toolkit stack must be deployed` | Not bootstrapped in this Region ([phase 4](04-iac-bootstrap.md)). |
| Stack is `ROLLBACK_COMPLETE` | The first deploy failed and rolled back. Read **CloudFormation > the stack > Events** for the first red `CREATE_FAILED` line. Delete the stack (**Delete**), fix the cause, deploy again. |
| `Resource handler returned message: "... Access to model denied / not subscribed"` | Bedrock access is missing. See `check_bedrock.py` in the [deploy guide](../expenseflow/deploy.md). |
| `already exists` for a resource | Something with that name exists from an earlier try. Delete it in the console, or choose another stage name. |
| WebSocket stage fails with `CloudWatch Logs role ARN must be set in account settings` | In ExpenseFlow the stack sets the API Gateway **account-level** CloudWatch role itself. If your account already uses one, the stack **replaces** it (the old role keeps existing). Other REST or WebSocket APIs in the same Region keep logging through the new role. |
| It asks for confirmation and you are in CI | Pass `--require-approval never` only in CI, where changes were already reviewed in a pull request ([phase 9](09-cicd.md)). |
| `Unable to resolve AWS account` | You are not signed in ([phase 3](03-identity-credentials.md)). |
| `ENOTEMPTY ... jsii-kernel` when `cdk` exits | Harmless on Windows. |

## 🔁 Repeat for every project

- [ ] Pre-flight: right account, right Region, Region bootstrapped
- [ ] `cdk diff`, then `cdk deploy --all`; read the IAM table before you press `y`
- [ ] Outputs saved (or read by scripts): URL, IDs, names
- [ ] Config reaches the app through a config file or outputs, never hard-coded
- [ ] Stacks show `*_COMPLETE` in CloudFormation
