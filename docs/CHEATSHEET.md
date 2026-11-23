# My AWS cheat sheet (Weeks 1–6)

One page to remember what I did, what I built and the commands I need. The plan and progress are in [ROADMAP.md](ROADMAP.md).

## 1. The story so far

| Week | What I did | The one thing to remember |
|---|---|---|
| 1 | Made an AWS account, turned on MFA | Protect the account first |
| 2 | Installed the AWS CLI, signed in from my PC, made an S3 presigned URL | My PC can talk to AWS |
| 3 | Deployed ExpenseFlow with one command | A whole app can be built from code |
| 4 | Toured ExpenseFlow's 5 core pieces | Login → API → database → files → website |
| 5 | Ran a normal ASP.NET + PostgreSQL app in Docker on my PC | Containers are disposable; data lives outside them |
| 6 | Put that same container on AWS, broke it, scaled it, deleted it | AWS keeps alive what I ask for; one command deletes it all |

## 2. The two apps

### App 1: ExpenseFlow, "serverless" (no servers at all)

An expense app: employees submit expenses with receipts, managers approve them.

```text
browser ──► CloudFront ──► S3 (website files)
   │
   ├──► Cognito (login → gives me an ID card, the token)
   │
   ├──► API Gateway ──► Lambda (Python) ──► DynamoDB
   │    (checks card)    (my code)          (the data)
   │
   └──► S3 receipts (uploaded directly, with a 5-minute pass)
```

- **Code:** `backend/` (Python Lambdas), `frontend/` (React), `infra/` (CDK).
- **Status:** deployed in Sydney, AI off. Costs about $0 when nobody uses it.
- **Its web address:** CloudFormation → stack `ExpenseFlow-Dev-Edge` → **Outputs** → `SiteUrl`.

### App 2: Company app lab, "containers" (same app, new place to run)

An IT equipment register: ASP.NET Core + PostgreSQL, a typical company app.

```text
browser ──► load balancer ──► container on ECS Fargate ──► RDS PostgreSQL
                                     ▲
                        image in ECR (built from app/Dockerfile)
```

- **Code:** `labs/company-app/` (`app/`, `compose.yaml`, `infra/`).
- **Status:** destroyed, costs $0. `npx cdk deploy` brings it back in about 10 minutes, with an empty database.
- **While deployed it costs about $2/day.** Always destroy at the end of a session.

### Two ways to move an app to the cloud (I did both)

| Strategy | Meaning | My example |
|---|---|---|
| Replatform | Keep the app, put it in a container, use managed services | Company app |
| Refactor | Rebuild the app for the cloud | ExpenseFlow |

## 3. The AWS words, one line each

| Word | Plain words | On-premise / .NET comparison |
|---|---|---|
| Region | The city where my things live (mine: Sydney, `ap-southeast-2`) | The data center |
| IAM | Who may do what in my account | Windows users and permissions |
| CloudFormation | AWS builds everything from a description file | — |
| CDK | I write that description in Python; it generates the file | — |
| S3 | Unlimited private file storage | A file server |
| Presigned URL | A temporary pass to upload or download one file | A one-time download link |
| CloudFront | Copies of my website all over the world, close to visitors | IIS + a global cache |
| Cognito | The login system | ASP.NET Identity, run by AWS |
| Token (JWT) | The signed ID card I get after login | The auth cookie |
| API Gateway | The API's front door; checks the ID card | Routing + `[Authorize]` |
| Lambda | My code; runs only when called, paid per call | A controller action without a server |
| DynamoDB | Very fast key-value database; design the keys for your questions | — (no JOINs) |
| Step Functions | A flowchart that runs the steps for me | A workflow engine |
| Docker image | A frozen package: app + runtime + OS | A published app folder that includes the OS |
| Container | A running copy of an image | A running process |
| ECR | Private store for my images | A private Docker Hub |
| ECS + Fargate | Runs my containers and keeps N of them alive; no servers to patch | IIS on servers, minus the servers |
| RDS | PostgreSQL run by AWS (backups, updates) | The database server |
| VPC, subnets | My private network inside AWS | The office network |
| Security group | A firewall around one thing | Windows Firewall rules |
| Load balancer (ALB) | Receives visitors and spreads them over the containers | A load balancer appliance |
| Secrets Manager | A safe for passwords; apps get them when they start | Passwords out of web.config |
| CloudWatch | Logs, metrics and alarms | Event Viewer + monitoring |

## 4. The 8 lessons that matter

