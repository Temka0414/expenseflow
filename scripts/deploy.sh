#!/usr/bin/env bash
# Build the frontend, then deploy every ExpenseFlow stack with the AWS CDK.
#
#   bash scripts/deploy.sh                       deploy the "dev" stage
#   bash scripts/deploy.sh -c stage=prod         any extra arguments go to `cdk deploy`
#
# Credentials and region come from the usual places: AWS_PROFILE (or `aws login`/`aws sso login`) and
# AWS_REGION (e.g. ap-northeast-1). Use the environment variables rather than `--profile`, so the checks
# below and the CDK use the same identity. The first deploy in an account and region needs a one-time
# `cd infra && npx cdk bootstrap` (see scripts/README.md).
set -euo pipefail

# --help prints the comment block above (lines 2-10) instead of deploying.
case "${1:-}" in -h | --help) sed -n '2,10s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; exit 0 ;; esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() { echo "ERROR: $*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || fail "'$1' was not found. $2"; }

echo "== Checking prerequisites"
require node "Install Node.js 22 or later (https://nodejs.org)."
require npm "It comes with Node.js."
require python "Activate the virtualenv with the infra requirements installed (scripts/README.md)."
require aws "Install the AWS CLI v2 (https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)."

node_major="$(node --version | sed 's/^v//; s/\..*//')"
[ "$node_major" -ge 22 ] || fail "Node.js 22 or later is required (found $(node --version))."
# cdk.json runs plain `python app.py`, so the python on PATH must have the CDK libraries.
python -c "import aws_cdk, cdk_nag" 2>/dev/null \
  || fail "The python on PATH lacks the CDK libraries. Activate your virtualenv and run: pip install -r infra/requirements.txt"

region="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
[ -n "$region" ] || fail "No AWS region. Set AWS_REGION, for example: export AWS_REGION=ap-northeast-1"
identity="$(aws sts get-caller-identity --query Arn --output text)" \
  || fail "AWS credentials do not work. Sign in (aws login / aws sso login) or set AWS_PROFILE."
echo "Deploying as $identity in $region (profile: ${AWS_PROFILE:-default})"

echo "== Building the frontend"
(cd "$root/frontend" && npm ci && npm run build)

echo "== Deploying with the CDK"
# The CDK asks before applying IAM or security-group changes; answer y after reading them.
(cd "$root/infra" && npm ci && npx cdk deploy --all "$@")

cat <<'EOF'

Deployed. Next steps (run from the repository root):
  python scripts/add_user_to_group.py --email you@example.com --group managers
  python scripts/smoke_test.py
EOF
