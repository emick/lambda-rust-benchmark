const cdk = require('aws-cdk-lib');
const { lambda, dynamodb, s3, iam, logs, apigatewayv2, apigatewayv2_integrations, ec2, s3_assets } = {
  lambda: cdk.aws_lambda, dynamodb: cdk.aws_dynamodb, s3: cdk.aws_s3, iam: cdk.aws_iam,
  logs: cdk.aws_logs, apigatewayv2: cdk.aws_apigatewayv2,
  apigatewayv2_integrations: cdk.aws_apigatewayv2_integrations,
  ec2: cdk.aws_ec2, s3_assets: cdk.aws_s3_assets,
};
const path = require('node:path');
require('dotenv').config({ path: path.join(__dirname, '..', '.env'), quiet: true });
if (!process.env.AWS_REGION) throw new Error('Set AWS_REGION in .env');
const app = new cdk.App();
const env = { region: process.env.AWS_REGION, account: process.env.CDK_DEFAULT_ACCOUNT };
const memory = Number(app.node.tryGetContext('memory') || 512);
if (![128, 256, 512, 1024].includes(memory)) throw new Error('memory must be 128, 256, 512, or 1024');

class Variant extends cdk.Stack {
  constructor(variant) {
    super(app, `Benchmark-${variant}`, { env });
    const prefix = `benchmark-${variant}`;
    const table = new dynamodb.Table(this, 'Users', {
      tableName: `${prefix}-users`, partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST, removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const bucket = new s3.Bucket(this, 'Files', { versioned: false, blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true, removalPolicy: cdk.RemovalPolicy.DESTROY });
    const role = new iam.Role(this, 'ExecutionRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    const logGroup = new logs.LogGroup(this, 'Logs', { logGroupName: `/aws/lambda/${prefix}-lambda`,
      retention: logs.RetentionDays.ONE_WEEK, removalPolicy: cdk.RemovalPolicy.DESTROY });
    logGroup.grantWrite(role);
    role.addToPolicy(new iam.PolicyStatement({ actions: ['dynamodb:PutItem'], resources: [table.tableArn] }));
    role.addToPolicy(new iam.PolicyStatement({ actions: ['s3:PutObject'], resources: [bucket.arnForObjects('*')] }));
    const python = variant.startsWith('python');
    const fn = new lambda.Function(this, 'Function', { functionName: `${prefix}-lambda`,
      runtime: python ? lambda.Runtime.PYTHON_3_13 : lambda.Runtime.PROVIDED_AL2023,
      architecture: lambda.Architecture.ARM_64, handler: python ? 'app.handler' : 'bootstrap',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'build', python ? 'python.zip' : 'rust.zip')),
      memorySize: memory, timeout: cdk.Duration.seconds(15), reservedConcurrentExecutions: 100,
      environment: { USERS_TABLE: table.tableName, FILES_BUCKET: bucket.bucketName },
      role, logGroup, snapStart: variant === 'python-snapstart' ? lambda.SnapStartConf.ON_PUBLISHED_VERSIONS : undefined,
    });
    const alias = new lambda.Alias(this, 'BenchmarkAlias', { aliasName: 'benchmark', version: fn.currentVersion });
    const api = new apigatewayv2.HttpApi(this, 'Api', { apiName: `${prefix}-api`,
      defaultIntegration: new apigatewayv2_integrations.HttpLambdaIntegration('CatchAll', alias, {
        payloadFormatVersion: apigatewayv2.PayloadFormatVersion.VERSION_2_0,
      }),
    });
    for (const [key, value] of Object.entries({ Variant: variant, ApiUrl: api.apiEndpoint,
      FunctionName: fn.functionName, Alias: alias.aliasName, TableName: table.tableName,
      BucketName: bucket.bucketName, LogGroup: logGroup.logGroupName })) new cdk.CfnOutput(this, key, { value });
  }
}

if (!app.node.tryGetContext('runnerOnly')) for (const variant of ['python', 'python-snapstart', 'rust']) new Variant(variant);

