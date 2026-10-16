"""ExpenseFlow infrastructure as code: the AWS CDK (Python) app that deploys the whole system.

* `config`     - CDK context (`cdk.json` + `-c key=value`) parsed into one validated `StageConfig`.
* `paths`      - where the backend code, the layer requirements and the frontend build live.
* `nag`        - helpers to acknowledge cdk-nag findings with a recorded reason.
* `constructs` - reusable building blocks (private S3 buckets, Lambda functions, the dependencies layer, ...).
* `stacks`     - one CloudFormation stack per concern, wired together in `app.py`.
"""
