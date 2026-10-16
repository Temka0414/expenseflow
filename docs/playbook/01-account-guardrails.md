# Phase 1: Account and guardrails

## Goal

An AWS account that is protected against theft and cannot surprise you with a big bill.

## Why

The **root user** (the email address you sign up with) can do everything and cannot be restricted. If someone steals its password, they own the account. A **budget alert** tells you early if something runs that should not. Set both up *before* you create any resource.

## Exact steps

### 1. Understand the plan you get

AWS accounts created on or after 15 July 2025 start on the **Free plan**. As documented by AWS at the time of writing:

- You get **USD 100 in credits** at sign-up, plus up to USD 100 more for finishing tasks in the console's "Explore AWS" widget.
- The Free plan ends after **6 months or when the credits are used up**, whichever comes first. It cannot be extended.
- Some services and some AWS Marketplace offers are not available on the Free plan.
- After it ends you have **90 days to upgrade to the Paid plan**. If you do not, the account and its content are deleted.
- On the **Paid plan** you can use all services. Usage beyond your credits is billed. Unused credits carry over.
- Some actions upgrade a Free-plan account automatically, for example joining AWS Organizations (this matters in [phase 3](03-identity-credentials.md)).

Older accounts use the previous model (always-free limits and 12-month trials). Check **Billing and Cost Management > Free Tier** to see what your account has.

**In ExpenseFlow:** everything except the AI receipt reader works with the Free plan's services. Claude on Amazon Bedrock is billed through AWS Marketplace. We could not verify that the Free plan allows it, so **plan to upgrade to the Paid plan if you want AI extraction**. To stay on the Free plan, deploy with `-c extractionModelId=none` (receipts are then stored but not read by Claude).

### 2. Sign up

1. Open <https://aws.amazon.com/> and choose **Create an AWS account**.
2. Use an email address that you will keep for years. A shared team mailbox is better than a personal one for a company account.
3. Choose a strong, unique password and store it in a password manager.
4. Add a payment method. Verification may place a small temporary charge.

AWS has also started a **newer sign-up flow** (rolling out from September 2026). According to the AWS documentation:

- Each project lives in **one Region**, chosen from the contact address. For Japan the documentation names `ap-southeast-2` (Sydney), not Tokyo. You cannot pick another.
- You sign in with Google, GitHub, Apple or Amazon.
- IAM users and roles work for programmatic access only. IAM Identity Center and AWS Organizations are available only after you turn on "advanced features".

We have not used this flow. If you got it, write down your Region first and use it everywhere. ExpenseFlow takes the Region from `AWS_REGION` or your profile (see [phase 4](04-iac-bootstrap.md)), so it is not tied to Tokyo. The Region must offer all the services it uses.

**In ExpenseFlow:** the docs assume `ap-northeast-1` (Tokyo) and show where to change it.

<!-- screenshot: console top-right Region menu showing the active Region -->

### 3. Turn on MFA for the root user

1. Sign in as the root user.
2. Click your account name (top right) > **Security credentials**.
3. Under **Multi-factor authentication (MFA)** choose **Assign MFA device**.
4. Pick **Passkey or security key** or **Authenticator app**, and follow the screens.
5. On the same page check that **Access keys** is empty. Never create access keys for the root user.

<!-- screenshot: Security credentials > Multi-factor authentication (MFA) with one assigned device -->

**In ExpenseFlow:** this account will hold the five ExpenseFlow stacks. Protect it before you deploy them.

### 4. Stop using root

From now on, root is only for the few tasks that need it (closing the account, changing the support plan). Everything else uses the admin identity you create in [phase 3](03-identity-credentials.md).

**In ExpenseFlow:** every command in the [deploy guide](../expenseflow/deploy.md) runs as your admin identity, never as root.

### 5. Let your admin identity see the bill

1. Account name (top right) > **Account**.
2. Scroll to **IAM user and role access to Billing information** > **Edit** > **Activate IAM access** > **Update**.

Without this, an administrator that is not root gets "Access denied" on every billing page.

**In ExpenseFlow:** the Budget stack and the Cost Explorer checks in [phase 8](08-operate.md) need this.

### 6. Create a budget alert

Choose one of the two ways. Do not do both, or you get two sets of emails.

**A. In the console (good for the first time, you see every option):**

1. **Billing and Cost Management > Budgets > Create budget**.
2. Choose **Use a template (simplified)** > **Monthly cost budget**.
3. Enter a budget name, an amount such as `10` USD and your email address.
4. **Create budget**.

<!-- screenshot: Billing and Cost Management > Budgets > Create budget (monthly cost budget template) -->

**B. With the infrastructure code** (needs the tools and the bootstrap of phases 2 to 4, so do it later, or use option A now):

**In ExpenseFlow:** the optional Budget stack creates a monthly cost budget with two emails: one when the **actual** cost passes 80 %, and one when the **forecast** passes 100 %. It covers the whole account, not just ExpenseFlow.

```powershell
cd infra
npx cdk deploy ExpenseFlow-Budget -c budgetEmail=you@example.com -c monthlyBudgetUsd=10
```

Good to know about any budget:

- It only **alerts**. It never stops anything.
- Billing data is refreshed a few times a day, so alerts arrive hours late.
- AWS needs about five weeks of history before it can forecast.

### 7. Choose and write down the Region

Pick one Region for the project and use it for every command. Tokyo (`ap-northeast-1`) is close to Japan. Not every service or model is in every Region, so check before you commit. A Region cannot be changed later without redeploying.

**In ExpenseFlow:** Claude on Bedrock is called through a *cross-Region inference profile*. From Tokyo the app uses `jp.anthropic.claude-opus-5-5`, which routes between Tokyo and Osaka. The code derives the right prefix for your Region. See [Bedrock pattern](../patterns/06-bedrock-ai-extraction.md).

## ✅ Verify

- **Security credentials** shows one MFA device and no access keys for root.
- **Budgets** lists your budget. The email address in it is yours.
- **Billing and Cost Management > Bills** opens for your admin user (after phase 3).

## ⚠️ Common errors

| Problem | Fix |
|---|---|
| Account verification fails or stays "pending" | Wait up to a day, then contact AWS Support from the sign-in page. |
| "You are not authorized to view this page" on Billing | Step 5: activate IAM access to billing. |
| The service you want is "not available on the Free plan" | Upgrade to the Paid plan (**Billing and Cost Management > Account**), or switch the feature off. |
| You lost the MFA device | Use the root sign-in page's "Troubleshoot MFA" link. Keep a second device or a passkey as backup. |
| Budget email never arrives | Check spam. Budgets only evaluate after AWS has billing data, which can take a day. |

## 🔁 Repeat for every project

- [ ] Account exists, plan understood (credits, end date)
- [ ] Root MFA on; no root access keys
- [ ] IAM access to billing activated
- [ ] Budget alert created (console or stack), email is yours
- [ ] Region chosen and written in the project README
- [ ] Optional: separate AWS account for production (see [phase 10](10-teardown-prod.md))
