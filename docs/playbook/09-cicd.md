# Phase 9: CI/CD with GitHub Actions and OIDC

## Goal

Every pull request is tested automatically, and every merge to `main` is deployed automatically, **without any AWS access key stored in GitHub**.

## Why

CI (continuous integration) means a robot runs your tests on every change, so a broken change cannot be merged unnoticed. CD (continuous deployment) means a robot deploys what was merged, the same way every time. The old way to give a robot AWS access was an access key in a GitHub secret. A key never expires, and if it leaks, someone else can deploy into your account.

**OIDC** fixes this. For every job, GitHub signs a short-lived token that says "I am the workflow of repository X, branch Y". AWS checks the signature and the claims, and hands out temporary credentials for one hour. There is nothing stored, so nothing to leak.

## Exact steps

### 1. CI: test every pull request

**In ExpenseFlow:** `.github\workflows\ci.yml` runs on every push to `main` and every pull request. It needs no AWS access. It has three jobs:

| Job | Runs |
|---|---|
| Backend and scripts | `ruff`, `mypy`, `pytest` for the backend; `ruff`, `pytest` for the helper scripts |
| Infrastructure | `ruff`, `mypy`, `pytest`, and a **real** `npx cdk synth` with cdk-nag (fake account `123456789012`, a stub frontend build) |
| Frontend | `npm run lint`, `npm test`, `npm run build` |

Open a pull request, scroll to the bottom, and watch the **Checks**. A red cross blocks the merge once the `main` ruleset (step 5) is active.

### 2. Create the OIDC role (once, from your PC)

AWS must trust GitHub's identity provider, and have a role that only your repository can assume.

**Check first (read-only):** IAM console > **Identity providers**. If `token.actions.githubusercontent.com` is already listed (another project created it), creating it again fails. Copy its ARN and add `-c githubOidcProviderArn=<that ARN>` to the deploy command below. Type the ARN only on the command line and never commit it: it contains your account ID.

**Case matters.** Type the owner and repository exactly as GitHub shows them at the top of the repository page: `Octo-Cat/expenseflow` is not `octo-cat/expenseflow`. The trust policy compares them letter by letter.

**In ExpenseFlow:** the optional `ExpenseFlow-GitHubOidc` stack creates the provider and the role. Use the same terminal setup as for every deploy ([deploy guide](../expenseflow/deploy.md), step 1), from the repository root:

```powershell
.\.venv\Scripts\Activate.ps1
$env:AWS_PROFILE = 'expenseflow'
$env:AWS_REGION  = 'ap-northeast-1'   # the Region you bootstrapped and deployed to
aws sts get-caller-identity
cd infra
npx cdk deploy ExpenseFlow-GitHubOidc --exclusively -c githubRepo=<Owner>/<repo>
```

`--exclusively` deploys only this stack. The other stacks are synthesized but not deployed, so the AI and alarm flags are not needed here. Read the IAM changes, then answer `y`.

It creates the IAM OIDC provider for `token.actions.githubusercontent.com` and a role that:

- accepts only tokens with audience `sts.amazonaws.com`,
- accepts only jobs of **your repository on the `main` branch** that have no `environment:` (not other branches, not pull requests),
- can do **one thing**: assume the four CDK bootstrap roles (deploy, file publishing, image publishing, lookup). Those roles hold the real rights, exactly as on your PC. In practice, code that runs in such a job can do what your deploy can do: treat `main` as admin access to the account.

Copy two outputs: `DeployRoleArn` is the role the workflow assumes, and `DeployRoleId` lets the workflow hide that role's ID in the log (step 3).

**Newer repositories:** repositories created after 15 July 2026 use an *immutable* subject that contains numeric IDs (`repo:OWNER@OWNER_ID/REPO@REPO_ID:...`). Passing `owner/repo` accepts both forms. If you prefer the strict form, get the exact names and the IDs with:

```powershell
gh api repos/<owner>/<repo> --jq '.full_name, .owner.id, .id'
```

(or open `https://api.github.com/repos/<owner>/<repo>` in a browser) and deploy with `-c githubRepo=<Owner>@<owner-id>/<repo>@<repo-id>`.

### 3. Tell GitHub which role to use

In the repository: **Settings > Secrets and variables > Actions**. In a public repository anyone can read the Actions logs. Private values go into **secrets** (GitHub masks them in logs), the rest into **variables**.

**First, choose how the alarm e-mail reaches you.** A deploy without `-c alarmEmail` deletes the e-mail subscription that an earlier deploy *with* the flag created ([phase 8](08-operate.md)). That deletion prints the old subscription's ID, which is your address without its punctuation, and GitHub cannot mask that. So it must happen on your PC, before GitHub deploys anything.

