# Company app lab (Weeks 5-6)

A small, typical company app: an **IT equipment register** (ASP.NET Core Razor Pages + EF Core + PostgreSQL).
In Week 5 you run it in containers on your PC. In Week 6 you move the same container to AWS.
At the end you can explain how a normal .NET web app runs on ECS Fargate with RDS.

**Honest status:** the code builds, and the app and infrastructure tests pass. Docker was not available on the machine that wrote this lab, so `docker compose` and `cdk deploy` have never run for real. You are the first. If a step fails in a way this page does not explain, write the exact message in [LEARNING_LOG.md](../../LEARNING_LOG.md) and fix this page.

## What you build

```text
  Internet (your browser)
        |
        |  HTTP, port 80
        v
  Application Load Balancer        public subnets
        |
        |  HTTP, port 8080
        v
  App container on ECS Fargate     public subnets     <-- image from Amazon ECR
        |                                             <-- DB user + password from Secrets Manager
        |  PostgreSQL, port 5432
        v
  RDS for PostgreSQL               isolated subnets (no route to the internet)

  One VPC with 2 Availability Zones. ECR and Secrets Manager are AWS services outside it.
```

| On-premise | On AWS in this lab |
|---|---|
| IIS on a Windows server | A container on ECS Fargate (no server to patch) |
| Load balancer appliance | Application Load Balancer |
| PostgreSQL server | Amazon RDS for PostgreSQL |
| Network and firewall | VPC, subnets and security groups |
| Password in the connection string in a config file | AWS Secrets Manager |

What is in this folder:

| Path | What it is |
|---|---|
| `app/` | The ASP.NET Core app and its `Dockerfile` |
| `tests/` | xUnit tests for the app |
| `compose.yaml` | Runs the app and PostgreSQL on your PC |
| `infra/` | The AWS CDK app (Python): one stack, `CompanyApp-Lab` |

## Cost

