# Learning log

One entry per lesson, newest at the top. Write it right after you merge the pull request, while it is fresh.
Keep each entry short: five to ten lines is enough. This file is the raw material for your Zenn or Qiita article
(see [docs/portfolio/github.md](docs/portfolio/github.md)).

## Template

Copy this block to the top of the list.

```markdown
### YYYY-MM-DD - <lesson title> (#<issue>, PR #<number>)

- **Goal:** what I wanted to learn.
- **What I did:** the change, in one or two sentences.
- **What I learned:** the one idea I would explain to a colleague.
- **What broke:** the error message, and what fixed it.
- **AWS console path:** where I looked, for example CloudFormation > Stacks > ExpenseFlow-Dev-Backend > Outputs.
- **Cost:** what it cost, or "nothing deployed".
- **Next:** the next lesson I want to do.
```

## Entries

### 2026-10-20 - Example: first deploy to my own account (#1, PR #1)

*This is an example of the format. Replace it with your own first entry.*

- **Goal:** deploy ExpenseFlow to my own AWS account and use it in the browser.
- **What I did:** followed [docs/expenseflow/deploy.md](docs/expenseflow/deploy.md): bootstrap, `check_bedrock.py`, `deploy.ps1`.
- **What I learned:** `cdk bootstrap` is a one-time step per account and Region. It creates the S3 bucket and roles that every later `cdk deploy` uses.
- **What broke:** `cdk deploy` said it could not find credentials. Setting `$env:AWS_PROFILE` in the same terminal fixed it.
- **AWS console path:** CloudFormation > Stacks > ExpenseFlow-Dev-Edge > Outputs > SiteUrl.
- **Cost:** about 0 USD for the deploy itself.
- **Next:** [docs/patterns/01-cognito-auth.md](docs/patterns/01-cognito-auth.md).