1. **Everything is locked by default.** I open exactly what is needed: IAM, security groups, bucket policies.
2. **Infrastructure as code.** One command builds it, one command deletes it, nothing is forgotten.
3. **The console is a window, CDK is the steering wheel.** Look in the console, change things in code.
4. **Logs first.** When something breaks, read the logs (API Gateway, ECS, even the Rancher Desktop bug).
5. **Containers are disposable.** Data lives outside them: a volume on my PC, RDS on AWS.
6. **I say what I want, AWS keeps it true.** "Run 1 container": I killed it, AWS started a new one.
7. **Two copies = no downtime.** The load balancer sends visitors to whichever copy is healthy.
8. **Stop what costs money.** Serverless is about $0 when idle; containers + database + load balancer are about $2/day, so destroy them.

## 5. Where things are in the repo

```text
expenseflow/
├── backend/         ExpenseFlow's Python code (the Lambdas)
├── frontend/        ExpenseFlow's website (React)
├── infra/           ExpenseFlow's AWS setup (CDK, Python)
├── scripts/         helpers: deploy.ps1, add_user_to_group.py, smoke_test.py
├── labs/
│   └── company-app/
│       ├── app/          the ASP.NET app + its Dockerfile
│       ├── compose.yaml  runs the app + PostgreSQL on my PC
│       ├── infra/        its AWS setup (CDK, Python)
│       └── README.md     Weeks 5–6 lessons + troubleshooting
├── docs/
│   ├── ROADMAP.md      my plan and progress
│   ├── CHEATSHEET.md   this page
│   ├── playbook/       the steps for deploying ANY app on AWS
│   └── patterns/       one page per ExpenseFlow piece
└── LEARNING_LOG.md     my own notes
```

## 6. Commands

**Start of every session: sign in** (new PowerShell window, in the repo folder). If anything later says *expired*, run these again.

```powershell
aws login --profile expenseflow
$env:AWS_PROFILE = 'expenseflow'
$env:AWS_REGION = 'ap-southeast-2'
aws configure export-credentials --format powershell | Invoke-Expression
.\.venv\Scripts\Activate.ps1
```

**Company app on my PC** (Rancher Desktop running, in `labs\company-app`):

| Command | What it does |
|---|---|
| `docker compose up --build` | Build and start the app + database → <http://localhost:8080> |
| **Ctrl+C** | Stop |
| `docker compose down` | Delete the containers; the data stays in the volume |
| `docker compose down -v` | Delete the containers **and** the data |
| `docker ps` / `docker image ls` / `docker volume ls` | List containers / images / volumes |

**Company app on AWS** (signed in, in `labs\company-app\infra`):

| Command | What it does |
|---|---|
| `npm ci` | Install the CDK tool (once) |
| `npx cdk deploy` | Build everything (about 10 minutes, about $2/day from now on) |
| `npx cdk deploy -c desiredCount=2` | Run 2 containers instead of 1 |
| `npx cdk destroy` | Delete everything → $0 |

**ExpenseFlow** (signed in, in the repo folder):

| Command | What it does |
|---|---|
| `.\scripts\deploy.ps1 -c extractionModelId=none` | Deploy or update it, with AI off |
| Delete it | See [Playbook 10](playbook/10-teardown-prod.md) |

**What the words in the commands mean:**

| Word | Meaning |
|---|---|
| `npx` | Run the tool installed in this folder (the version pinned in `package.json`) |
| `cdk synth` | Only translate Python → CloudFormation template; changes nothing in AWS |
| `cdk diff` | Show what a deploy would change |
| `cdk deploy` | Translate, send to AWS, build or update |
| `cdk destroy` | Delete everything the stack created |
| `cdk bootstrap` | One-time toolbox setup per account and Region (already done for Sydney) |
| `-c name=value` | Pass a setting into the CDK code |

## 7. Good to know

- **Running right now:** ExpenseFlow only (about $0 when idle). The lab is destroyed. Left on purpose: the CDK bootstrap and the lab's image in ECR (a few cents a month).
- **Rancher Desktop 1.24 bug:** fixed by the script `%LOCALAPPDATA%\rancher-desktop\provisioning\fix-docker-healthcheck.start`. Delete it after upgrading to a version with the real fix (see the lab README, Troubleshooting).
- **Not done yet:** push this repo to GitHub; create the budget alert when I move to the Paid plan.

## 8. What's next

Week 7: monitoring + cost → Week 8: CI/CD (push to GitHub, it deploys itself) → Weeks 9–10: AI with Bedrock + portfolio.
