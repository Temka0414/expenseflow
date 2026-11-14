#!/usr/bin/env python3
"""Entry point of the lab's CDK app (`cdk.json` runs `python app.py`).

`npx cdk synth` runs this script, which writes the CloudFormation template to `cdk.out/`.
`npx cdk deploy` then creates the stack in AWS and `npx cdk destroy` deletes it again.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import App

from company_app_infra.lab_stack import LabStack

APP_DIR = Path(__file__).resolve().parent.parent / "app"

app = App()
# No `env=`: the stack is environment-agnostic, so `cdk synth` works without AWS credentials and
# `cdk deploy` uses the account and Region of the credentials the learner is signed in with.
LabStack(app, "CompanyApp-Lab", app_dir=APP_DIR)
app.synth()
