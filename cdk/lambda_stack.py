from __future__ import annotations

import os
import shutil
import subprocess
import sys

import jsii
from aws_cdk import (
    Arn,
    ArnComponents,
    AssetHashType,
    Aws,
    BundlingOptions,
    CfnParameter,
    Duration,
    ILocalBundling,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_s3 as s3
from constructs import Construct


MODEL_ID = 'amazon.nova-lite-v1:0'
@jsii.implements(ILocalBundling)
class LocalPythonBundler:
    """Build a Linux/Python 3.12 Lambda asset without requiring Docker."""

    def __init__(self, source_dir: str, requirements_file: str) -> None:
        self.source_dir = source_dir
        self.requirements_file = requirements_file

    def try_bundle(self, output_dir: str, options: BundlingOptions) -> bool:
        subprocess.run(
            [
                sys.executable,
                '-m',
                'pip',
                'install',
                '--disable-pip-version-check',
                '--no-compile',
                '--platform',
                'manylinux2014_x86_64',
                '--implementation',
                'cp',
                '--python-version',
                '3.12',
                '--abi',
                'cp312',
                '--only-binary=:all:',
                '--target',
                output_dir,
                '-r',
                self.requirements_file,
            ],
            check=True,
        )
        shutil.copytree(
            self.source_dir,
            os.path.join(output_dir, 'src'),
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'),
        )
        return True


class AgentRuntimeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        source_dir = os.path.join(project_root, 'src')
        requirements_file = os.path.join(os.path.dirname(__file__), 'requirements-lambda.txt')

        table_name = CfnParameter(
            self,
            'TradeEventsTableName',
            type='String',
            min_length=1,
            description='Existing persistence-only TradeEvents table name.',
        )
        function_name = CfnParameter(
            self,
            'PaperAgentFunctionName',
            type='String',
            min_length=1,
            max_length=64,
            allowed_pattern=r'^[A-Za-z0-9-_]+$',
            description='Name for a new paper-only Lambda function.',
        )
        table_arn = self.format_arn(
            service='dynamodb',
            resource='table',
            resource_name=table_name.value_as_string,
        )
        model_arn = Arn.format(
            ArnComponents(
                service='bedrock',
                region=Aws.REGION,
                account='',
                resource='foundation-model',
                resource_name=MODEL_ID,
            ),
            self,
        )

        log_group = logs.LogGroup(
            self,
            'PaperAgentLogGroup',
            log_group_name=f'/aws/lambda/{function_name.value_as_string}',
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        role = iam.Role(
            self,
            'PaperAgentRole',
            assumed_by=iam.ServicePrincipal('lambda.amazonaws.com'),
            description='Least-privilege role for the BTC-USD paper-only runtime.',
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid='TradeEventsOnly',
                actions=[
                    'dynamodb:GetItem',
                    'dynamodb:PutItem',
                    'dynamodb:Query',
                    'dynamodb:TransactWriteItems',
                ],
                resources=[table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid='InvokeReviewedModelOnly',
                actions=['bedrock:InvokeModel'],
                resources=[model_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid='WriteOwnLogsOnly',
                actions=['logs:CreateLogStream', 'logs:PutLogEvents'],
                resources=[f'{log_group.log_group_arn}:*'],
            )
        )

        report_bucket = s3.Bucket(
            self,
            'PaperReportBucket',
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
        )
        report_policy = iam.Policy(
            self,
            'PaperReportPolicy',
            statements=[iam.PolicyStatement(
                sid='WriteReportsToS3',
                actions=['s3:PutObject'],
                resources=[f'{report_bucket.bucket_arn}/reports/paper_performance/*'],
            )],
        )
        report_policy.attach_to_role(role)

        code = _lambda.Code.from_asset(
            source_dir,
            asset_hash_type=AssetHashType.OUTPUT,
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                local=LocalPythonBundler(source_dir, requirements_file),
            ),
        )
        fn = _lambda.Function(
            self,
            'PaperAgentRuntime',
            function_name=function_name.value_as_string,
            description='Private BTC-USD paper-only analysis and simulated-order runtime.',
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.X86_64,
            handler='src.agent.lambda_handler.lambda_handler',
            code=code,
            role=role,
            log_group=log_group,
            timeout=Duration.seconds(30),
            memory_size=512,
            environment={
                'PAPER_MODE': 'true',
                'SYMBOL': 'BTC-USD',
                'REPORT_S3_BUCKET': report_bucket.bucket_name,
                'REPORT_S3_PREFIX': 'reports/paper_performance',
                'ALLOW_SYNTHETIC_MARKET_DATA': 'false',
                'BEDROCK_MODEL_ID': MODEL_ID,
                'TRADE_EVENTS_TABLE': table_name.value_as_string,
                'DIP_BUY_ENABLED': 'true',
                'DIP_THRESHOLD_PCT': '0.02',
                'DIP_LOOKBACK_MINUTES': '60',
                'DIP_PAPER_ORDER_USD': '25',
                'DIP_COOLDOWN_MINUTES': '60',
            },
        )

        # Schedule a periodic evaluation for automatic strategies (every 5 minutes)
        rule = events.Rule(
            self,
            'AutoDipSchedule',
            schedule=events.Schedule.rate(Duration.minutes(5)),
            enabled=True,
            description='Periodic schedule to evaluate automatic dip-buy strategy (paper-only).',
        )
        rule.add_target(events_targets.LambdaFunction(
            fn,
            event=events.RuleTargetInput.from_object({'action': 'auto_dip_evaluate'}),
            retry_attempts=0,
            max_event_age=Duration.minutes(5),
        ))
