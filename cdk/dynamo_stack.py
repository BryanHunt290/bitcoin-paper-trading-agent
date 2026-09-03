from aws_cdk import (
    Stack,
    RemovalPolicy,
)
from constructs import Construct
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam


class DynamoTableStack(Stack):
    """CloudFormation stack defining the single-table DynamoDB schema for trade events.

    Design notes:
    - PK/SK single-table with entity prefixes (PORTFOLIO#, ORDER#, FILL#, IDEMPOTENCY#)
    - TTL attribute: `ttl` (epoch seconds)
    - PAY_PER_REQUEST billing
    - Server-side encryption with AWS managed CMK
    - Point-in-time recovery enabled
    - GSI `GSI1` for alternate access patterns (GSI1PK/GSI1SK)
    - RemovalPolicy.RETAIN to avoid accidental data loss in production
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        table = dynamodb.Table(
            self,
            "TradeEventsTable",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=True,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Global secondary index to enable queries by alternate keys (example: idempotency or entity-type)
        table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(name="GSI1PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="GSI1SK", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Minimal example IAM policy statement for the agent's role to access the table.
        # This is rendered here for review; attach to a role/principal in deployment environments.
        policy_statement = iam.PolicyStatement(
            actions=[
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:TransactWriteItems",
                "dynamodb:Query",
                "dynamodb:BatchGetItem",
            ],
            resources=[table.table_arn, f"{table.table_arn}/*"],
        )

        policy = iam.Policy(self, "AgentDynamoPolicy", statements=[policy_statement])

