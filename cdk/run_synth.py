import aws_cdk as cdk
from dynamo_stack import DynamoTableStack
from lambda_stack import AgentRuntimeStack

app = cdk.App()
DynamoTableStack(app, "ShowcasePersistenceStack")
AgentRuntimeStack(app, "ShowcaseRuntimeStack")
app.synth()
