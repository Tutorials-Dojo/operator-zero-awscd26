"""
Operator Zero — Action Handler Lambda

Invoked by the AgentCore Gateway on behalf of the Supervisor, Diagnostics,
and Remediation Harnesses. Routes tool calls: query_incident_history,
post_to_slack, restart_ecs_service, verify_ecs_service_health,
diagnose_incident, execute_remediation, record_incident_outcome.

Environment (set by CDK, no code edits required):
  MEMORY_TABLE              DynamoDB table name (operator-zero-incidents)
  SLACK_SECRET_NAME         Secrets Manager secret holding the Slack webhook URL
  DIAGNOSTICS_HARNESS_ARN   Diagnostics Harness ARN (set after Step 9)
  REMEDIATION_HARNESS_ARN   Remediation Harness ARN (set after Step 10)
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
import urllib.request
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
secretsmanager = boto3.client("secretsmanager")
ecs_client = boto3.client("ecs")
# AgentCore data-plane client is "bedrock-agentcore" / invoke_harness(...).
# There is no "bedrock-agentcore-runtime" client or invoke_agent(...) method.
agentcore_client = boto3.client("bedrock-agentcore")

TTL_SECONDS = 90 * 24 * 60 * 60
HARNESS_MAX_ITERATIONS = 8
HARNESS_TIMEOUT_SECONDS = 90

_slack_webhook_cache: str | None = None

SEVERITY_EMOJI = {
    "CRITICAL": "🚨",
    "HIGH": "🔴",
    "MEDIUM": "🟡",
    "LOW": "🟢",
    "INFO": "ℹ️",
}
DEFAULT_SEVERITY_EMOJI = "🤖"
AGENT_EXCERPT_CHARS = 500
SLACK_HTTP_TIMEOUT_SECONDS = 10


def build_response(event: dict, function_name: str, body_text: str) -> dict:
    return {
        "actionGroup": event.get("actionGroup", ""),
        "function": function_name,
        "functionResponse": {
            "responseBody": {
                "TEXT": {
                    "body": body_text,
                }
            }
        },
    }


def get_slack_webhook() -> str:
    global _slack_webhook_cache

    if _slack_webhook_cache:
        return _slack_webhook_cache

    secret_name = os.environ.get("SLACK_SECRET_NAME")
    if not secret_name:
        raise ValueError(
            "SLACK_SECRET_NAME is not set. Expected operator-zero/slack-webhook "
            "(created in Task 1 of the lab guide)."
        )

    try:
        secret = secretsmanager.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.error(
            "Secrets Manager GetSecretValue failed: code=%s message=%s",
            error.get("Code", "Unknown"),
            error.get("Message", str(exc)),
        )
        raise

    _slack_webhook_cache = secret["SecretString"]
    return _slack_webhook_cache


def query_incident_history(event: dict, params: dict) -> dict:
    incident_type = params.get("incident_type") or "unknown"
    try:
        limit = int(params.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5

    logger.info("Querying incident history: type=%s limit=%d", incident_type, limit)

    table_name = os.environ.get("MEMORY_TABLE")
    if not table_name or table_name.startswith("PLACEHOLDER"):
        return build_response(
            event,
            "query_incident_history",
            (
                "MEMORY_TABLE is missing or still a PLACEHOLDER. "
                "Set it to operator-zero-incidents on the action-handler Lambda. "
                "Proceed without historical context."
            ),
        )

    try:
        table = dynamodb.Table(table_name)
        session_id = (params.get("session_id") or "").strip()
        if session_id and session_id != "default":
            result = table.query(
                IndexName="session-id-index",
                KeyConditionExpression=Key("session_id").eq(session_id),
                ScanIndexForward=False,
                Limit=max(limit * 3, 15),
            )
            items = [
                item
                for item in (result.get("Items") or [])
                if str(item.get("incident_type")) == incident_type
            ][:limit]
        else:
            result = table.query(
                KeyConditionExpression=Key("incident_type").eq(incident_type),
                ScanIndexForward=False,
                Limit=limit,
            )
            items = result.get("Items") or []

        if not items:
            body = (
                f"No past incidents found for type '{incident_type}'. "
                "This appears to be the first occurrence in my memory."
            )
            return build_response(event, "query_incident_history", body)

        lines = [f"Found {len(items)} past incident(s) of type '{incident_type}':\n"]
        for index, item in enumerate(items, start=1):
            excerpt = str(item.get("agent_response", ""))[:AGENT_EXCERPT_CHARS]
            lines.append(
                f"\n--- Incident {index} ---\n"
                f"Timestamp: {item.get('timestamp', 'unknown')}\n"
                f"Outcome: {item.get('outcome', 'unknown')}\n"
                f"Agent Analysis (excerpt): {excerpt}\n"
            )

        body = "".join(lines)
        logger.info("History query returned %d item(s)", len(items))
        return build_response(event, "query_incident_history", body)

    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.error(
            "DynamoDB Query ClientError: code=%s message=%s",
            error.get("Code", "Unknown"),
            error.get("Message", str(exc)),
        )
        return build_response(
            event,
            "query_incident_history",
            f"Error querying incident history: {error.get('Message', exc)}. "
            "Proceed without historical context.",
        )
    except Exception as exc:
        logger.error("DynamoDB history query failed: %s\n%s", exc, traceback.format_exc())
        return build_response(
            event,
            "query_incident_history",
            f"Error querying incident history: {exc}. Proceed without historical context.",
        )


def post_to_slack(event: dict, params: dict) -> dict:
    message = params.get("message") or ""
    severity = str(params.get("severity") or "INFO").upper()
    emoji = SEVERITY_EMOJI.get(severity, DEFAULT_SEVERITY_EMOJI)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    logger.info("Posting to Slack: severity=%s message_length=%d", severity, len(message))

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} OPERATOR ZERO — Incident Analysis"},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": message}},
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Operator Zero | Autonomous AWS Agent | {timestamp}"}
                ],
            },
            {"type": "divider"},
        ]
    }

    try:
        # Per-request webhook only — never fall back to a shared Secrets Manager
        # webhook, or every learner would post into the same channel.
        webhook_url = (
            (params.get("webhook_url") or "").strip()
            or (event.get("webhook_url") or "").strip()
        )

        if not webhook_url:
            return build_response(
                event,
                "post_to_slack",
                "Slack post skipped: no webhook URL configured for this session. "
                "The incident is recorded in DynamoDB memory.",
            )

        request = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=SLACK_HTTP_TIMEOUT_SECONDS) as response:
            slack_body = response.read().decode("utf-8")

        logger.info("Slack webhook response: %s", slack_body)
        return build_response(event, "post_to_slack", "Message posted to Slack successfully.")

    except Exception as exc:
        logger.error("Slack post failed: %s\n%s", exc, traceback.format_exc())
        return build_response(
            event,
            "post_to_slack",
            f"Failed to post to Slack: {exc}. "
            f"The undelivered analysis was: {message[:AGENT_EXCERPT_CHARS]}",
        )


def restart_ecs_service(event: dict, params: dict) -> dict:
    cluster_name = params.get("cluster_name") or "operator-zero-cluster"
    service_name = params.get("service_name") or "operator-zero-service"
    reason = params.get("reason") or "Autonomous remediation triggered by Operator Zero"

    try:
        ecs_client.update_service(cluster=cluster_name, service=service_name, forceNewDeployment=True)

        execution_id = f"oz-restart-{datetime.now(timezone.utc).isoformat()}"
        logger.info(
            "ECS service restart initiated: cluster=%s service=%s reason=%s execution=%s",
            cluster_name, service_name, reason, execution_id,
        )

        return build_response(
            event,
            "restart_ecs_service",
            f"ECS service restart initiated successfully.\n"
            f"Cluster: {cluster_name}\n"
            f"Service: {service_name}\n"
            f"Execution ID: {execution_id}\n"
            f"Reason: {reason}\n"
            f"Status: New deployment triggered. Service is restarting.",
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.error(
            "ECS restart ClientError: code=%s message=%s",
            error.get("Code", "Unknown"), error.get("Message", str(exc)),
        )
        return build_response(
            event, "restart_ecs_service",
            f"ECS service restart failed: {error.get('Message', exc)}. Escalate to human operator.",
        )
    except Exception as exc:
        logger.error("ECS restart failed: %s", exc)
        return build_response(
            event, "restart_ecs_service",
            f"ECS service restart failed: {exc}. Escalate to human operator.",
        )


def verify_ecs_service_health(event: dict, params: dict) -> dict:
    cluster_name = params.get("cluster_name") or "operator-zero-cluster"
    service_name = params.get("service_name") or "operator-zero-service"

    try:
        response = ecs_client.describe_services(cluster=cluster_name, services=[service_name])

        services = response.get("services", [])
        if not services:
            return build_response(
                event, "verify_ecs_service_health",
                f"Service {service_name} not found in cluster {cluster_name}.",
            )

        svc = services[0]
        running = svc.get("runningCount", 0)
        desired = svc.get("desiredCount", 1)
        status = svc.get("status", "unknown")
        deployments = svc.get("deployments", [])
        primary = next((d for d in deployments if d.get("status") == "PRIMARY"), {})
        primary_running = primary.get("runningCount", 0)

        healthy = running >= desired and status == "ACTIVE"

        return build_response(
            event, "verify_ecs_service_health",
            f"ECS Service Health Check:\n"
            f"Service: {service_name}\n"
            f"Status: {status}\n"
            f"Running Tasks: {running}/{desired}\n"
            f"Primary Deployment Running: {primary_running}\n"
            f"Health Assessment: {'HEALTHY' if healthy else 'STILL RECOVERING'}\n"
            f"Note: Allow 60-90 seconds for new tasks to reach running state.",
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.error(
            "ECS health check ClientError: code=%s message=%s",
            error.get("Code", "Unknown"), error.get("Message", str(exc)),
        )
        return build_response(
            event, "verify_ecs_service_health",
            f"Health check failed: {error.get('Message', exc)}. Cannot verify service state.",
        )
    except Exception as exc:
        logger.error("ECS health check failed: %s", exc)
        return build_response(
            event, "verify_ecs_service_health",
            f"Health check failed: {exc}. Cannot verify service state.",
        )


def _invoke_harness(harness_arn: str, session_id: str, message_text: str) -> str:
    response = agentcore_client.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        messages=[{"role": "user", "content": [{"text": message_text}]}],
        maxIterations=HARNESS_MAX_ITERATIONS,
        timeoutSeconds=HARNESS_TIMEOUT_SECONDS,
    )

    full_text = ""
    stream = response.get("stream")
    if stream is None:
        return full_text

    for stream_event in stream:
        if "contentBlockDelta" in stream_event:
            delta = stream_event["contentBlockDelta"].get("delta") or {}
            if "text" in delta:
                full_text += delta["text"]
        for err_key in ("internalServerException", "validationException", "runtimeClientError"):
            if err_key in stream_event:
                raise RuntimeError(f"AgentCore harness error ({err_key}): {stream_event[err_key]}")

    return full_text.strip()


def diagnose_incident(event: dict, params: dict) -> dict:
    harness_arn = os.environ.get("DIAGNOSTICS_HARNESS_ARN", "")
    incident_description = params.get("incident_description", "")
    alarm_name = params.get("alarm_name", "unknown")

    if not harness_arn:
        return build_response(
            event, "diagnose_incident",
            "DIAGNOSTICS_HARNESS_ARN not configured on the action-handler Lambda. "
            "Set it after creating the Diagnostics Harness (Step 9).",
        )

    try:
        result = _invoke_harness(
            harness_arn,
            f"diag-{uuid.uuid4()}",
            f"Diagnose this incident: {incident_description}\nAlarm: {alarm_name}",
        )
        return build_response(event, "diagnose_incident", result or "No assessment returned.")
    except Exception as exc:
        logger.error("Diagnostics harness invocation failed: %s\n%s", exc, traceback.format_exc())
        return build_response(
            event, "diagnose_incident",
            f"Diagnostics harness error: {exc}. RECOMMENDED ACTION: ESCALATE_TO_HUMAN",
        )


def execute_remediation(event: dict, params: dict) -> dict:
    harness_arn = os.environ.get("REMEDIATION_HARNESS_ARN", "")
    service_name = params.get("service_name") or "operator-zero-service"
    reason = params.get("reason", "")

    if not harness_arn:
        return build_response(
            event, "execute_remediation",
            "REMEDIATION_HARNESS_ARN not configured on the action-handler Lambda. "
            "Set it after creating the Remediation Harness (Step 10).",
        )

    try:
        result = _invoke_harness(
            harness_arn,
            f"rem-{uuid.uuid4()}",
            f"Execute remediation:\n"
            f"Service: {service_name}\n"
            f"Cluster: operator-zero-cluster\n"
            f"Reason: {reason}",
        )
        return build_response(event, "execute_remediation", result or "No remediation result returned.")
    except Exception as exc:
        logger.error("Remediation harness invocation failed: %s\n%s", exc, traceback.format_exc())
        return build_response(
            event, "execute_remediation",
            f"Remediation harness error: {exc}. ACTION: ESCALATE_TO_HUMAN",
        )


def record_incident_outcome(event: dict, params: dict) -> dict:
    table_name = os.environ.get("MEMORY_TABLE", "operator-zero-incidents")
    timestamp = datetime.now(timezone.utc).isoformat()

    record = {
        "incident_type": params.get("incident_type", "unknown"),
        "timestamp": timestamp,
        "session_id": params.get("session_id", "workshop"),
        "classification": params.get("classification", ""),
        "diagnostics_summary": params.get("diagnostics_summary", ""),
        "action_taken": params.get("action_taken", ""),
        "outcome": params.get("outcome", ""),
        "severity": params.get("severity", "MEDIUM"),
        "agent_response": f"Action: {params.get('action_taken', '')} | Outcome: {params.get('outcome', '')}",
        "source": "supervisor-agent",
        "detail_type": "AutonomousIncidentResolution",
        "raw_event": "{}",
        "ttl": int(time.time()) + TTL_SECONDS,
    }

    try:
        table = dynamodb.Table(table_name)
        table.put_item(Item=record)
        return build_response(
            event, "record_incident_outcome",
            f"Incident report written to DynamoDB.\n"
            f"Incident Type: {record['incident_type']}\n"
            f"Action Taken: {record['action_taken']}\n"
            f"Outcome: {record['outcome']}\n"
            f"Timestamp: {timestamp}",
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.error(
            "DynamoDB PutItem ClientError: code=%s message=%s",
            error.get("Code", "Unknown"), error.get("Message", str(exc)),
        )
        return build_response(
            event, "record_incident_outcome",
            f"Failed to write to DynamoDB: {error.get('Message', exc)}",
        )
    except Exception as exc:
        logger.error("Failed to write incident outcome: %s", exc)
        return build_response(event, "record_incident_outcome", f"Failed to write to DynamoDB: {exc}")


FUNCTION_HANDLERS = {
    "query_incident_history": query_incident_history,
    "post_to_slack": post_to_slack,
    "restart_ecs_service": restart_ecs_service,
    "verify_ecs_service_health": verify_ecs_service_health,
    "diagnose_incident": diagnose_incident,
    "execute_remediation": execute_remediation,
    "record_incident_outcome": record_incident_outcome,
}


def _normalize_invocation(event: dict) -> tuple[str, str, dict]:
    """Accepts both Bedrock Agents Classic and AgentCore Gateway event shapes."""
    if event.get("function") and isinstance(event.get("parameters"), list):
        params = {
            p["name"]: p["value"]
            for p in event.get("parameters", [])
            if "name" in p and "value" in p
        }
        return "agents-classic", event.get("function", ""), params

    function = event.get("toolName") or event.get("name") or event.get("tool_name") or ""
    raw_args = event.get("arguments") or event.get("input") or event.get("parameters") or {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            raw_args = {"value": raw_args}
    if isinstance(raw_args, list):
        params = {
            p["name"]: p["value"]
            for p in raw_args
            if isinstance(p, dict) and "name" in p and "value" in p
        }
    elif isinstance(raw_args, dict):
        params = dict(raw_args)
    else:
        params = {}

    if not function and isinstance(event.get("requestBody"), dict):
        function = event["requestBody"].get("name") or function
        params = event["requestBody"].get("arguments") or params

    return "agentcore", function, params


def handler(event, context):
    event = event or {}
    try:
        logger.info("Action group event: %s", json.dumps(event, default=str))

        mode, function, params = _normalize_invocation(event)

        if "actionGroup" not in event:
            event = {**event, "actionGroup": "OperatorZero", "function": function}

        logger.info("Routing to: %s mode=%s params keys=%s", function, mode, list(params.keys()))

        action_fn = FUNCTION_HANDLERS.get(function)
        if action_fn is not None:
            result = action_fn(event, params)
        else:
            logger.error("Unknown action group function: %s", function)
            result = build_response(
                event,
                function or "unknown",
                f"Unknown function '{function}'. "
                f"Available functions: {', '.join(sorted(FUNCTION_HANDLERS))}.",
            )

        if mode == "agentcore":
            body_text = (
                result.get("functionResponse", {})
                .get("responseBody", {})
                .get("TEXT", {})
                .get("body", json.dumps(result))
            )
            result = {
                "message": body_text,
                "content": [{"type": "text", "text": body_text}],
                "functionResponse": result.get("functionResponse"),
            }

        return result

    except Exception as exc:
        logger.error("Unhandled action-handler error: %s\n%s", exc, traceback.format_exc())
        return build_response(
            event if isinstance(event, dict) else {},
            (event or {}).get("function", "unknown") if isinstance(event, dict) else "unknown",
            f"Action handler error: {exc}. Proceed without this tool result.",
        )