if (app.node.tryGetContext('runner') || app.node.tryGetContext('runnerOnly')) {
  const stack = new cdk.Stack(app, 'Benchmark-Runner', { env });
  const vpc = new ec2.Vpc(stack, 'Vpc', { maxAzs: 1, natGateways: 0,
    subnetConfiguration: [{ name: 'Public', subnetType: ec2.SubnetType.PUBLIC }] });
  const securityGroup = new ec2.SecurityGroup(stack, 'SecurityGroup', { vpc, allowAllOutbound: true });
  const role = new iam.Role(stack, 'RunnerRole', { assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
    managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore')] });
  const fnArn = stack.formatArn({ service: 'lambda', resource: 'function', resourceName: 'benchmark-*-lambda*', arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME });
  role.addToPolicy(new iam.PolicyStatement({ actions: ['lambda:GetFunction', 'lambda:GetFunctionConfiguration',
    'lambda:UpdateFunctionConfiguration', 'lambda:PublishVersion', 'lambda:UpdateAlias', 'lambda:GetAlias',
    'lambda:ListVersionsByFunction', 'lambda:DeleteFunction', 'lambda:GetFunctionConcurrency',
    'lambda:ListProvisionedConcurrencyConfigs'], resources: [fnArn] }));
  role.addToPolicy(new iam.PolicyStatement({ actions: ['lambda:GetAccountSettings', 'cloudwatch:GetMetricStatistics',
    'pricing:GetProducts'], resources: ['*'] }));
  role.addToPolicy(new iam.PolicyStatement({ actions: ['logs:FilterLogEvents'], resources: [
    stack.formatArn({ service: 'logs', resource: 'log-group', resourceName: '/aws/lambda/benchmark-*-lambda:*', arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME })] }));
  role.addToPolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem', 'dynamodb:Scan', 'dynamodb:BatchWriteItem'],
    resources: [stack.formatArn({ service: 'dynamodb', resource: 'table', resourceName: 'benchmark-*-users' })] }));
  role.addToPolicy(new iam.PolicyStatement({ actions: ['s3:ListBucket'], resources: [`arn:aws:s3:::benchmark-*-files*`] }));
  role.addToPolicy(new iam.PolicyStatement({ actions: ['s3:GetObject', 's3:DeleteObject'], resources: [`arn:aws:s3:::benchmark-*-files*/*`] }));
  const exports = new s3.Bucket(stack, 'Exports', { blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    enforceSSL: true, removalPolicy: cdk.RemovalPolicy.DESTROY });
  exports.grantReadWrite(role);
  const asset = new s3_assets.Asset(stack, 'Source', { path: path.join(__dirname, '..', 'build', 'runner') });
  asset.grantRead(role);
  const host = new ec2.Instance(stack, 'Host', { vpc, securityGroup, role,
    instanceType: new ec2.InstanceType('t3.small'), machineImage: ec2.MachineImage.latestAmazonLinux2023(),
    requireImdsv2: true, associatePublicIpAddress: true,
    blockDevices: [{ deviceName: '/dev/xvda', volume: ec2.BlockDeviceVolume.ebs(12, { encrypted: true }) }] });
  host.userData.addCommands('set -eu', 'dnf install -y -q unzip',
    'mkdir -p /opt/benchmark', `aws s3 cp s3://${asset.s3BucketName}/${asset.s3ObjectKey} /tmp/source.zip --only-show-errors`,
    'unzip -q /tmp/source.zip -d /opt/benchmark', 'cd /opt/benchmark',
    'curl -LsSf https://astral.sh/uv/install.sh | sh',
    `printf 'AWS_REGION=${process.env.AWS_REGION}\n' > .env`,
    '/root/.local/bin/uv sync --frozen -q',
    `printf '%s' '${exports.bucketName}' > export-bucket`, 'touch /opt/benchmark/ready');
  new cdk.CfnOutput(stack, 'InstanceId', { value: host.instanceId });
  new cdk.CfnOutput(stack, 'ExportBucket', { value: exports.bucketName });
}
cdk.Tags.of(app).add('Project', 'lambda-rust-benchmark');