> **About $2 per day while it is deployed** (about 8 cents per hour). Week 5 costs nothing: it all runs on your PC.
>
> | Part | Per day (Tokyo and Sydney are about the same) |
> |---|---|
> | Application Load Balancer | about $0.60 |
> | RDS `db.t4g.micro`, single-AZ | about $0.60 |
> | Fargate task, 0.25 vCPU and 0.5 GB | about $0.35 |
> | 3 public IPv4 addresses (2 for the load balancer, 1 for the task) | about $0.36 |
> | Disk, secret, logs | a few cents |
>
> These are estimates. The bill is the truth: check **Billing and Cost Management > Bills** the next day.
>
> **Rule: destroy at the end of every session (Lesson 7).** A forgotten lab costs about $60 a month. Deploying again takes about 15 minutes.
>
> On the Free plan this is paid from your credits: USD 100 lasts about 50 days of a forgotten lab (see **Billing and Cost Management > Credits**). On the Paid plan, create the budget alert first ([Playbook 01, step 6](../../docs/playbook/01-account-guardrails.md#6-create-a-budget-alert)).

## Week 5: containers on your PC

### Lesson 1: Install Rancher Desktop

Rancher Desktop gives you the `docker` command on Windows. It runs a small Linux machine in WSL 2, and your containers run inside it. It is free, also for companies.

1. Open PowerShell **as administrator** (right-click **Start** > **Terminal (Admin)**). Install WSL without a Linux distribution (Rancher Desktop brings its own):

```powershell
wsl --install --no-distribution
```

2. Restart the PC.

3. Open a normal PowerShell window and install Rancher Desktop. Accept the Windows prompt if one appears.

```powershell
winget install --id SUSE.RancherDesktop -e
```

4. Start **Rancher Desktop** from the Start menu. On the first start:
   - Untick **Enable Kubernetes**. You do not need it, and it uses a lot of memory.
   - Container engine: **dockerd (moby)**. This gives you the `docker` command.
   - If it asks how to set PATH: **Automatic**.

   Wait until the progress bar at the bottom of the window is gone. The first start takes a few minutes.

5. Open a **new** PowerShell window (older windows do not know the `docker` command yet) and run:

```powershell
docker run --rm hello-world
```

Expected (shortened):

```text
Hello from Docker!
This message shows that your installation appears to be working correctly.
```

Docker downloaded a tiny image, started a container from it, and the container printed this text. `--rm` deletes the container when it ends.

### Lesson 2: Run the app with Docker Compose

`compose.yaml` describes two containers: `db` (PostgreSQL 17) and `app` (this app). Open PowerShell in the repository root (for example `C:\src\expenseflow`).

```powershell
cd labs\company-app
```

```powershell
docker compose up --build
```

Docker builds the app image from `app\Dockerfile` and starts PostgreSQL. It waits until the database is ready, then starts the app. The app creates its table (an EF Core migration) and starts listening. The first run downloads the base images and takes a few minutes.

Expected, among many other lines (you also see the SQL that EF Core sends):

```text
app-1  |       Applying migration '20261030020638_InitialCreate'.
app-1  |       Now listening on: http://[::]:8080
```

Two `warn:` lines from `Microsoft.AspNetCore.DataProtection` are expected: each container keeps its own keys (see the anti-forgery row in [Lab shortcuts vs production](#lab-shortcuts-vs-production)).

Open <http://localhost:8080>:

- The page shows **IT equipment** and **No assets yet.**
- The footer shows `Served by container: 1a2b3c4d5e6f` (the container's ID) and `Database host: db`.

Click **Add asset**. Enter a Name (for example `ThinkPad T14`) and a Category (`Laptop`), then click **Save**. Add two more. Try **Edit** and **Delete** too.

**Do the data survive?** Press **Ctrl+C** in PowerShell. The containers stop. Now remove them:

```powershell
docker compose down
```

Expected: `Container company-app-app-1 Removed` and `Container company-app-db-1 Removed`. The containers are gone, but the volume is not:

```powershell
docker volume ls
```

Expected: a line with `company-app_pgdata`. Start again:

```powershell
docker compose up
```

Refresh the browser. Your assets are still there, but the container ID in the footer is new. New containers, same data: the data lives in the volume, outside the containers. Press **Ctrl+C**, then delete everything, the volume too:

```powershell
docker compose down -v
```

Expected: also `Volume company-app_pgdata Removed`. The next `docker compose up` starts with an empty list.

On AWS it is the same idea: ECS replaces containers often, and the data lives in RDS.

### Lesson 3: Read the Dockerfile

Open `app\Dockerfile`. It has two stages. Only the last stage becomes the image.

| Line | What it does |
|---|---|
| `FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build` | Stage 1 starts from the big SDK image, which has the compiler. |
| `COPY CompanyApp.csproj .` and `RUN dotnet restore` | Downloads the NuGet packages. Docker caches this step, so a code change does not download them again. |
| `COPY . .` and `RUN dotnet publish ...` | Compiles the app into `/app`. |
| `FROM mcr.microsoft.com/dotnet/aspnet:10.0` | Stage 2 starts fresh from the small ASP.NET Core runtime image. |
| `COPY --from=build /app .` | Takes only the published output from stage 1. |
| `USER $APP_UID` | Runs the app as the non-root user `app`, which is built into the image. |
| `ENTRYPOINT ["dotnet", "CompanyApp.dll"]` | Starts the app, like on your PC. |

Why the final image has no SDK:

- **Smaller:** faster to push to ECR and faster to start on Fargate.
- **Safer:** no compiler and no source code inside for an attacker to use.
- Stage 1 is thrown away. Only what `COPY --from=build` takes survives.

`app\.dockerignore` keeps your local `bin\` and `obj\` folders out of the build. See the image size:

```powershell
docker image ls company-app-app
```

Expected: one line with a size of a few hundred MB. The SDK image alone is close to 1 GB.

See the non-root user. Start the app in the background (`-d`: no logs in the window):

```powershell
docker compose up -d
```

```powershell
docker compose exec app whoami
```

Expected: `app`. A non-root user may not open ports below 1024. This is why the app listens on 8080, not 80. Stop again:

```powershell
docker compose down
```

Optional, if you have the .NET SDK (10 or newer): run the unit tests in this folder.

```powershell
dotnet test
```

Expected: a summary with 10 tests and 0 failed. One test checks that `/health` answers without a database.

## Week 6: the same app on AWS

### Lesson 4: Deploy

You need the Week 3 setup: the AWS CLI, the `expenseflow` profile, the repository `.venv`, Node.js, and the CDK bootstrap in your Region.

1. Start Rancher Desktop: `cdk deploy` builds the image with Docker. Check that Docker answers:

```powershell
docker ps
```

Expected: a header line `CONTAINER ID   IMAGE ...` and no error.

2. Sign in and choose profile and Region, like in Week 3 ([deploy guide](../../docs/expenseflow/deploy.md), step 1). Use the Region you deployed ExpenseFlow to: here Sydney (`ap-southeast-2`), the fixed home Region of an account from the new sign-up flow. (The deploy guide's examples use Tokyo, `ap-northeast-1`.) Open PowerShell in the repository root:

```powershell
aws login --profile expenseflow
```

```powershell
$env:AWS_PROFILE = 'expenseflow'
```

```powershell
$env:AWS_REGION = 'ap-southeast-2'
```

Copy the temporary credentials into this PowerShell window, so the CDK and Docker steps use them as they are:

```powershell
aws configure export-credentials --format powershell | Invoke-Expression
```

```powershell
aws sts get-caller-identity
```

Expected: your account number and your user's ARN, not `:root`. The credentials expire after a while: if a later command says *expired*, repeat this step from `aws login`.

3. Activate the virtual environment. The CDK starts the app with plain `python app.py`, so the right Python must come first.

```powershell
.\.venv\Scripts\Activate.ps1
```

Expected: the prompt starts with `(.venv)`.

4. Go to the CDK app and install its tools:

```powershell
cd labs\company-app\infra
```

```powershell
pip install -r requirements.txt
```

Expected: `Requirement already satisfied` (the same versions as Week 3).

```powershell
npm ci
```

Expected: `added 1 package` (the CDK command-line tool).

5. Deploy:

```powershell
npx cdk deploy
```

The CDK builds the image with Docker and pushes it to ECR. It shows a table of security changes: IAM roles, and security group rules such as "port 80 open to `0.0.0.0/0`" for the load balancer. It asks `Do you wish to deploy these changes (y/n)?`. Read the table, then type `y`.

It takes about **15 minutes**. RDS is the slowest part. Expected at the end:

```text
Outputs:
CompanyApp-Lab.AppUrl = http://Compan-Servi-xxxxxxxxxxxx-1234567890.ap-southeast-2.elb.amazonaws.com
```

6. Open the `AppUrl`. It is the same app, with an empty list. Add two assets. The footer shows a container name that Fargate chose (for example `ip-10-0-1-23`) and a database host ending in `.rds.amazonaws.com`.

**From now on the lab costs about 8 cents an hour. Stopping for today? Do Lesson 7 now.**

New PowerShell window later? Repeat steps 2 and 3, then `cd labs\company-app\infra`.

### Lesson 5: Tour the console

**Look, don't touch: CDK is the steering wheel.** If you change something by hand, the next `cdk deploy` can undo it or fail, and `cdk destroy` can leave pieces behind. Change the code and deploy instead.

Check the Region at the top right of the console first. Then:

1. **Elastic Container Service (ECS) > Clusters > CompanyApp-Lab-Cluster... > Services > CompanyApp-Lab-Service... > Tasks >** click the task **> Logs.** The same log lines as on your PC. This is the first place to look when something is wrong.
2. **Aurora and RDS > Databases > companyapp-lab-database...** Tab **Connectivity & security**: the endpoint is the "Database host" from the footer, and **Publicly accessible** is **No**. Tab **Configuration**: `db.t4g.micro`, PostgreSQL 17.
3. **Secrets Manager > Secrets > DbSecret...** The database user name and the generated password. When a container starts, ECS reads them and passes them in as `DB_USER` and `DB_PASSWORD`. Do not click **Retrieve secret value** while you share your screen.
4. **EC2 > Load balancers > Compan-Servi-...** Its DNS name is your `AppUrl`, and it listens on HTTP:80. Then **EC2 > Target groups >** the target group (the list of containers the load balancer sends requests to) **> Targets**: one target, **Healthy**. Tab **Health checks**: path `/health`. This page answers without asking the database, so a database problem does not make ECS replace the containers.
5. **VPC > Security groups.** Ignore the groups named `default` (one per VPC). The other three form a chain. Open each one's **Inbound rules**:

   | Name contains | Allows in |
   |---|---|
   | `ServiceLBSecurityGroup` (load balancer) | port 80 from `0.0.0.0/0`, everyone |
   | `ServiceSecurityGroup` (containers) | port 8080, only from the load balancer's group |
   | `DatabaseSecurityGroup` (database) | port 5432, only from the containers' group |

6. **Elastic Container Registry (ECR) > Private registry > Repositories > cdk-hnb659fds-container-assets-...** The image that CDK built on your PC. Its tag is a long hash. This repository belongs to the CDK bootstrap, and all your CDK apps share it.

### Lesson 6: Break it

**A. Stop the container.**

1. **ECS > Clusters > CompanyApp-Lab-Cluster... > Services > CompanyApp-Lab-Service... > Tasks.** Tick the running task, choose **Stop > Stop selected**, and confirm. (This is allowed: you do not change the setup, and ECS repairs it.)
2. Refresh the app a few times. Expected: `503 Service Temporarily Unavailable`. No container is behind the load balancer.
3. Refresh the **Tasks** tab. ECS starts a new task by itself, because the service wants 1 running. After 1-2 minutes the app works again. Your assets are still there (they are in RDS), and the container name in the footer is new.

**B. Run two containers.**

```powershell
npx cdk deploy -c desiredCount=2
```

This time there is no security question: nothing new is allowed. It takes a few minutes. Then refresh the app several times. `Served by container` switches between two names, and `Database host` stays the same. The load balancer sends each request to one of the healthy containers.

The CDK does not remember `-c`: a plain `npx cdk deploy` goes back to 1 container. A second container costs about $0.50 per day more.

**The lab still costs about 8 cents an hour. Stopping for today? Do Lesson 7 now.**

### Lesson 7: Clean up

New PowerShell window? First repeat Lesson 4 steps 2 and 3, then `cd labs\company-app\infra`. Use the same Region you deployed to.

```powershell
npx cdk destroy
```

It asks `Are you sure you want to delete: CompanyApp-Lab (y/n)?`. Type `y`. It takes about 10 minutes. Expected at the end: `CompanyApp-Lab: destroyed`.

The database is deleted **without a final snapshot**, so your test assets are gone. In a lab that is what we want: a snapshot would keep costing money.

Check that nothing is left:

```powershell
aws cloudformation describe-stacks --stack-name CompanyApp-Lab
```

Expected: an error, `Stack with id CompanyApp-Lab does not exist`. Here the error is good news.

```powershell
aws rds describe-db-instances --query "DBInstances[].DBInstanceIdentifier"
```

Expected: no `companyapp-lab-database...` (`[]` if you have no other databases).

In the console: **CloudFormation > Stacks** has no `CompanyApp-Lab`, and **Aurora and RDS > Databases** is empty.

What stays: the image in the CDK's ECR repository (a few cents per month) and the `CDKToolkit` stack from Week 3. Keep both.

Write your [LEARNING_LOG.md](../../LEARNING_LOG.md) entry while it is fresh.

## Lab shortcuts vs production

| Lab | Production | Why the lab does it |
|---|---|---|
| HTTP only | HTTPS on the load balancer | HTTPS needs a domain name and an ACM certificate. |
| Containers in public subnets with a public IP | Private subnets with a NAT gateway | A NAT gateway costs about as much as the whole lab. |
| 1 container | At least 2, in 2 Availability Zones | Cost. |
| Single-AZ database | Multi-AZ database | Half the price. A lab can wait while the database restarts. |
| Database deleted without a snapshot | Final snapshot and deletion protection | No leftovers that keep costing. |
| Migrations run when the app starts | Migrations as a separate deployment step | Simple. A failed migration should stop the release, not every container start. |
| `SslMode=Prefer` | `SslMode=VerifyFull` with the RDS certificate bundle | The same setting works with the local database, which has no TLS. |
| Form anti-forgery check switched off | Check on, key shared by all containers | The app has no sign-in. Each container has its own key, so a form posted to the other container would fail. |
| No WAF | AWS WAF in front of the load balancer | Cost and setup time. |

## Troubleshooting

**`docker` is not recognized** (`CommandNotFoundException`; on Japanese Windows `認識されません`)

The PowerShell window was opened before Rancher Desktop was installed. Open a new window. Still not found? Start Rancher Desktop once and wait until it is ready.

**`error during connect` or `Cannot connect to the Docker daemon`**

Rancher Desktop is not running or is still starting. Start it and wait until the progress bar is gone. In **Preferences > Container Engine**, the engine must be **dockerd (moby)**.

**`timed out dialing Hyper-V socket`** (Rancher Desktop 1.23 and 1.24; `docker.log` fills with `bad file descriptor`)

A bug in Rancher Desktop's own health check: its `curl` rejects the address `http://./_ping`, so about a minute after every start Rancher Desktop cuts the connection between Windows and Docker. Docker itself is fine. The maintainers' workaround is a startup script that fixes the address (fixed for real in the next release, [issue #10739](https://github.com/rancher-sandbox/rancher-desktop/issues/10739)):

```powershell
"#!/bin/sh`nsed -i 's|http://\./_ping|http://localhost/_ping|g' /etc/init.d/docker`n" | Set-Content -NoNewline -Encoding ascii "$env:LOCALAPPDATA\rancher-desktop\provisioning\fix-docker-healthcheck.start"
```

Then quit Rancher Desktop (**File > Quit**) and start it again. Once a version with the fix is installed, delete the script.

**Port 8080 is already in use** (`port is already allocated` or `ports are not available`)

Another program uses port 8080. Find it:

```powershell
Get-NetTCPConnection -LocalPort 8080 -State Listen
```

The `OwningProcess` column is the process ID: close that program. Or, in `compose.yaml`, change `"8080:8080"` to `"8081:8080"` and open <http://localhost:8081>. The left number is your PC's port, the right one is the container's.

**`ExpiredToken`, `Your session has expired` or `The security token included in the request is expired`**

Sign in again in the same window, then run the command again:

```powershell
aws login --profile expenseflow
```

**`cdk deploy`: `Has the environment been bootstrapped?`**

Check `$env:AWS_REGION`. If the Region is right but new for you, bootstrap it once:

```powershell
npx cdk bootstrap
```

**`not available on the Free plan`**

See the common errors at the end of [Playbook 01](../../docs/playbook/01-account-guardrails.md).

**`cdk deploy` cannot reach Docker** (`docker exited with error code 1`, `error during connect` or `spawn docker ENOENT`)

Start Rancher Desktop, check with `docker ps`, then run `npx cdk deploy` again.

**`cdk deploy` waits a long time, or the service keeps starting new tasks**

The container crashes or fails the health check. Open **ECS > Clusters > CompanyApp-Lab-Cluster... > Services > CompanyApp-Lab-Service... > Tasks**, set the filter to **Stopped**, and click a task. Read **Stopped reason**, then the **Logs** tab. Typical messages:

- `The environment variable DB_HOST is not set.`: a setting is missing in the task definition.
- A timeout connecting to the database on port 5432: check the security group chain (Lesson 5).
- `CannotPullContainerError` or `ResourceInitializationError`: the task cannot reach ECR or Secrets Manager. It needs its public IP.

After several failed starts, ECS gives up and `cdk deploy` fails with the reason. On an update, ECS goes back to the last working version. On the first deploy there is none, so CloudFormation deletes what it created. Fix the cause and run `npx cdk deploy` again.

**`dotnet test`: `You must install or update .NET to run this application`**

Your PC has only a newer .NET SDK. Let the tests run on it, in this window only:

```powershell
$env:DOTNET_ROLL_FORWARD = 'Major'
```
