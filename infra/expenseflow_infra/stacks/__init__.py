"""The CloudFormation stacks of ExpenseFlow, one module per stack (wired together in `app.py`).

Deploy order follows the references between them; CDK derives it automatically:

1. `edge_stack`     - private site bucket + CloudFront distribution (the app's public URL).
2. `data_stack`     - DynamoDB tables and the receipts/reports buckets (CORS needs the site URL).
3. `auth_stack`     - Cognito user pool, groups and app client.
4. `backend_stack`  - Lambdas, HTTP + WebSocket APIs, events, workflows, monitoring.
5. `frontend_stack` - uploads the built SPA and its `config.json` to the site bucket.

Optional, account-wide: `budget_stack` (cost alerts, with `-c budgetEmail=`) and
`github_oidc_stack` (keyless CI/CD deployments, with `-c githubRepo=`).

Stacks receive the constructs they depend on (a table, a bucket, a URL token) as constructor
arguments. CDK turns each such reference into a stack output plus an `Fn::GetStackOutput` in the
consuming template (`@aws-cdk/core:defaultCrossStackReferences: "weak"` in cdk.json).
"""
