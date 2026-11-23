"""The whole lab in one stack: the company app on ECS Fargate behind a load balancer, with RDS PostgreSQL.

    browser -> Application Load Balancer   public subnets, port 80
            -> app containers (Fargate)    public subnets, port 8080
            -> RDS for PostgreSQL          isolated subnets, port 5432

The VPC has NO NAT gateway: one costs about as much as everything else in this stack together.
Without it, a container in a private subnet could not reach ECR to pull its image, so the containers
run in the public subnets with a public IP. Their security group still accepts traffic from the load
balancer only. The database sits in isolated subnets, which have no route to the internet at all.

Cost while deployed: about $2/day (load balancer, db.t4g.micro, one small container, public IPv4
addresses). `npx cdk destroy` deletes everything, the database included, without a final snapshot.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from constructs import Construct

POSTGRES_PORT = 5432  # PostgreSQL's standard port, which RDS uses unless told otherwise


class LabStack(Stack):
    """Network, database and container service of the company app.

    Context: `-c desiredCount=N` sets how many app containers run (default 1). With 2, reload the
    page and watch the footer alternate between the two container names.
    """

    def __init__(self, scope: Construct, construct_id: str, *, app_dir: Path) -> None:
        """Create the stack.

        Args:
            app_dir: The app's Docker build context (the directory with its Dockerfile).
        """
        super().__init__(
            scope,
            construct_id,
            description="Company app lab: ECS Fargate behind a load balancer, with RDS PostgreSQL",
        )
        desired_count = int(self.node.try_get_context("desiredCount") or 1)

        # Two AZs because a load balancer and an RDS subnet group both require subnets in at least two.
        # The stack has no fixed Region (see app.py), so CloudFormation picks the AZs (Fn::GetAZs).
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            # Nothing in this lab uses the VPC's default security group, so there is nothing to lock
            # down. Locking it needs a helper Lambda whose log group `cdk destroy` would leave behind.
            restrict_default_security_group=False,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC),
                ec2.SubnetConfiguration(name="Database", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            ],
        )

        # The password never appears in code or in the template: Secrets Manager generates it, RDS
        # uses it as the master password and ECS hands it to the containers when they start.
        db_secret = rds.DatabaseSecret(self, "DbSecret", username="companyapp")
        database = rds.DatabaseInstance(
            self,
            "Database",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_17),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
            allocated_storage=20,
            # Free with the AWS managed key, and CDK's built-in template checks warn without it.
            storage_encrypted=True,
            multi_az=False,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            publicly_accessible=False,
            database_name="companyapp",
            credentials=rds.Credentials.from_secret(db_secret),
            # The shortest retention that keeps automated backups (and point-in-time restore) on.
            backup_retention=Duration.days(1),
            # Lab data only: `cdk destroy` must not leave a snapshot or backups behind that keep
            # costing money. The default (SNAPSHOT) is the right choice for real data.
            removal_policy=RemovalPolicy.DESTROY,
            delete_automated_backups=True,
        )

        # With Fargate a cluster is only a named group of services: there are no servers to manage.
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)
        log_group = logs.LogGroup(
            self, "AppLogs", retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY
        )
        # The pattern wires up the usual trio: an internet-facing load balancer with an HTTP listener,
        # a target group, and a Fargate service whose containers register in that target group.
        # Plain HTTP because HTTPS needs a domain name and a certificate.
        service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "Service",
            cluster=cluster,
            cpu=256,
            memory_limit_mib=512,
            # Must match the image: it is built on the learner's x64 PC.
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            desired_count=desired_count,
            # Without a NAT gateway, a public IP is how the containers reach ECR (to pull the image),
            # Secrets Manager (for the database password) and CloudWatch Logs.
            task_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            assign_public_ip=True,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                # `cdk deploy` runs `docker build` on this directory and pushes the image to the ECR
                # repository that `cdk bootstrap` created.
                image=ecs.ContainerImage.from_asset(str(app_dir), platform=ecr_assets.Platform.LINUX_AMD64),
                container_port=8080,
                environment={
                    "DB_HOST": database.db_instance_endpoint_address,
                    "DB_PORT": str(POSTGRES_PORT),
                    "DB_NAME": "companyapp",
                },
                secrets={
                    "DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
                    "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
                },
                log_driver=ecs.LogDrivers.aws_logs(stream_prefix="app", log_group=log_group),
            ),
            # A new version whose containers never become healthy is rolled back automatically
            # instead of being retried for hours.
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            # Old containers keep serving until their replacements pass the health check.
            min_healthy_percent=100,
        )
        # /health does not touch the database. If it did, a short database outage would make every
        # container fail the check, and ECS would replace them all without fixing anything.
        service.target_group.configure_health_check(path="/health")
        # How long the load balancer lets a stopping container finish open requests. The default,
        # 300 seconds, makes every deployment and `cdk destroy` five minutes slower.
        service.target_group.set_attribute("deregistration_delay.timeout_seconds", "30")

        database.connections.allow_from(
            service.service, ec2.Port.tcp(POSTGRES_PORT), "PostgreSQL from the app containers only"
        )

        # The pattern adds two outputs of its own under generated names (ServiceLoadBalancerDNS...,
        # ServiceServiceURL...); remove them so that `cdk deploy` prints just AppUrl.
        for output_id in ("LoadBalancerDNS", "ServiceURL"):
            service.node.try_remove_child(output_id)
        CfnOutput(
            self,
            "AppUrl",
            value=f"http://{service.load_balancer.load_balancer_dns_name}",
            description="Open this URL to use the app",
        )
