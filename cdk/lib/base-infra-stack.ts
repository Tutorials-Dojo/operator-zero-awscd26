import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { execSync } from 'child_process';

const SLACK_SECRET_NAME = 'operator-zero/slack-webhook';
const ECS_CPU_ALARM_NAME = 'operator-zero-ECSHighCPU';
const CHAOS_ALARM_NAME = 'operator-zero-ChaosSimulation';

// AgentCore needs a boto3 build newer than what the base Lambda Python
// runtime bundles today. Bundle each function's own dependencies (from its
// requirements.txt) into the deployment package instead of relying on the
// runtime's preinstalled boto3. Local bundling (pip on the CDK host —
// CloudShell always has python3 + pip3) is tried first so no Docker daemon
// is required; it falls back to Docker bundling automatically otherwise.
function pythonLambdaCode(dir: string): lambda.Code {
  return lambda.Code.fromAsset(dir, {
    bundling: {
      image: lambda.Runtime.PYTHON_3_14.bundlingImage,
      local: {
        tryBundle(outputDir: string): boolean {
          try {
            execSync('python3 --version', { stdio: 'ignore' });
          } catch {
            return false;
          }
          execSync(
            `python3 -m pip install --quiet -r "${path.join(dir, 'requirements.txt')}" -t "${outputDir}"`,
            { stdio: 'inherit' },
          );
          execSync(`cp -R "${dir}"/. "${outputDir}"`, { stdio: 'inherit' });
          return true;
        },
      },
      command: [
        'bash', '-c',
        'pip install --quiet -r requirements.txt -t /asset-output && cp -R . /asset-output',
      ],
    },
  });
}

