# Phase 2: Workstation tools

## Goal

Your Windows PC has every tool the project needs, at known versions, and you can prove it with four commands.

## Why

"It works on my machine" is the most common deployment problem. Install the same tools, at versions you wrote down, and the rest of the playbook behaves the same for you as for everyone else. Version numbers also tell you which error messages to expect.

## Exact steps

### 1. Install the tools with winget

`winget` is Windows' package manager. Open **Windows PowerShell** and run:

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.13 -e
winget install --id OpenJS.NodeJS.LTS -e
winget install --id Amazon.AWSCLI -e
```

Then **close the terminal and open a new one**. A running terminal does not see the new PATH.

| Tool | Why you need it | Version for ExpenseFlow |
|---|---|---|
| Git | get the code, version your work | any current version |
| Python | the CDK app, the Lambda code, the helper scripts | **3.13** (the Lambda runtime is Python 3.13) |
| Node.js + npm | frontend build and the CDK command-line tool | **22.22 or newer** (CI uses 24) |
| AWS CLI v2 | sign in, ask AWS questions from the shell | **2.32 or newer** (needed for `aws login`) |
| CDK CLI | `cdk synth`, `cdk deploy` | **2.1143.0**, not installed globally (see below) |

Optional: `winget install --id GitHub.cli -e` (the `gh` command, used in the [portfolio guide](../portfolio/github.md)).

**In ExpenseFlow:** `frontend/package.json` requires Node `>=22.22.0` and `npm ci` refuses to run on an older Node (`engine-strict` in `frontend/.npmrc`).

The Node LTS that winget installs may be newer than 24. That is fine, as long as it is 22.22 or newer.

### 2. Do not install the CDK CLI globally

The CDK command-line tool changes often. Pin it per project, so every machine and CI run uses the same one:

```powershell
npx aws-cdk@2.1143.0 --version      # works anywhere, downloads on first use
```

**In ExpenseFlow:** `infra/package.json` pins `aws-cdk` to `2.1143.0`. After `cd infra; npm ci` you run `npx cdk ...` and get exactly that version. The CDK *libraries* are Python packages pinned in `infra/requirements.txt` (`aws-cdk-lib==2.273.0`).

### 3. Turn off the Python "App execution aliases"

On a fresh Windows 11, typing `python` can open the Microsoft Store instead of Python. Fix it once:

1. **Settings > Apps > Advanced app settings > App execution aliases**.
2. Turn **off** the entries for `python.exe` and `python3.exe` (App Installer).

<!-- screenshot: Settings > Apps > Advanced app settings > App execution aliases with python.exe off -->

**In ExpenseFlow:** `python -m venv .venv` and `cdk.json` (`python app.py`) must find Python 3.13, not the Store stub.

### 4. Keep paths short (Windows MAX_PATH)

Windows still limits a full path to 260 characters unless long paths are enabled. The CDK bundles the Lambda dependencies into deep folders, and a long project path can make that fail with confusing "file not found" errors.

- **Easiest:** clone into a short folder such as `C:\src\expenseflow`.
- **Or** enable long paths (run PowerShell as Administrator, then restart the terminal):

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
git config --system core.longpaths true
```

**In ExpenseFlow:** clone into `C:\src\expenseflow`. The project's own folder is only 38 characters long for the same reason.

### 5. Allow PowerShell scripts for your user

Virtual-environment activation and `scripts\deploy.ps1` are PowerShell scripts. If Windows blocks them:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

**In ExpenseFlow:** `.venv\Scripts\Activate.ps1` and `scripts\deploy.ps1` are PowerShell scripts.

### 6. Write the versions down

```powershell
git --version
python --version
node --version
aws --version
```

Paste the output into your project notes. When something breaks in three months, you will know what "working" looked like.

**In ExpenseFlow:** also write down the CDK version that the project pins: after `cd infra; npm ci`, run `npx cdk --version`.

## ✅ Verify

| Command | Expected |
|---|---|
| `git --version` | `git version 2.x` |
| `python --version` | `Python 3.13.x` |
| `node --version` | `v22.22` or higher |
| `aws --version` | `aws-cli/2.32` or higher |
| `npx aws-cdk@2.1143.0 --version` | `2.1143.0 (build ...)` |

## ⚠️ Common errors

| Problem | Fix |
|---|---|
| `'aws' is not recognized` right after install | Open a **new** terminal. If it still fails, sign out and in again. |
| `python` opens the Microsoft Store | Step 3. |
| `aws login` says `invalid choice` | Your AWS CLI is older than 2.32. Run `aws update` or install the latest with `winget upgrade Amazon.AWSCLI`. |
| `npm error engine Unsupported engine` | Your Node is too old. Install a newer one with winget. |
| `running scripts is disabled on this system` | Step 5. |
| `jsii` prints an `ENOTEMPTY` error when `cdk` exits | Harmless noise on Windows. Ignore it if the command succeeded. |
| Strange "file not found" or "path too long" during `cdk synth` | Step 4. |

## 🔁 Repeat for every project

- [ ] `git`, `python`, `node`, `aws` print versions in a **new** terminal
- [ ] Versions match what the project needs (check the project's README or `engines` field)
- [ ] CDK CLI pinned in the project, not installed globally
- [ ] Project lives in a short path
- [ ] Versions pasted into the project notes
