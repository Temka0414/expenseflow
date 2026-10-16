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

Open a pull request, scroll to the bottom, and watch the **Checks**. A red cross blocks you from merging if you turn on branch protection (step 5).

### 2. Create the OIDC role (once, from your PC)

AWS must trust GitHub's identity provider, and have a role that only your repository can assume.

**In ExpenseFlow:** the optional `ExpenseFlow-GitHubOidc` stack does both:

```powershell
cd infra
npx cdk deploy ExpenseFlow-GitHubOidc -c githubRepo=<your-github-user>/expenseflow
```

It creates the IAM OIDC provider for `token.actions.githubusercontent.com` and a role that:

- accepts only tokens with audience `sts.amazonaws.com`,
- accepts only runs of **your repository on the `main` branch** (not other branches, not pull requests),
- can do **one thing**: assume the four CDK bootstrap roles (deploy, file publishing, image publishing, lookup). Those roles hold the real rights, exactly as on your PC.

The output `DeployRoleArn` is the role the workflow assumes. Copy it.

**Newer repositories:** repositories created after 15 July 2026 use an *immutable* subject that contains numeric IDs (`repo:OWNER@OWNER_ID/REPO@REPO_ID:...`). Passing `owner/repo` accepts both forms. If you prefer the strict form, get the IDs with:

```powershell
gh api repos/<owner>/<repo> --jq '.owner.id, .id'
```

and deploy with `-c githubRepo=<owner>@<owner-id>/<repo>@<repo-id>`.

If your account already has the GitHub OIDC provider (another project created it), creating it again fails. Pass the existing one: `-c githubOidcProviderArn=arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com`.

### 3. Tell GitHub which role to use

In the repository: **Settings > Secrets and variables > Actions > Variables > New repository variable**. Add **variables** (not secrets, none of them is secret):

| Name | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | the `DeployRoleArn` output |
| `AWS_REGION` | the Region you bootstrapped and deployed to, for example `ap-northeast-1` |
| `STAGE` | optional; the stage to deploy (default `dev`) |

<!-- screenshot: GitHub > Settings > Secrets and variables > Actions > Variables -->

**In ExpenseFlow:** `deploy.yml` reads `vars.AWS_DEPLOY_ROLE_ARN`, `vars.AWS_REGION` and `vars.STAGE`.

### 4. CD: deploy on merge

**In ExpenseFlow:** `.github\workflows\deploy.yml` runs on every push to `main` and by hand (**Actions > Deploy > Run workflow**). Its important lines:

- `permissions: id-token: write` lets the job ask GitHub for the OIDC token.
- `aws-actions/configure-aws-credentials` exchanges the token for temporary credentials for `AWS_DEPLOY_ROLE_ARN`.
- `npx cdk deploy --all --require-approval never`. The confirmation prompt cannot be answered in CI, so changes must be reviewed **in the pull request** before they reach `main`.
- `concurrency` with `cancel-in-progress: false`: a second run waits instead of interrupting a running deployment.

**Do not add an `environment:` key to the deploy job.** It changes the token's `sub` claim from `...:ref:refs/heads/main` to `...:environment:<name>`, and the role's trust policy no longer matches.

### 5. Protect `main`

Without this, anyone with write access can push to `main` and trigger a deploy.

**Settings > Branches > Add branch protection rule** (or **Rules > Rulesets**) for `main`:

- Require a pull request before merging
- Require status checks to pass: select the three CI jobs

**In ExpenseFlow:** select the three CI jobs from `ci.yml` as required checks. Note that `deploy.yml` itself does not wait for CI, so branch protection is what keeps untested code out of `main`.

### 6. The demo site is separate

**In ExpenseFlow:** `.github\workflows\pages.yml` publishes the **demo mode** to GitHub Pages. It needs no AWS and no role. See the [portfolio guide](../portfolio/github.md).

## ✅ Verify

- A pull request shows the three CI jobs, and they pass.
- **Actions > Deploy > Run workflow** succeeds. The step **Configure AWS credentials** succeeds, and no access key appears anywhere.
- **CloudFormation > Stacks** shows `UPDATE_COMPLETE` with a new time.
- **GitHub > Settings > Secrets and variables > Actions** has no `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY`.

## ⚠️ Common errors

| Message | Fix |
|---|---|
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The token's `sub` or `aud` does not match the role's trust policy. Check the repository name (case), the branch (only `main` is allowed), and that the job has no `environment:`. |
| `Credentials could not be loaded` | `permissions: id-token: write` is missing, or the workflow runs from a fork. |
| `Could not assume role ... AccessDenied` on the CDK roles | The Region in `AWS_REGION` is not bootstrapped, or differs from the Region of the OIDC stack. |
| `Provider with url ... already exists` when deploying the OIDC stack | Reuse it with `-c githubOidcProviderArn=...`. |
| Deploy is skipped or fails with an empty role ARN | The variable `AWS_DEPLOY_ROLE_ARN` is not set (variables, not secrets). |
| `cdk deploy` waits for input in CI | `--require-approval never` is missing. |
| Deploy works locally but not in CI | CI has a clean machine. Check the logs for a missing build step or dependency. |

## 🔁 Repeat for every project

- [ ] CI workflow: lint, tests, `cdk synth` on every pull request, no AWS access
- [ ] OIDC provider and a deploy role that only assumes the CDK roles
- [ ] Role trust limited to this repository and the `main` branch
- [ ] GitHub **variables** set; **no** access keys anywhere
- [ ] Branch protection on `main` requires the CI checks
- [ ] `--require-approval never` only in CI
