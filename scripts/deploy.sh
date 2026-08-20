#!/usr/bin/env bash
# Operator Zero Workshop — Base Infrastructure Deploy Script
# Run this in AWS CloudShell after cloning the repo.
# Region: us-east-1 (N. Virginia) — default for attendee sandbox accounts

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║       OPERATOR ZERO — Base Infrastructure Deploy     ║"
echo "║       AWS Community Day Philippines 2026             ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Account : $ACCOUNT_ID"
echo "Region  : $REGION"
echo ""

# Step 1: Install CDK dependencies
echo "▶ Installing CDK dependencies..."
cd "$(dirname "$0")/../cdk"
npm install --silent

# Step 2: Bootstrap CDK (safe to run multiple times)
echo "▶ Bootstrapping CDK..."
cdk bootstrap "aws://${ACCOUNT_ID}/${REGION}" --region "$REGION"

# Step 3: Deploy base stack
echo ""
echo "▶ Deploying OperatorZeroBaseStack..."
echo "  This takes 8–12 minutes. Watch the resources being created."
echo ""
cdk deploy OperatorZeroBaseStack \
  --require-approval never \
  --region "$REGION"

# Step 4: Print outputs
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║                Deploy Complete ✅                    ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Resources created in your account:"
echo ""
aws lambda list-functions \
  --region "$REGION" \
  --query 'Functions[?starts_with(FunctionName, `operator-zero`)].FunctionName' \
  --output table

echo ""
aws dynamodb describe-table \
  --table-name operator-zero-incidents \
  --region "$REGION" \
  --query 'Table.{Name:TableName,Status:TableStatus}' \
  --output table

echo ""
echo "Next step: Create your AgentCore Harness in the AWS Console."
echo "Follow the workshop guide starting at Step 4."
echo ""
