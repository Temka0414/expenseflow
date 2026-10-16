# Phase 4: Infrastructure as code and bootstrap

## Goal

Your account and Region are **bootstrapped** for the AWS CDK, and you know how one codebase becomes several environments (`dev`, `prod`).

## Why

Clicking in the console is good for learning and bad for repeating. With **infrastructure as code (IaC)** the whole system is text in Git: reviewable, testable, and re-creatable in minutes. The **AWS CDK** lets you write that text in a normal language (Python here) and turns it into CloudFormation templates.

CDK needs a small helper setup in each account and Region first: a place to upload files (Lambda code, website files) and roles that CloudFormation uses to deploy. Creating it is called **bootstrapping**.

## Exact steps

### 1. Pick the IaC tool

| Tool | What it is | Use it when |
|---|---|---|
| **AWS CDK** | You write Python, TypeScript, C# and so on. Produces CloudFormation. | You like real code, loops, tests. (ExpenseFlow) |
| CloudFormation | YAML or JSON templates, native to AWS. | You want no extra tool. Everything else is built on it. |
| Terraform / OpenTofu | Its own language, many clouds. | Your team already uses it. |

You do not need to compare further. Pick one per project and stay with it. As a .NET developer you may like CDK in C# too. ExpenseFlow uses Python because the Lambda code is Python.

**In ExpenseFlow:** the CDK app is `infra/app.py`. It builds five stacks that are always deployed (see step 4) plus two optional ones.

### 2. Pin the CDK CLI

See [phase 2](02-workstation-tools.md). In ExpenseFlow, `cd infra; npm ci` installs the pinned `aws-cdk@2.1143.0` into `infra\node_modules`, and `npx cdk` runs it.

### 3. Bootstrap the account and Region

Run this **once per account and Region**. You need the admin identity from [phase 3](03-identity-credentials.md).

Generic form, works in any folder:

```powershell
npx aws-cdk@2.1143.0 bootstrap "aws://$account/$env:AWS_REGION"
```

**In ExpenseFlow:**

```powershell
cd infra
npm ci
npx cdk bootstrap "aws://$account/$env:AWS_REGION"
```

What it creates, in a CloudFormation stack named `CDKToolkit`:

- an S3 bucket for assets, named like `cdk-hnb659fds-assets-<account>-<region>` (`hnb659fds` is the default *qualifier*)
- an ECR repository for container images (unused by ExpenseFlow)
- IAM roles: deploy, file publishing, image publishing, lookup, and the CloudFormation execution role
- an SSM parameter `/cdk-bootstrap/hnb659fds/version` that CDK reads to check the bootstrap version

Bootstrapping is cheap: it costs cents per month for the stored files. The roles it creates are the ones CI will assume in [phase 9](09-cicd.md).

Bootstrap again when you deploy to **another Region** or **another account**.

### 4. Understand environments and stages

- An **environment** is an account plus a Region.
- A **stage** is one copy of the app inside an environment, such as `dev` or `prod`. You choose it with a context value: `-c stage=prod`.
- **Context** is configuration you pass with `-c key=value` (or write in `cdk.json`). It is how one codebase gives different results.

**In ExpenseFlow:**

| Stack | Holds |
|---|---|
| `ExpenseFlow-Dev-Edge` | S3 site bucket and CloudFront distribution |
| `ExpenseFlow-Dev-Data` | DynamoDB tables, receipts and reports buckets |
| `ExpenseFlow-Dev-Auth` | Cognito user pool, groups, post-confirmation Lambda |
| `ExpenseFlow-Dev-Backend` | API Gateway, Lambdas, Step Functions, EventBridge, SQS, alarms, dashboard |
| `ExpenseFlow-Dev-Frontend` | uploads the built website and its `config.json` |
| `ExpenseFlow-Budget` (optional) | account-wide cost budget, with `-c budgetEmail=...` |
| `ExpenseFlow-GitHubOidc` (optional) | GitHub Actions deploy role, with `-c githubRepo=owner/repo` |

`-c stage=prod` gives `ExpenseFlow-Prod-*` stacks that **keep** their data on delete (see [phase 10](10-teardown-prod.md)). All settings are read in one file, `infra/expenseflow_infra/config.py`, which rejects a wrong value at synth time with a message that names the `-c` key.

### 5. Know how the Region is chosen

The CDK CLI takes the account and Region from your **profile** or `AWS_REGION`, and falls back to `us-east-1`. It sets `CDK_DEFAULT_REGION` itself before it runs `app.py`, so exporting that variable yourself does nothing. Always set `$env:AWS_REGION` (phase 3).

**In ExpenseFlow:** `infra\expenseflow_infra\config.py` documents this gotcha. Without a Region, `cdk synth` still works and builds templates that are not tied to a Region; `cdk deploy` needs one.

## ✅ Verify

```powershell
aws cloudformation describe-stacks --stack-name CDKToolkit --query "Stacks[0].StackStatus" --output text
```

Expected: `CREATE_COMPLETE` (or `UPDATE_COMPLETE` if you bootstrapped before).

In the console: **CloudFormation > Stacks > CDKToolkit**, tab **Resources**.

<!-- screenshot: CloudFormation > Stacks > CDKToolkit > Resources -->

In ExpenseFlow also run `cd infra; npx cdk ls`. It prints the five `ExpenseFlow-Dev-*` stack names.

## ⚠️ Common errors

| Message | Fix |
|---|---|
| `... uses assets, so the toolkit stack must be deployed to the environment` (during `cdk deploy`) | This account and Region are not bootstrapped. Run step 3 for the Region in `$env:AWS_REGION`. |
| `Cloud assembly schema version mismatch ... Please upgrade your CLI` | The CLI is older than the Python `aws-cdk-lib`. Use the project's pinned CLI (`npm ci`, then `npx cdk`), not a global one. |
| `ModuleNotFoundError: No module named 'aws_cdk'` | `cdk.json` runs plain `python app.py`. Activate the virtual environment first ([phase 5](05-build-test-locally.md)). |
| Bootstrapped one Region, deploying to another | `$env:AWS_REGION` differs between the two commands. Check it. |
| `AccessDenied` while bootstrapping | You are not using the admin identity. Run `aws sts get-caller-identity`. |

## 🔁 Repeat for every project

- [ ] IaC tool chosen; CLI version pinned in the project
- [ ] `cdk bootstrap aws://<account>/<region>` done for every Region you deploy to (once per account and Region, not once per project)
- [ ] Stage names decided (`dev`, `prod`), with a rule for what each keeps on delete
- [ ] All settings read in one place and validated
- [ ] `cdk ls` lists the stacks you expect
