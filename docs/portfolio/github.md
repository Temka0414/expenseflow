# Portfolio guide: show your work on GitHub

ExpenseFlow can be a portfolio project, **if you can explain it**. A hiring manager does not need a perfect app. They want proof that you can build, deploy, test and talk about a cloud system. This guide turns the repository into that proof.

The path:

1. Review the code until you can explain it.
2. Make the first commit and push it to your GitHub.
3. Turn on the live demo (GitHub Pages).
4. Learn one lesson at a time: issue, branch, pull request, CI, merge, learning log.
5. Take good screenshots and polish the README.
6. Write an article for Zenn or Qiita.

## 1. Review the code before you publish it

Your name goes on this repository. Read it first.

| Read | Why |
|---|---|
| `infra/app.py`, then one stack, for example `infra/expenseflow_infra/stacks/data_stack.py` | How the infrastructure is built |
| `backend/src/handlers/api/routes_expenses.py` | How a request becomes a database write |
| `backend/src/handlers/stream_processor.py` and `infra/expenseflow_infra/constructs/approval_workflow.py` | The event-driven core |
| One [pattern page](../patterns/01-cognito-auth.md) per day | The idea behind each service |

Check these before the first push:

- **No secrets.** The repository stores no AWS keys by design (CI uses OIDC), and `frontend\public\config.json` is ignored by Git on purpose. Step 2 has a one-line search that proves it.
- **Nothing private.** Your account ID, email address and API URLs are not secrets that break security, but do not publish them without a reason. The `.gitignore` already skips the scratch files of the build process and the IDE folders (`.vs`, `.idea`).
- **Be honest about how it was built.** If an AI assistant helped write it, say so in the README. Then make sure *you* can answer questions about it. "I used AI to build it, then I deployed it, broke it and fixed it. Here is what I learned" is a strong story. "I wrote all of it" followed by "I do not know how this part works" is a weak one.

## 2. First commit and push

Set your name and an email once. Use GitHub's private "noreply" address if you do not want your real email in commits (**GitHub > Settings > Emails > Keep my email addresses private** shows it):

```powershell
git config --global user.name "Your Name"
git config --global user.email "<id>+<your-github-user>@users.noreply.github.com"
```

Create the repository and push. With the GitHub CLI (`gh auth login` first):

```powershell
cd C:\src\expenseflow
git init -b main
git add .
git status
git grep --cached -n -E "AKIA[0-9A-Z]{16}|BEGIN (RSA |EC )?PRIVATE KEY"
git commit -m "Initial commit: ExpenseFlow"
gh repo create expenseflow --public --source . --remote origin --push
```

Read the `git status` output before you commit. Does the list look right? `node_modules`, `.venv` and `cdk.out` must **not** be in it. The `git grep` line searches the files you are about to commit for AWS access key IDs and private keys. It must print nothing.

Without `gh`: create an empty repository on <https://github.com/new>, then follow the "push an existing repository" commands that GitHub shows.

**Public or private?** GitHub Pages (step 3) needs a **public** repository on a free GitHub account. A portfolio should be public anyway.

Then open the repository on GitHub and check:

- **Actions** tab: the `CI` workflow runs. It should turn green.
- The README renders, and the images show.

## 3. Turn on the live demo (GitHub Pages)

The repository has a workflow, `.github/workflows/pages.yml`, that builds the **demo mode** of the app and publishes it. The demo runs the full UI against an in-memory fake backend in the browser, so it needs **no AWS account** and costs nothing.

1. **Settings > Pages > Build and deployment > Source: GitHub Actions**.
2. **Actions > Pages > Run workflow** (or just push to `main`).
3. When it is green, your demo is at `https://<your-github-user>.github.io/expenseflow/`.
4. Put that link in the root `README.md` (search for `<your-github-user>`), and in **repository > About (gear icon) > Website**.

How it works, in short:

- The site lives below `/<repo-name>/`, not at the domain root. The build sets `VITE_BASE=/<repo-name>/`, and the router in demo mode uses the same base (`import.meta.env.BASE_URL`).
- GitHub Pages has no "single-page app" fallback. It serves `404.html` for unknown paths. The workflow copies `index.html` to `404.html`, so reloading `/expenseflow/expenses` still opens the right page.
- Every push to `main` republishes it.

Test the Pages build on your PC:

```powershell
cd frontend
$env:VITE_BASE = '/expenseflow/'
npm run build:pages
npx vite preview --mode mock --outDir dist-pages
```

Open <http://localhost:4173/expenseflow/>. Remove the variable afterwards: `Remove-Item Env:VITE_BASE`.

## 4. The lesson loop: one lesson, one pull request

Every [pattern page](../patterns/01-cognito-auth.md) ends with a "Your turn" challenge sized as **one pull request**. Do them one at a time. Each lesson leaves a trail on GitHub that shows how you work.