- **Console (simplest, nothing private in GitHub).** In this order:
  1. On your PC, run your usual deploy without `-c alarmEmail`. For this project with AI off: `.\scripts\deploy.ps1 -c extractionModelId=none`. Wait until it reports the stacks as deployed.
  2. **SNS > Topics >** the alarm topic **> Subscriptions** is now empty. A subscription you made by hand earlier for the same address was the *same* subscription, so it is gone too.
  3. **Create subscription**: protocol **Email**, your address. Click the link in the confirmation e-mail (it expires after 2 days) and check that the status is **Confirmed**.

  No deploy touches this subscription. Only a new topic (for example after `cdk destroy`) needs a new one. From now on never pass `-c alarmEmail`, and leave `ALARM_EMAIL` unset.
- **As code.** This version gives the subscription a fixed ID, so move it once on your PC first: deploy once *without* `-c alarmEmail` (it deletes the old subscription, and the line with its old ID stays in your local terminal), then once *with* `-c alarmEmail=<your address>`, and click the new confirmation link. Check that **SNS** shows exactly one **Confirmed** subscription. Only then set the secret `ALARM_EMAIL` to exactly that address. Upper and lower case count: a different spelling replaces the subscription and you must confirm again.

Then create the settings in this order. Use **New repository secret** on the **Secrets** tab and **New repository variable** on the **Variables** tab, not the environment ones (the deploy job has no environment):

| Order | Kind | Name | Value |
|---|---|---|---|
| 1 | Secret | `AWS_DEPLOY_ROLE_ARN` | the `DeployRoleArn` output. It is not a password, but it contains your account ID. |
| 2 | Secret | `AWS_DEPLOY_ROLE_ID` | the `DeployRoleId` output (starts with `AROA`). The credentials step prints it, and it can be traced back to your account, so the workflow masks it. |
| 3 | Secret | `ALARM_EMAIL` | only for the "as code" way above |
| 4 | Variable | `STAGE` | optional; the stage to deploy (default `dev`) |
| 5 | Variable | `EXTRACTION_MODEL_ID` | optional. Unset means AI receipt reading is **off**, like `-c extractionModelId=none`. To turn it on (Paid plan): run `python scripts\check_bedrock.py` once from your PC ([deploy guide](../expenseflow/deploy.md), step 5), then set the model id for your Region, for example `jp.anthropic.claude-opus-5-5` (Tokyo) or `au.anthropic.claude-opus-5-5` (Sydney). From then on deploy from your PC without `-c extractionModelId=none`, or your PC and GitHub keep switching AI on and off. |
| 6 | Variable | `AWS_REGION` | the Region of the OIDC stack and your bootstrapped CDK environment. **Last**, after the `main` ruleset (step 5) exists: from now on every merge to `main` deploys. |

<!-- screenshot: GitHub > Settings > Secrets and variables > Actions -->

CI passes **only** these settings. If you deploy from your PC with other `-c` flags (for example `-c senderEmail=...`), the GitHub deploy removes what they created. Stop using them, or add them to `deploy.yml` the same way (a secret for an address, a variable otherwise).

**In ExpenseFlow:** `deploy.yml` reads `secrets.AWS_DEPLOY_ROLE_ARN`, `secrets.AWS_DEPLOY_ROLE_ID`, `secrets.ALARM_EMAIL`, `vars.STAGE`, `vars.EXTRACTION_MODEL_ID` and `vars.AWS_REGION`.

### 4. CD: deploy on merge

**In ExpenseFlow:** `.github\workflows\deploy.yml` runs on every push to `main` and by hand (**Actions > Deploy > Run workflow**). Its important lines:

- `build-frontend` builds the site in its own job and hands the files to `deploy`. That build runs hundreds of third-party npm packages, so it gets no AWS access.
- `permissions: id-token: write`, on the `deploy` job only, lets it ask GitHub for the OIDC token.
- `aws-actions/configure-aws-credentials` exchanges the token for one-hour credentials for the role in the secret `AWS_DEPLOY_ROLE_ARN`. `mask-aws-account-id: true` replaces your account ID with `***` in the rest of the log; the role ID is masked from the `AWS_DEPLOY_ROLE_ID` secret.
- `npx cdk deploy --all -c stage=... -c extractionModelId=... --require-approval never` (plus `-c alarmEmail=...` when the secret is set): the same command as on your PC. The confirmation prompt cannot be answered in CI, so changes must be reviewed **in the pull request** before they reach `main`.
- The log does **not** show the stack outputs (site URL, Cognito and API IDs); read them in **CloudFormation > Stacks >** a stack **> Outputs**. The workflow uploads no templates either: they contain your account ID.
- `concurrency` with `cancel-in-progress: false`: a second run waits instead of interrupting a running deployment.

**Do not add an `environment:` key to the deploy job.** It changes the token's `sub` claim from `...:ref:refs/heads/main` to `...:environment:<name>`, and the role's trust policy no longer matches.

**Give `id-token: write` only to the job that needs it.** The role trusts every job on `main` that has this permission and no `environment:`. That is why `deploy.yml` builds the frontend in a separate job, and `pages.yml` grants it only to its `deploy` job (which has an environment), never to a job that runs `npm ci`.

