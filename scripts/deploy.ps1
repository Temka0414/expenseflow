<#
.SYNOPSIS
  Build the frontend, then deploy every ExpenseFlow stack with the AWS CDK.

.DESCRIPTION
  Any extra arguments are passed to `cdk deploy`:

    .\scripts\deploy.ps1                          deploy the "dev" stage
    .\scripts\deploy.ps1 -c stage=prod            deploy another stage

  Credentials and region come from the usual places: AWS_PROFILE (or `aws login` / `aws sso login`) and
  AWS_REGION (e.g. ap-northeast-1). Use the environment variables rather than `--profile`, so the checks
  below and the CDK use the same identity:

    $env:AWS_PROFILE = 'expenseflow'; $env:AWS_REGION = 'ap-northeast-1'

  The first deploy in an account and region needs a one-time `cd infra; npx cdk bootstrap` (see
  scripts\README.md).
#>

# No param() block on purpose: with it, PowerShell would read `-c stage=prod` as an abbreviated
# parameter name instead of passing it to the CDK. Everything arrives in $args.
$cdkArgs = $args
$root = Split-Path -Parent $PSScriptRoot

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Require([string]$Command, [string]$Hint) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) { Fail "'$Command' was not found. $Hint" }
}

function Run([string]$Folder, [scriptblock]$Command) {
    Push-Location $Folder
    try { & $Command } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Fail "Command failed in $Folder (exit code $LASTEXITCODE)." }
}

Write-Host '== Checking prerequisites'
Require node   'Install Node.js 22 or later (https://nodejs.org).'
Require npm    'It comes with Node.js.'
Require python 'Activate the virtualenv with the infra requirements installed (scripts\README.md).'
Require aws    'Install the AWS CLI v2 (https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).'

$nodeVersion = (node --version).TrimStart('v')
if ([int]$nodeVersion.Split('.')[0] -lt 22) { Fail "Node.js 22 or later is required (found $nodeVersion)." }

# cdk.json runs plain `python app.py`, so the python on PATH must have the CDK libraries.
python -c "import aws_cdk, cdk_nag" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail 'The python on PATH lacks the CDK libraries. Activate your virtualenv and run: pip install -r infra\requirements.txt'
}

$region = if ($env:AWS_REGION) { $env:AWS_REGION } elseif ($env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION } else { aws configure get region 2>$null }
if (-not $region) { Fail 'No AWS region. Set AWS_REGION, for example: $env:AWS_REGION = ''ap-northeast-1''' }
$identity = aws sts get-caller-identity --query Arn --output text
if ($LASTEXITCODE -ne 0) { Fail 'AWS credentials do not work. Sign in (aws login / aws sso login) or set AWS_PROFILE.' }
$profileName = if ($env:AWS_PROFILE) { $env:AWS_PROFILE } else { 'default' }
Write-Host "Deploying as $identity in $region (profile: $profileName)"

Write-Host '== Building the frontend'
Run "$root\frontend" { npm ci; if ($LASTEXITCODE -eq 0) { npm run build } }

Write-Host '== Deploying with the CDK'
# The CDK asks before applying IAM or security-group changes; answer y after reading them.
Run "$root\infra" { npm ci; if ($LASTEXITCODE -eq 0) { npx cdk deploy --all @cdkArgs } }

Write-Host ''
Write-Host 'Deployed. Next steps (run from the repository root):'
Write-Host '  python scripts\add_user_to_group.py --email you@example.com --group managers'
Write-Host '  python scripts\smoke_test.py'