| Step | What you do | Where |
|---|---|---|
| 1. Issue | Open a new issue with the **Lesson** template: what you want to learn and the small change you will make. | GitHub > Issues > New issue |
| 2. Branch | `git switch -c lesson/04-receipt-size-limit` | terminal |
| 3. Change | Make the change. Run the tests you touched ([phase 5](../playbook/05-build-test-locally.md)). | editor |
| 4. Commit | `git add -A`, then `git commit -m "Reject receipts over 2 MB"` | terminal |
| 5. Push | `git push -u origin lesson/04-receipt-size-limit` | terminal |
| 6. Pull request | `gh pr create --fill`, or use the link that `git push` prints. Fill the template. Write `Closes #<issue number>`. | GitHub |
| 7. CI | Wait for the checks to turn green. Fix and push again if one is red. | GitHub > Checks |
| 8. Learning log | Add an entry to [LEARNING_LOG.md](../../LEARNING_LOG.md) **in the same pull request**. | editor |
| 9. Merge | **Squash and merge**, then delete the branch. | GitHub |
| 10. Update | `git switch main; git pull` | terminal |

If you deployed the change to AWS, run `python scripts\smoke_test.py` and say so in the pull request. If you did not deploy, write "not deployed". Never claim a check you did not run.

Rules that keep it clean:

- One lesson per pull request. If it grows, split it.
- Small commits with clear messages: what and why.
- Turn on branch protection for `main` after the first week ([phase 9](../playbook/09-cicd.md), step 5).

## 5. Take good screenshots

The README shows four screenshots from the **demo mode**. Demo mode has realistic seed data, no sign-in, and a switch for the three roles.

```powershell
cd frontend
npm run dev:mock
```

Open <http://localhost:5173> and follow this routine:

1. Use a **window of about 1280 x 800**. Resize the browser, or press F12 and use the device toolbar.
2. Hide the bookmarks bar. Close other tabs. Use the same light or dark setting for all of them (the app follows your Windows setting), so the gallery looks consistent.
3. Capture with **Win + Shift + S** (Snipping Tool). Choose the browser window or a rectangle.
4. Save as **PNG** into `docs\images\screenshots\` with these names:

| File | Show |
|---|---|
| `dashboard.png` | Dashboard as an employee, with tiles and recent expenses |
| `expense-ai.png` | A new expense with the AI suggestion panel after a sample receipt |
| `approvals.png` | The approvals page as a manager |
| `under-the-hood.png` | The **Under the hood** page |

5. Keep each file **under about 500 KB**. A big screenshot slows the README. Crop to the content.

For **AWS console** screenshots (the `<!-- screenshot: ... -->` placeholders in the docs): capture the page, then **cover** the 12-digit account ID, email addresses and any ARN that contains them, with a black box (Snipping Tool's pen or Paint). Never blur lightly: a weak blur can be undone.

Name console screenshots after the click path, for example `cfn-outputs.png`, and save them in `docs\images\screenshots\`. Then replace the HTML comment with a normal Markdown image that points to the file, for example `../images/screenshots/cfn-outputs.png` (use one `../` or two, depending on the folder of the page you are editing).

## 6. Polish the README

Open the root [README.md](../../README.md) and check:

- [ ] `<your-github-user>` replaced everywhere (CI badge, demo link, clone URL)
- [ ] The first paragraph says **what it is and why it matters** in plain words
- [ ] The CI badge is green, the demo link works
- [ ] Four screenshots show
- [ ] "How this was built" is honest
- [ ] The **What I learned** section is filled in with *your* sentences
- [ ] The cost note and the teardown command are there (a reviewer who deploys your project will thank you)
- [ ] On GitHub: **About > Description**, **Website** (demo link) and **Topics**, for example `aws`, `cdk`, `serverless`, `step-functions`, `bedrock`, `react`
- [ ] The repository is **pinned** on your GitHub profile

## 7. Write the article (Zenn or Qiita)

When you have done several lessons, turn [LEARNING_LOG.md](../../LEARNING_LOG.md) into one article. Pick **one story**, not a tour of everything.

Outline:

1. **Why I built it.** Your background (.NET developer), the goal (learn AWS by building something real), one sentence on the result.
2. **What it does.** The demo link and one screenshot. The architecture diagram.
3. **The design in three ideas.** For example: events instead of direct calls, a human-approval workflow that waits for free, one DynamoDB table.
4. **Three things that went wrong.** Take them from your learning log: the error message, what you tried, what fixed it. This is the part readers remember.
5. **One deep dive.** One [pattern](../patterns/08-task-token-approvals.md) explained with a diagram and a few lines of code.
6. **Cost and numbers.** What it costs idle, what one receipt costs.
7. **What I would do next.** Honest limits (see the production checklist in [phase 10](../playbook/10-teardown-prod.md)).
8. **Links.** Repository, live demo, the docs.

Practical notes:

- **Zenn** can publish articles straight from a GitHub repository (Markdown files plus its CLI). **Qiita** accepts pasted Markdown and up to five tags. Read each site's current guide for the exact steps.
- Write in the language of the readers you want. A Japanese article for Japanese employers is a strong signal.
- Do not paste secrets or account IDs. Link to your repository instead of pasting long code.
- Mention that the demo runs without AWS, so readers can try it in one click.

## Interview preparation

Each [pattern page](../patterns/01-cognito-auth.md) ends with interview questions. Before an interview, be ready to explain, out loud and without notes:

- why the app uses events and a workflow instead of one big function,
- what happens when a message is delivered twice,
- how the app is deployed without an access key,
- what the app costs, and how you would find out.
