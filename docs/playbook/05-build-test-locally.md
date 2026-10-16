# Phase 5: Build and test locally

## Goal

Get the code onto your PC, install its dependencies in isolation, and prove with tests and a dry-run build (`cdk synth`) that it is correct **before** you spend a cent on AWS.

## Why

A local test takes seconds and costs nothing. A deploy takes minutes, and a failed deploy can leave a stack half-created. The cheapest bug is the one you catch before deploying. `cdk synth` also runs **cdk-nag**, which checks the generated infrastructure against AWS security best practices and fails the build on a violation.

## Exact steps

### 1. Get the code

```powershell
cd C:\src
git clone https://github.com/<your-github-user>/expenseflow.git
cd expenseflow
```

Keep the path short (see [phase 2](02-workstation-tools.md)).

**In ExpenseFlow:** this is your own copy of the repository (see the [portfolio guide](../portfolio/github.md)).

### 2. Create an isolated Python environment

A **virtual environment** (venv) keeps the project's Python packages away from the rest of your PC.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r backend/requirements-dev.txt -r infra/requirements-dev.txt
```

The prompt now starts with `(.venv)`. **Activate it in every new terminal** before you use `cdk`: `cdk.json` runs plain `python app.py`, which means "whichever Python comes first on PATH".

**In ExpenseFlow:** one venv at the repository root serves the backend, the CDK app and the helper scripts. All versions are pinned exactly in the `requirements*.txt` files, so every machine and CI get the same packages.

### 3. Install the Node.js dependencies

Use `npm ci` (exact versions from `package-lock.json`), not `npm install`.

```powershell
cd frontend; npm ci; cd ..
cd infra;    npm ci; cd ..
```

**In ExpenseFlow:** both folders have an `.npmrc`. `save-exact=true` stores exact versions. `min-release-age=14` refuses packages published in the last 14 days, a cheap defence against fresh malicious releases. `engine-strict=true` stops you from using a too-old Node.

### 4. Run the tests

| What | Command (from the repository root) |
|---|---|
| Backend (Lambda code), AWS is faked with `moto` | `cd backend; pytest -q; cd ..` |
| Helper scripts | `pytest -q scripts/tests` |
| Infrastructure code | `cd infra; pytest -q; cd ..` |
| Frontend | `cd frontend; npm run lint; npm test; cd ..` |

None of these call real AWS, so they need no credentials. Add the linters the CI also runs when you change Python code: `ruff check`, `ruff format --check` and `mypy` (see `.github\workflows\ci.yml`).

**In ExpenseFlow:** the backend tests use `moto` to run DynamoDB, S3, Cognito, Step Functions and more in memory. That is why hundreds of tests finish in about two minutes, with no AWS account.

### 5. Synthesize the infrastructure

`cdk synth` runs your CDK code and writes CloudFormation templates into `infra\cdk.out`. It is a full dry run: nothing is created in AWS.

With your AWS profile set ([phase 3](03-identity-credentials.md)):

```powershell
cd frontend; npm run build; cd ..
cd infra
npx cdk synth
```

Without credentials (this is what CI does), give it a fake account and Region:

```powershell
$env:AWS_REGION = 'ap-northeast-1'
$env:CDK_DEFAULT_ACCOUNT = '123456789012'
$env:CDK_DEFAULT_REGION = 'ap-northeast-1'
npx cdk synth
```

Two things to know:

- **cdk-nag:** every finding that is not fixed or explicitly *acknowledged with a written reason* (in `infra/expenseflow_infra/nag.py`) fails the synth. Read the message. It names the rule and the resource.
- **`cdk diff`** (after the first deploy) shows what a deploy would change. Make it a habit before every `cdk deploy`.

**In ExpenseFlow:** `.github\workflows\ci.yml` runs exactly this synth (with a stub frontend build and the fake account) on every pull request.

### 6. Try the app without AWS

```powershell
cd frontend
npm run dev:mock
```

Open <http://localhost:5173>. This **demo mode** runs the whole UI against an in-memory fake backend. The bar at the top switches between an employee, a manager and an admin.

**In ExpenseFlow:** this demo mode is also what [GitHub Pages](../portfolio/github.md) publishes as the live demo.

## ✅ Verify

- All four test commands end with a green summary and no failures.
- `npx cdk synth` ends without errors. `infra\cdk.out` contains `ExpenseFlow-Dev-*.template.json` files.
- `npm run dev:mock` shows the dashboard at <http://localhost:5173>.

## ⚠️ Common errors

| Message | Fix |
|---|---|
| `running scripts is disabled on this system` (when activating the venv) | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |
| `ModuleNotFoundError: No module named 'aws_cdk'` during synth | The venv is not active in this terminal. Run `.\.venv\Scripts\Activate.ps1`. |
| `npm error engine ... Unsupported engine` | Node is older than 22.22. Update it ([phase 2](02-workstation-tools.md)). |
| `-c autoApproveLimit='abc': expected a whole number` (or a similar message) | A context value is invalid. The message names the `-c` key and what it expects. |
| A warning `FrontendBuildMissing` | `frontend\dist` does not exist yet. Run `npm run build` in `frontend`, or pass `-c frontendDist=<folder>`. |
| `Error: [AwsSolutions-...]` at the end of synth | A cdk-nag finding. Fix the resource, or acknowledge it with a reason in `nag.py`. |
| `ENOTEMPTY ... jsii-kernel` when `cdk` exits | Harmless on Windows. Ignore it if the command succeeded. |
| `Cannot find module` / long-path errors while bundling | The project path is too long ([phase 2](02-workstation-tools.md), step 4). |
| Port 5173 already in use | Stop the other dev server. The port is fixed on purpose: the API's CORS allow-list contains exactly `http://localhost:5173`. |

## 🔁 Repeat for every project

- [ ] Code cloned into a short path
- [ ] Virtual environment created, activated, packages installed from pinned files
- [ ] `npm ci` (not `npm install`) in each Node folder
- [ ] Unit tests green, with AWS faked or stubbed
- [ ] `cdk synth` green with the security checks (cdk-nag or similar) on
- [ ] A local or demo mode so you can see the app without deploying
