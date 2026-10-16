# Helper scripts

Small tools around the deployed app. The Python scripts read everything they need (user pool, API URL, ...) from the **CloudFormation stack outputs** of one *stage* (default `dev`), so nothing is hard-coded.

| Script | What it does |
|---|---|
| `deploy.ps1` / `deploy.sh` | Builds the frontend, then runs `npx cdk deploy --all`. Extra arguments go to `cdk deploy`. |
| `write_frontend_config.py` | Writes `frontend/public/config.json` from the stack outputs, so `npm run dev` (http://localhost:5173) uses your deployed stage. |
| `add_user_to_group.py` | Puts a signed-up user in the `managers` or `admins` Cognito group (everyone starts as `employees`). |
| `smoke_test.py` | Runs one expense through the real deployed APIs: upload, AI extraction, submit, approval, audit history. Narrates each step. |
| `check_bedrock.py` | Sends one tiny request to the configured Claude model to check Bedrock access. |

## Setup

One virtualenv for all Python in the repository (Python 3.13), created at the repository root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # bash: source .venv/bin/activate
pip install -r backend/requirements-dev.txt -r infra/requirements-dev.txt
```

Keep it **activated** when you run `deploy.*`: `cdk.json` starts the CDK app with plain `python app.py`. You also need Node.js 22+ and the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).

## Credentials and region

Sign in with `aws login` (or `aws sso login`), then choose profile and region once per terminal:

```powershell
$env:AWS_PROFILE = 'expenseflow'      # bash: export AWS_PROFILE=expenseflow
$env:AWS_REGION  = 'ap-northeast-1'   # bash: export AWS_REGION=ap-northeast-1
```

The Python scripts also accept `--profile`, `--region` and `--stage`. The deploy scripts use the environment variables so that their checks and the CDK see the same identity. Profiles created by `aws login` need `pip install "botocore[crt]"` for the Python scripts.

## First deployment

```powershell
cd infra; npx cdk bootstrap; cd ..                       # once per AWS account and Region
.\scripts\deploy.ps1                                     # bash: bash scripts/deploy.sh
python scripts/write_frontend_config.py                  # only for local development
python scripts/add_user_to_group.py --email you@example.com --group managers   # after you signed up
python scripts/smoke_test.py
```

Deploy another stage or pass other context with `-c`, for example `.\scripts\deploy.ps1 -c stage=prod -c alarmEmail=you@example.com`. The CDK asks for confirmation before it applies IAM or security-group changes: read them and answer `y`.

`smoke_test.py` creates two temporary Cognito users and deletes them again, even when the test fails (`--keep` keeps them). A decided expense is a permanent record, so the approved test expense stays and shows up in that month's totals. The AI extraction costs a small amount of Bedrock usage.

## GitHub Actions

- `.github/workflows/ci.yml` runs on every push to `main` and every pull request: backend (ruff, mypy, pytest), these scripts (ruff, pytest), infrastructure (pytest and a real `cdk synth` with cdk-nag) and frontend (lint, tests, build). It needs no AWS access.
- `.github/workflows/deploy.yml` deploys on every push to `main` (and by hand from the Actions tab) **without stored AWS keys**: GitHub proves who it is with an OIDC token, and AWS hands out temporary credentials. One-time setup:
  1. From your PC: `cd infra; npx cdk deploy ExpenseFlow-GitHubOidc -c githubRepo=<owner>/<repo>`. The stack's `DeployRoleArn` output is the role the workflow assumes.
  2. In the repository settings (Secrets and variables, Actions, **Variables**) add `AWS_DEPLOY_ROLE_ARN` (that output), `AWS_REGION` (the Region you bootstrapped and deployed to) and optionally `STAGE` (default `dev`).

## Tests

The scripts have offline tests (AWS calls are stubbed): `python -m pytest -q scripts/tests` from the repository root.
