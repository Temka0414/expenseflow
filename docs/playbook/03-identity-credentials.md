# Phase 3: Identity and credentials

## Goal

Your terminal acts on AWS as a **named identity with MFA**, never as root, and no permanent secret key sits in a file on your disk.

## Why

Every AWS command is a signed request from some identity. If that identity is root, one leaked file ends the account. If it is a long-lived access key, the leak never expires. The modern way is a **short-lived session** that you renew by signing in, like a website.

## Exact steps

### 1. Create your admin identity

Choose **one** option.

**Option A: IAM user + `aws login` (simplest for one person).**

1. **IAM > Users > Create user**.
2. User name: for example `admin-<yourname>`. Tick **Provide user access to the AWS Management Console** and set a password.
3. **Next > Attach policies directly**. Select:
   - `AdministratorAccess`
   - `SignInLocalDevelopmentAccess` (this lets `aws login` work for this user)
4. **Create user**. Sign in to the console as this user (use the account's sign-in URL shown on the last page).
5. Turn on MFA for it: **IAM > Users > your user > Security credentials > Assign MFA device**.

<!-- screenshot: IAM > Users > Create user > Set permissions with the two policies selected -->

If the console offers no "console access" for IAM users (newer sign-up flow, see [phase 1](01-account-guardrails.md)), use option B.

**Option B: IAM Identity Center (the company-style way).**

1. **IAM Identity Center > Enable**.
2. **Users > Add user** (your email).
3. **Permission sets > Create permission set > Predefined > AdministratorAccess**.
4. **AWS accounts > your account > Assign users or groups**, and pick your user and the permission set.

**Warning:** enabling IAM Identity Center creates an AWS **Organization**. On a Free-plan account this **upgrades the account to the Paid plan**. Credits carry over, but real charges become possible.

**Option C: long-lived access keys.** AWS lists them as "not recommended". Use them only if A and B are impossible. Never put them in a repository, and delete them after use.

**About "admin":** `AdministratorAccess` is for you, the human who builds and bootstraps. The *application* never gets it. It runs with narrow roles that the CDK creates per function, and the CI pipeline gets a role that can only assume the CDK roles (see [phase 9](09-cicd.md)). That split is called **least privilege**.

**In ExpenseFlow:** the project uses the profile name `expenseflow` in all examples.

### 2. Sign in from the terminal

Option A (IAM user):

```powershell
aws login --profile expenseflow
```

A browser opens. Sign in as your IAM user. The session lasts up to 12 hours; run the same command again when it expires.

Option B (Identity Center):

```powershell
aws configure sso --profile expenseflow
aws sso login --profile expenseflow
```

**In ExpenseFlow:** `aws login --profile expenseflow`. Every example in these docs uses the profile name `expenseflow`.

### 3. Choose profile and Region for this terminal

```powershell
$env:AWS_PROFILE = 'expenseflow'
$env:AWS_REGION  = 'ap-northeast-1'
```

These variables last only for this terminal window. Set them again in each new window (or put the two lines in your PowerShell profile if you work on one project).

Why environment variables and not `--profile` on every command: the CDK, the AWS CLI and boto3 all read them. One setting, one identity everywhere.

**Region matters twice.** The CDK CLI picks the Region of a deployment from the profile or `AWS_REGION`. If neither is set, it silently falls back to `us-east-1` and you deploy into the wrong Region.

**In ExpenseFlow:** `scripts\deploy.ps1` reads `AWS_PROFILE` and `AWS_REGION`. The Python scripts read them too, and also accept `--profile`, `--region` and `--stage`.

### 4. Ask AWS who you are

```powershell
aws sts get-caller-identity
$account = aws sts get-caller-identity --query Account --output text
$account
```

The output shows `Account` (12 digits) and `Arn` (your user or role). Check that the account is the one you expect.

**In ExpenseFlow:** `scripts\deploy.ps1` and the Python scripts print the identity and Region before they do anything. The Python scripts use `boto3`, which needs one extra package for profiles made by `aws login`:

```powershell
pip install "botocore[crt]"
```

(run this inside the project's virtual environment, see [phase 5](05-build-test-locally.md)).

## ✅ Verify

- `aws sts get-caller-identity` prints your account ID and a user or role ARN, **not** `:root`.
- `$env:AWS_REGION` prints the Region you chose.

## ⚠️ Common errors

| Message | Cause and fix |
|---|---|
| `Unable to locate credentials` | You are not signed in, or the profile name is wrong. Run `aws login --profile expenseflow` and set `$env:AWS_PROFILE`. |
| `Your session has expired` / `Token has expired` | Sign in again with `aws login`. |
| `You must specify a region` | Set `$env:AWS_REGION`. |
| `argument command: Invalid choice, 'login'` | AWS CLI older than 2.32. Update it (phase 2). |
| `AccessDenied` for `SignInLocalDevelopmentAccess` | Attach that policy to your IAM user (step 1, option A). |
| Commands run as the wrong account | An old `AWS_ACCESS_KEY_ID` environment variable overrides the profile. Remove it: `Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_SESSION_TOKEN -ErrorAction SilentlyContinue`. |
| `cdk` deploys into `us-east-1` | No Region was set. Step 3. |
| A Python script says it cannot use the `login_session` profile | `pip install "botocore[crt]"`. |

## 🔁 Repeat for every project

- [ ] Admin identity with MFA exists (reuse the same one for all your projects in this account)
- [ ] `aws login --profile <name>` works
- [ ] `$env:AWS_PROFILE` and `$env:AWS_REGION` set in this terminal
- [ ] `aws sts get-caller-identity` shows the right account and not root
- [ ] No access keys in the project folder (`git grep AKIA` finds nothing)