### 5. Protect `main` (you work alone)

Without this, a push straight to `main` deploys, and `deploy.yml` does not wait for CI. Set it up right after CI has run once, and before you create `AWS_REGION`.

**Settings >** in the left sidebar under *Code, planning, and automation*, **Rules** or **Rulesets** (the label depends on your screen) **> New ruleset > New branch ruleset**:

- Name `main`, Enforcement status **Active**, Bypass list **empty**, Target branches **Include default branch**.
- Keep **Restrict deletions** and **Block force pushes**.
- **Require a pull request before merging**, with **Required approvals: 0**. You cannot approve your own pull request, so 1 would block every merge.
- **Require status checks to pass**: add `Backend and scripts (Python)`, `Infrastructure (CDK)` and `Frontend (React)`. They appear in the search box only after CI has run once.

From now on a direct `git push` to `main` is refused. Work like this: branch > push > pull request > green checks > merge > deploy.

### 6. The demo site is separate

**In ExpenseFlow:** `.github\workflows\pages.yml` publishes the **demo mode** to GitHub Pages. It needs no AWS and no role. Until you turn Pages on (**Settings > Pages > Source: GitHub Actions**), its run fails with `Ensure GitHub Pages has been enabled`. That is expected; re-run it afterwards. See the [portfolio guide](../portfolio/github.md).

## ✅ Verify

- A pull request shows the three CI jobs, and they pass.
- **Actions > Deploy > Run workflow** succeeds, and the log lists ✅ for each `ExpenseFlow-Dev-*` stack. `(no changes)` is normal when `main` matches what you last deployed from your PC; CloudFormation then keeps the old time.
- The log hides your account ID and e-mail. On a green run every step is collapsed, and GitHub's log search (like **Ctrl+F**) only searches expanded steps. So download it instead: the gear icon above the log **> Download log archive**, unzip it, and run in PowerShell:

  ```powershell
  Get-ChildItem <unzipped folder> -Recurse -Filter *.txt | Select-String -SimpleMatch -Pattern '<account ID>', '<your e-mail>', '<your e-mail without @ and dots>'
  ```

  It must print nothing (`***` in the log is fine).
- **Lambda** has no `ExtractReceipt` function (AI is still off), and the alarm topic in **SNS** still has one **Confirmed** e-mail subscription.
- **GitHub > Settings > Secrets and variables > Actions** has no `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY`.

## ⚠️ Common errors

| Message | Fix |
|---|---|
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The token's `sub` or `aud` does not match the role's trust policy. Check the repository name (**case**), the branch (only `main` is allowed), and that the job has no `environment:`. A wrong name needs a redeploy of the OIDC stack. |
| `Credentials could not be loaded` | `permissions: id-token: write` is missing, or the workflow runs from a fork. |
| `Could not assume role ... AccessDenied` on the CDK roles | The Region in `AWS_REGION` is not bootstrapped, or differs from the Region of the OIDC stack. |
| `Provider with url ... already exists` when deploying the OIDC stack | Reuse it with `-c githubOidcProviderArn=...` (step 2). |
| Deploy is skipped | `AWS_REGION` is not a repository **variable** (Variables tab). A secret or an environment variable with that name does not count. |
| `Repository secret AWS_DEPLOY_ROLE_ARN is not set` | Add it under **Secrets**, not Variables. |
| A new "Subscription Confirmation" e-mail, or no alarm e-mails, after a CI deploy | `ALARM_EMAIL` is spelled differently from your local `-c alarmEmail=`, it is unset while your subscription came from `-c alarmEmail`, or the one-time move on your PC was skipped. Check **SNS > Topics >** the alarm topic **> Subscriptions**, then follow step 3. |
| `GH013: Repository rule violations found` when you push to `main` | The ruleset works. Push a branch and open a pull request instead (step 5). |
| `Ensure GitHub Pages has been enabled` in the Pages workflow | Turn Pages on (step 6), then re-run. |
| `cdk deploy` waits for input in CI | `--require-approval never` is missing. |
| Deploy works locally but not in CI | CI has a clean machine. Check the logs for a missing build step or dependency. |

## 🔁 Repeat for every project

- [ ] CI workflow: lint, tests, `cdk synth` on every pull request, no AWS access
- [ ] OIDC provider and a deploy role that only assumes the CDK roles
- [ ] Role trust limited to this repository and the `main` branch; `id-token: write` only on the deploy job
- [ ] GitHub **secrets** for private values (role ARN, role ID, e-mail), **variables** for the rest; **no** access keys anywhere
- [ ] CI deploys with the same flags as your PC (here: AI off, the same alarm e-mail setup)
- [ ] Public logs show no account ID, e-mail or stack outputs
- [ ] A ruleset on `main` requires the CI checks, with no bypass, before the deploy is switched on
- [ ] `--require-approval never` only in CI
