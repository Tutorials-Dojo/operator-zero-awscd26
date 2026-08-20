#!/usr/bin/env bash
# Point the dispatcher Lambda at Agent IDs / AgentCore harness from .env.agent.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export PATH="${HOME}/Library/Python/3.9/bin:${PATH}"

ENV_FILE="${ROOT}/.env.agent"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: ${ENV_FILE} not found. Run: python3 scripts/create_agent.py" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

if [[ -z "${AGENT_ID:-}" || -z "${AGENT_ALIAS_ID:-}" ]]; then
  echo "ERROR: AGENT_ID / AGENT_ALIAS_ID missing in .env.agent" >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
MODEL_ID="${BEDROCK_MODEL_ID:-apac.anthropic.claude-3-5-sonnet-20241022-v2:0}"
ACTION_HANDLER_NAME="${ACTION_HANDLER_NAME:-operator-zero-action-handler}"
HARNESS_ARN="${HARNESS_ARN:-}"
GATEWAY_ARN="${GATEWAY_ARN:-}"

VARS="AGENT_ID=${AGENT_ID},AGENT_ALIAS_ID=${AGENT_ALIAS_ID},MEMORY_TABLE=operator-zero-incidents,SLACK_SECRET_NAME=operator-zero/slack-webhook,AWS_ACCOUNT_ID=${ACCOUNT_ID},BEDROCK_MODEL_ID=${MODEL_ID},ACTION_HANDLER_NAME=${ACTION_HANDLER_NAME}"
if [[ -n "$HARNESS_ARN" ]]; then
  VARS="${VARS},HARNESS_ARN=${HARNESS_ARN}"
fi
if [[ -n "$GATEWAY_ARN" ]]; then
  VARS="${VARS},GATEWAY_ARN=${GATEWAY_ARN}"
fi

aws lambda update-function-configuration \
  --function-name operator-zero-dispatcher \
  --environment "Variables={${VARS}}" \
  --timeout 180 \
  --region "$REGION" \
  --query '{FunctionName:FunctionName,LastUpdateStatus:LastUpdateStatus,AgentId:Environment.Variables.AGENT_ID,Harness:Environment.Variables.HARNESS_ARN}' \
  --output json

aws lambda wait function-updated \
  --function-name operator-zero-dispatcher \
  --region "$REGION"

echo "Dispatcher updated with Agent ID: ${AGENT_ID} Alias: ${AGENT_ALIAS_ID}"
