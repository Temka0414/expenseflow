# My AWS roadmap (10 weeks, ~4–5 hours per week)

**Rule:** one topic per week, one result per week. Ignore everything else in `docs/` until its week comes.
**Mindset:** AI can type the code and commands. You learn to *choose*, *secure*, *control cost*, *debug* and *explain*.

| Week | Topic | Result | Read / do |
|---|---|---|---|
| 1 | Account + guardrails | A safe AWS account (MFA, budget) | [Playbook 01](playbook/01-account-guardrails.md) |
| 2 | Tools, IAM, CLI, S3 | Your PC talks to AWS; you can explain users, roles and policies | [Playbook 02](playbook/02-workstation-tools.md), [Playbook 03](playbook/03-identity-credentials.md) |
| 3 | Deploy ExpenseFlow | The app runs in your account | [Deploy guide](expenseflow/deploy.md) |
| 4 | ExpenseFlow core | You can draw and explain the architecture | [Patterns 01–05](patterns/01-cognito-auth.md) |
| 5–6 | Containers + RDS (ASP.NET + PostgreSQL on ECS Fargate) | You can move a typical company app to AWS | [Company app lab](../labs/company-app/README.md) |
| 7 | Monitoring + cost | You find errors in logs, set alarms, read your bill | [Pattern 13](patterns/13-observability.md), [Playbook 08](playbook/08-operate.md) |
| 8 | CI/CD | Push to GitHub, it deploys itself | [Playbook 09](playbook/09-cicd.md) |
| 9–10 | AI on AWS (Bedrock) + portfolio | AI in your portfolio and a Qiita/Zenn article | [Pattern 06](patterns/06-bedrock-ai-extraction.md), [Portfolio guide](portfolio/github.md) |
| Optional | AWS Solutions Architect – Associate | The most recognized AWS certificate in Japan | — |

## Week 1 steps

- [x] **Step 0:** the five words you need (10 min, reading only)
- [x] **Step 1:** create the AWS account and choose a plan
- [x] **Step 2:** turn on MFA
- [ ] **Step 3:** create a $10 monthly budget alert — *deferred: the Free plan cannot charge me; do this when I upgrade to the Paid plan*

## Progress

- [x] **Week 2:** AWS CLI installed, `aws login`, first command, S3 presigned-URL lab
- [x] **Week 3:** ExpenseFlow deployed to my own account (AI off); tour of DynamoDB, S3, Step Functions
- [x] **Week 4:** core pieces, one per session — Cognito (login) → API Gateway + Lambda → DynamoDB → S3 uploads → CloudFront
- [ ] **Weeks 5–6:** a typical company app (ASP.NET + PostgreSQL) in containers — Docker on my PC → ECR → ECS Fargate + RDS

**Skip for now:** WebSockets, Step Functions and EventBridge details, Kubernetes, multi-account setups, and the other 200+ services.
