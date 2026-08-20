#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { OperatorZeroBaseStack } from '../lib/base-infra-stack';

const app = new cdk.App();

new OperatorZeroBaseStack(app, 'OperatorZeroBaseStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || 'us-east-1',
  },
  description:
    'Operator Zero — Base infrastructure for the autonomous AWS incident monitoring agent',
  tags: {
    Project: 'operator-zero',
    Workshop: 'aws-community-day-ph-2026',
    ManagedBy: 'cdk',
    Environment: 'workshop',
    Owner: 'tutorials-dojo',
  },
});

app.synth();