export class OperatorZeroBaseStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── TASK 1.2 — VPC ────────────────────────────────────────────────────────
    // Workshop account note: this account is already at the EC2-VPC Elastic IP
    // quota (5/5). A NAT Gateway requires a new EIP, so we use public subnets
    // only (no NAT). The monitored nginx task pulls images via a public IP.
    // Prefer private + NAT (natGateways: 1) when an EIP is available.
    const vpc = new ec2.Vpc(this, 'OperatorZeroVpc', {
      vpcName: 'operator-zero-vpc',
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'Public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    // ── TASK 1.3 — ECS Cluster + Fargate Service (the monitored workload) ─────
    const cluster = new ecs.Cluster(this, 'OperatorZeroCluster', {
      vpc,
      clusterName: 'operator-zero-cluster',
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, 'OperatorZeroTaskDef', {
      cpu: 256,
      memoryLimitMiB: 512,
    });

    taskDef.addContainer('nginx', {
      image: ecs.ContainerImage.fromRegistry('public.ecr.aws/nginx/nginx:latest'),
      memoryLimitMiB: 512,
      portMappings: [{ containerPort: 80 }],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'operator-zero-nginx',
        logRetention: logs.RetentionDays.ONE_WEEK,
      }),
    });

    const service = new ecs.FargateService(this, 'OperatorZeroService', {
      cluster,
      taskDefinition: taskDef,
      serviceName: 'operator-zero-service',
      desiredCount: 1,
      assignPublicIp: true,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });

    // ── TASK 1.4 — DynamoDB incident memory ───────────────────────────────────
    const incidentTable = new dynamodb.Table(this, 'IncidentMemoryTable', {
      tableName: 'operator-zero-incidents',
      partitionKey: { name: 'incident_type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      // Workshop teardown safety — change to RETAIN for production.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: false },
    });

    // Model B multi-tenant isolation: query all incidents for one lab session.
    incidentTable.addGlobalSecondaryIndex({
      indexName: 'session-id-index',
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── TASK 1.5 — Shared least-privilege Lambda execution role ───────────────
    const lambdaRole = new iam.Role(this, 'OperatorZeroLambdaRole', {
      roleName: 'operator-zero-lambda-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Shared execution role for the three Operator Zero Lambda functions',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaVPCAccessExecutionRole'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('AWSXRayDaemonWriteAccess'),
      ],
      inlinePolicies: {
        OperatorZeroPolicy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              sid: 'IncidentMemoryAccess',
              actions: [
                'dynamodb:PutItem',
                'dynamodb:Query',
                'dynamodb:GetItem',
                'dynamodb:DeleteItem',
              ],
              resources: [
                incidentTable.tableArn,
                `${incidentTable.tableArn}/index/*`,
              ],
            }),
            new iam.PolicyStatement({
              sid: 'SlackWebhookSecretRead',
              actions: ['secretsmanager:GetSecretValue'],
              resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:operator-zero/*`],
            }),
            new iam.PolicyStatement({
              sid: 'ChaosAlarmControl',
              actions: [
                'cloudwatch:PutMetricData',
                'cloudwatch:SetAlarmState',
                'cloudwatch:DescribeAlarms',
              ],
              resources: ['*'],
            }),
            new iam.PolicyStatement({
              sid: 'Logging',
              actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
              resources: ['*'],
            }),
            // dispatcher's invoke_harness on the Supervisor Harness, and
            // action-handler needs this to delegate diagnose_incident /
            // execute_remediation to the Diagnostics and Remediation Harnesses.
            // Harness ARNs don't exist until created in the console (Steps 9-10),
            // so this is scoped to the harness resource type, not a specific ARN.
            new iam.PolicyStatement({
              sid: 'InvokeSubHarnesses',
              actions: ['bedrock-agentcore:InvokeHarness'],
              resources: [`arn:aws:bedrock-agentcore:${this.region}:${this.account}:harness/*`],
            }),
            // action-handler's restart_ecs_service / verify_ecs_service_health —
            // scoped to the one cluster/service Operator Zero is allowed to touch.
            new iam.PolicyStatement({
              sid: 'RemediationEcsAccess',
              actions: ['ecs:UpdateService', 'ecs:DescribeServices'],
              resources: [
                `arn:aws:ecs:${this.region}:${this.account}:service/operator-zero-cluster/operator-zero-service`,
              ],
            }),
          ],
        }),
      },
    });

    // ── TASK 1.6 — Lambda functions ───────────────────────────────────────────
    const dispatcherLogGroup = new logs.LogGroup(this, 'DispatcherLogGroup', {
      logGroupName: '/aws/lambda/operator-zero-dispatcher',
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const dispatcherFn = new lambda.Function(this, 'DispatcherFn', {
      functionName: 'operator-zero-dispatcher',
      description: 'EventBridge → AgentCore Harness dispatcher',
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: pythonLambdaCode(path.join(__dirname, '../../lambda/dispatcher')),
      role: lambdaRole,
      memorySize: 512,
      timeout: cdk.Duration.minutes(3),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: dispatcherLogGroup,
      // Covers ~40 concurrent learners + buffer. Bedrock tokens/sec remain the real bottleneck.
      // reservedConcurrentExecutions: 50,
      environment: {
        // Set after creating the Supervisor Harness (Step 13) — no code edits needed.
        HARNESS_ARN: '',
        MEMORY_TABLE: incidentTable.tableName,
      },
    });

    dispatcherFn.addPermission('BedrockInvokeDispatcher', {
      principal: new iam.ServicePrincipal('bedrock.amazonaws.com'),
      action: 'lambda:InvokeFunction',
    });

    const actionHandlerLogGroup = new logs.LogGroup(this, 'ActionHandlerLogGroup', {
      logGroupName: '/aws/lambda/operator-zero-action-handler',
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const actionHandlerFn = new lambda.Function(this, 'ActionHandlerFn', {
      functionName: 'operator-zero-action-handler',
      description: 'AgentCore Gateway target — incident memory, ECS healing, Harness orchestration',
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: pythonLambdaCode(path.join(__dirname, '../../lambda/action-handler')),
      role: lambdaRole,
      memorySize: 256,
      // Diagnostics/Remediation Harness delegation can take 60-90s; the
      // Gateway's own invocation timeout is longer than the default 30s.
      timeout: cdk.Duration.seconds(120),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: actionHandlerLogGroup,
      environment: {
        MEMORY_TABLE: incidentTable.tableName,
        SLACK_SECRET_NAME: SLACK_SECRET_NAME,
        // Set these after creating the Diagnostics/Remediation Harnesses
        // (Steps 9-10) — no code edits needed.
        DIAGNOSTICS_HARNESS_ARN: '',
        REMEDIATION_HARNESS_ARN: '',
      },
    });

    actionHandlerFn.addPermission('BedrockInvokeActionHandler', {
      principal: new iam.ServicePrincipal('bedrock.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceArn: `arn:aws:bedrock:${this.region}:${this.account}:agent/*`,
    });

    // AgentCore Gateway (Lambda MCP target, Step 8) invokes this function
    // using its own execution role's IAM credentials, not this service
    // principal — but granting it too covers gateway invocation paths that
    // authenticate as the AgentCore service itself.
    actionHandlerFn.addPermission('AgentCoreGatewayInvokeActionHandler', {
      principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceAccount: this.account,
    });

    const chaosLogGroup = new logs.LogGroup(this, 'ChaosTriggerLogGroup', {
      logGroupName: '/aws/lambda/operator-zero-chaos-trigger',
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const chaosFn = new lambda.Function(this, 'ChaosTriggerFn', {
      functionName: 'operator-zero-chaos-trigger',
      description: 'Sets the CloudWatch alarm into ALARM state to simulate an incident',
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.handler',
      code: pythonLambdaCode(path.join(__dirname, '../../lambda/chaos-trigger')),
      role: lambdaRole,
      memorySize: 128,
      timeout: cdk.Duration.seconds(30),
      tracing: lambda.Tracing.PASS_THROUGH,
      logGroup: chaosLogGroup,
      environment: {
        ECS_CLUSTER_NAME: cluster.clusterName,
        ECS_SERVICE_NAME: service.serviceName,
        ALARM_NAME: ECS_CPU_ALARM_NAME,
      },
    });

    // ── TASK 1.7 — CloudWatch alarms ──────────────────────────────────────────
    const cpuAlarm = new cloudwatch.Alarm(this, 'ECSHighCPUAlarm', {
      alarmName: ECS_CPU_ALARM_NAME,
      alarmDescription: 'ECS CPU high — triggers Operator Zero agent',
      metric: service.metricCpuUtilization({
        period: cdk.Duration.minutes(1),
        statistic: 'Average',
      }),
      threshold: 60,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    cpuAlarm.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    const chaosAlarm = new cloudwatch.Alarm(this, 'ChaosSimAlarm', {
      alarmName: CHAOS_ALARM_NAME,
      alarmDescription: 'Operator Zero chaos simulation alarm — driven by the chaos trigger Lambda',
      metric: new cloudwatch.Metric({
        namespace: 'OperatorZero/Chaos',
        metricName: 'SimulatedCPUUtilization',
        dimensionsMap: {
          ClusterName: cluster.clusterName,
          ServiceName: service.serviceName,
        },
        period: cdk.Duration.minutes(1),
        statistic: 'Average',
      }),
      threshold: 60,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    chaosAlarm.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    // ── TASK 1.8 — EventBridge rule (DISABLED until the live demo) ────────────
    const alarmRule = new events.Rule(this, 'AlarmRouterRule', {
      ruleName: 'operator-zero-alarm-router',
      description: 'Routes CloudWatch alarm state changes to the Operator Zero dispatcher',
      enabled: false,
      eventPattern: {
        source: ['aws.cloudwatch'],
        detailType: ['CloudWatch Alarm State Change'],
        detail: {
          alarmName: [{ prefix: 'operator-zero-' }],
          state: { value: ['ALARM'] },
        },
      },
    });

    alarmRule.addTarget(
      new targets.LambdaFunction(dispatcherFn, {
        retryAttempts: 2,
      }),
    );

    // ── TASK 1.9 — Outputs ────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'DispatcherFunctionName', {
      value: dispatcherFn.functionName,
      description: 'Set HARNESS_ARN after creating the Supervisor Harness (Step 13)',
      exportName: 'OperatorZeroDispatcherFunctionName',
    });

    new cdk.CfnOutput(this, 'ActionHandlerFunctionName', {
      value: actionHandlerFn.functionName,
      description: 'Select this Lambda when creating Bedrock Action Groups',
      exportName: 'OperatorZeroActionHandlerFunctionName',
    });

    new cdk.CfnOutput(this, 'ChaosTriggerFunctionName', {
      value: chaosFn.functionName,
      description: 'Invoke this Lambda to trigger the live demo chaos event',
      exportName: 'OperatorZeroChaosTriggerFunctionName',
    });

    new cdk.CfnOutput(this, 'IncidentMemoryTableName', {
      value: incidentTable.tableName,
      description: 'DynamoDB table storing agent incident memory',
      exportName: 'OperatorZeroIncidentMemoryTable',
    });

    new cdk.CfnOutput(this, 'EventBridgeRuleName', {
      value: alarmRule.ruleName,
      description: 'Disabled by default — enable manually during the demo',
      exportName: 'OperatorZeroEventBridgeRule',
    });

    new cdk.CfnOutput(this, 'ECSClusterName', {
      value: cluster.clusterName,
      description: 'ECS cluster running the monitored workload',
      exportName: 'OperatorZeroECSCluster',
    });

    new cdk.CfnOutput(this, 'ECSServiceName', {
      value: service.serviceName,
      description: 'ECS Fargate service being monitored',
      exportName: 'OperatorZeroECSService',
    });

    new cdk.CfnOutput(this, 'AlarmName', {
      value: cpuAlarm.alarmName,
      description: 'Primary ECS CPU alarm that triggers the agent',
      exportName: 'OperatorZeroAlarmName',
    });

    new cdk.CfnOutput(this, 'ChaosAlarmName', {
      value: chaosAlarm.alarmName,
      description: 'Alarm fed by the chaos simulation custom metric',
      exportName: 'OperatorZeroChaosAlarmName',
    });

    // ── TASK 1.10 — Tags ──────────────────────────────────────────────────────
    cdk.Tags.of(this).add('Project', 'operator-zero');
    cdk.Tags.of(this).add('Workshop', 'aws-community-day-ph-2026');
    cdk.Tags.of(this).add('ManagedBy', 'cdk');
    cdk.Tags.of(this).add('Environment', 'workshop');
    cdk.Tags.of(this).add('Owner', 'tutorials-dojo');
  }
}
