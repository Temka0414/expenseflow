"""CompanyApp-Lab stack: cost guards, network isolation of the database and the app's contract.

The stack is built from a stand-in app directory with a one-line Dockerfile: synth only copies the
build context into cdk.out, so the tests never depend on the real app (Docker runs at deploy time).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App
from aws_cdk.assertions import Match, Template

from company_app_infra.lab_stack import LabStack

CDK_JSON = Path(__file__).resolve().parent.parent / "cdk.json"


def _synth(tmp_path_factory: pytest.TempPathFactory, **context: object) -> Template:
    app_dir = tmp_path_factory.mktemp("app")
    (app_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    # The cdk.json feature flags, so the tests see the same template as `cdk synth`. A short
    # output directory keeps the copied build context below the Windows path limit.
    flags: dict[str, Any] = json.loads(CDK_JSON.read_text(encoding="utf-8"))["context"]
    app = App(context={**flags, **context}, outdir=str(tmp_path_factory.mktemp("cdk")))
    return Template.from_stack(LabStack(app, "CompanyApp-Lab", app_dir=app_dir))


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Template:
    return _synth(tmp_path_factory)


def _logical_id(template: Template, resource_type: str, id_prefix: str) -> str:
    matches = [name for name in template.find_resources(resource_type) if name.startswith(id_prefix)]
    assert len(matches) == 1, f"expected one {resource_type} {id_prefix}*, found {matches}"
    return matches[0]


def test_vpc_has_no_nat_gateway(template: Template) -> None:
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::EC2::EIP", 0)


def test_database_is_private_small_and_deleted_with_the_stack(template: Template) -> None:
    template.has_resource(
        "AWS::RDS::DBInstance",
        {
            "Properties": Match.object_like(
                {
                    "Engine": "postgres",
                    "EngineVersion": "17",
                    "DBInstanceClass": "db.t4g.micro",
                    "AllocatedStorage": "20",
                    "StorageEncrypted": True,
                    "MultiAZ": False,
                    "PubliclyAccessible": False,
                    "DBName": "companyapp",
                    "BackupRetentionPeriod": 1,
                    "DeleteAutomatedBackups": True,
                }
            ),
            "DeletionPolicy": "Delete",
            "UpdateReplacePolicy": "Delete",
        },
    )


def test_database_accepts_postgres_only_from_the_app_containers(template: Template) -> None:
    db_sg = _logical_id(template, "AWS::EC2::SecurityGroup", "DatabaseSecurityGroup")
    service_sg = _logical_id(template, "AWS::EC2::SecurityGroup", "ServiceSecurityGroup")

    db_sg_properties = template.find_resources("AWS::EC2::SecurityGroup")[db_sg]["Properties"]
    assert "SecurityGroupIngress" not in db_sg_properties
    rules = template.find_resources(
        "AWS::EC2::SecurityGroupIngress", {"Properties": {"GroupId": {"Fn::GetAtt": [db_sg, "GroupId"]}}}
    )
    assert [rule["Properties"] for rule in rules.values()] == [
        {
            "GroupId": {"Fn::GetAtt": [db_sg, "GroupId"]},
            "SourceSecurityGroupId": {"Fn::GetAtt": [service_sg, "GroupId"]},
            "IpProtocol": "tcp",
            "FromPort": 5432,
            "ToPort": 5432,
            "Description": "PostgreSQL from the app containers only",
        }
    ]


def test_task_is_small_x86_and_gets_the_database_settings(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "Cpu": "256",
            "Memory": "512",
            "RuntimePlatform": {"CpuArchitecture": "X86_64", "OperatingSystemFamily": "LINUX"},
            "ContainerDefinitions": [
                Match.object_like(
                    {
                        "PortMappings": [Match.object_like({"ContainerPort": 8080})],
                        "Environment": Match.array_with(
                            [
                                {"Name": "DB_HOST", "Value": Match.any_value()},
                                {"Name": "DB_PORT", "Value": "5432"},
                                {"Name": "DB_NAME", "Value": "companyapp"},
                            ]
                        ),
                        "Secrets": [
                            {"Name": "DB_USER", "ValueFrom": Match.any_value()},
                            {"Name": "DB_PASSWORD", "ValueFrom": Match.any_value()},
                        ],
                    }
                )
            ],
        },
    )


def test_load_balancer_checks_health_on_the_database_free_endpoint(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "HealthCheckPath": "/health",
            "TargetGroupAttributes": Match.array_with(
                [{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}]
            ),
        },
    )


def test_service_rolls_back_failed_deployments_and_runs_one_container_by_default(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "DesiredCount": 1,
            "DeploymentConfiguration": Match.object_like(
                {"DeploymentCircuitBreaker": {"Enable": True, "Rollback": True}}
            ),
        },
    )


def test_desired_count_comes_from_context(tmp_path_factory: pytest.TempPathFactory) -> None:
    # `-c desiredCount=2` on the command line arrives as a string.
    template = _synth(tmp_path_factory, desiredCount="2")

    template.has_resource_properties("AWS::ECS::Service", {"DesiredCount": 2})


def test_app_url_is_the_only_output(template: Template) -> None:
    assert list(template.find_outputs("*")) == ["AppUrl"]
    template.has_output(
        "AppUrl",
        {"Value": {"Fn::Join": ["", ["http://", {"Fn::GetAtt": [Match.any_value(), "DNSName"]}]]}},
    )
