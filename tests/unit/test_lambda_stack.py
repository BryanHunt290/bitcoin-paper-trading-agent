import json
import sys
from pathlib import Path

from aws_cdk import App
from aws_cdk.assertions import Template


CDK_DIR = Path(__file__).resolve().parents[2] / 'cdk'
sys.path.insert(0, str(CDK_DIR))

from lambda_stack import AgentRuntimeStack  # noqa: E402


def test_runtime_stack_is_private_and_least_privilege():
    app = App()
    stack = AgentRuntimeStack(app, 'ShowcaseRuntimeStack')
    template = Template.from_stack(stack).to_json()

    functions = [r for r in template['Resources'].values() if r['Type'] == 'AWS::Lambda::Function']
    assert len(functions) == 1
    props = functions[0]['Properties']
    assert props['Runtime'] == 'python3.12'
    assert props['Handler'] == 'src.agent.lambda_handler.lambda_handler'
    assert props['FunctionName'] == {'Ref': 'PaperAgentFunctionName'}
    assert 'ReservedConcurrentExecutions' not in props
    variables = props['Environment']['Variables']
    assert variables['PAPER_MODE'] == 'true'
    assert variables['SYMBOL'] == 'BTC-USD'
    assert variables['REPORT_S3_PREFIX'] == 'reports/paper_performance'
    assert variables['ALLOW_SYNTHETIC_MARKET_DATA'] == 'false'
    assert variables['BEDROCK_MODEL_ID'] == 'amazon.nova-lite-v1:0'
    assert variables['DIP_BUY_ENABLED'] == 'true'
    assert variables['DIP_THRESHOLD_PCT'] == '0.02'
    assert variables['DIP_LOOKBACK_MINUTES'] == '60'
    assert variables['DIP_PAPER_ORDER_USD'] == '25'
    assert variables['DIP_COOLDOWN_MINUTES'] == '60'

    resource_types = {resource['Type'] for resource in template['Resources'].values()}
    assert 'AWS::ApiGateway::RestApi' not in resource_types
    assert 'AWS::SecretsManager::Secret' not in resource_types
    assert 'AWS::EC2::VPC' not in resource_types
    buckets = [
        (logical_id, resource)
        for logical_id, resource in template['Resources'].items()
        if resource['Type'] == 'AWS::S3::Bucket'
    ]
    assert len(buckets) == 1
    bucket_id, bucket = buckets[0]
    assert variables['REPORT_S3_BUCKET'] == {'Ref': bucket_id}
    bucket_props = bucket['Properties']
    assert bucket_props['BucketEncryption'] == {
        'ServerSideEncryptionConfiguration': [
            {'ServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}},
        ],
    }
    assert bucket_props['VersioningConfiguration'] == {'Status': 'Enabled'}
    assert bucket_props['OwnershipControls'] == {
        'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}],
    }
    assert bucket_props['PublicAccessBlockConfiguration'] == {
        'BlockPublicAcls': True,
        'BlockPublicPolicy': True,
        'IgnorePublicAcls': True,
        'RestrictPublicBuckets': True,
    }
    assert 'WebsiteConfiguration' not in bucket_props
    assert bucket['DeletionPolicy'] == 'Retain'
    assert bucket['UpdateReplacePolicy'] == 'Retain'

    report_policies = [
        resource for resource in template['Resources'].values()
        if resource['Type'] == 'AWS::IAM::Policy'
        and resource['Properties']['PolicyDocument']['Statement'][0].get('Sid') == 'WriteReportsToS3'
    ]
    assert len(report_policies) == 1
    report_statement = report_policies[0]['Properties']['PolicyDocument']['Statement'][0]
    assert report_statement['Action'] == 's3:PutObject'
    assert report_statement['Resource'] != '*'
    assert 'DeleteObject' not in str(report_statement)

    policies = [r for r in template['Resources'].values() if r['Type'] == 'AWS::IAM::Policy']
    statements = policies[0]['Properties']['PolicyDocument']['Statement']
    flat_actions = {action for statement in statements for action in ([statement['Action']] if isinstance(statement['Action'], str) else statement['Action'])}
    assert 'dynamodb:*' not in flat_actions
    assert 'secretsmanager:GetSecretValue' not in flat_actions
    assert 'bedrock:InvokeModel' in flat_actions
    dynamo = next(statement for statement in statements if 'dynamodb:GetItem' in statement['Action'])
    assert dynamo['Action'] == ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:TransactWriteItems']
    assert dynamo['Resource'] != '*'

    rules = [r for r in template['Resources'].values() if r['Type'] == 'AWS::Events::Rule']
    assert len(rules) == 1
    rule = rules[0]['Properties']
    assert 'Name' not in rule
    assert rule['ScheduleExpression'] == 'rate(5 minutes)'
    assert rule['State'] == 'ENABLED'
    assert json.loads(rule['Targets'][0]['Input']) == {'action': 'auto_dip_evaluate'}
    assert rule['Targets'][0]['RetryPolicy'] == {
        'MaximumEventAgeInSeconds': 300,
        'MaximumRetryAttempts': 0,
    }
