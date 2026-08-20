"""
Operator Zero — Dispatcher Lambda

Entry point for the incident pipeline. EventBridge invokes this function when
a CloudWatch alarm (or Config / GuardDuty finding) enters ALARM state. It
formats the event into a natural-language brief, invokes the Supervisor
Harness via AgentCore's invoke_harness, and persists the outcome to DynamoDB
for 90-day incident memory.

Environment:
  HARNESS_ARN   Supervisor Harness ARN (Step 13)
  MEMORY_TABLE  DynamoDB table name
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")

TTL_SECONDS = 90 * 24 * 60 * 60
AGENT_RESPONSE_MAX_CHARS = 4000
RAW_EVENT_MAX_CHARS = 2000
HARNESS_MAX_ITERATIONS = 8
# The Supervisor's own reasoning chain calls diagnose_incident and
# execute_remediation, each of which can itself run up to ~90-110s (see
# action-handler's HARNESS_TIMEOUT_SECONDS/read_timeout) — so 180s here was
# too tight to ever complete both delegated calls plus its own reasoning.
HARNESS_TIMEOUT_SECONDS = 270

# boto3's default read_timeout (60s) is far shorter than a full Supervisor
# reasoning chain — the client socket was timing out long before AgentCore's
# own timeoutSeconds allowance ran out. read_timeout is set with headroom
# above HARNESS_TIMEOUT_SECONDS but under the Lambda's function timeout
# (5 minutes, see CDK) so a genuine timeout returns cleanly. Retries
# disabled — retrying a slow multi-harness chain doubles latency/cost.
bedrock_agentcore = boto3.client(
    "bedrock-agentcore",
    config=Config(connect_timeout=10, read_timeout=285, retries={"max_attempts": 1}),
)


def validate_config() -> None:
    if not os.environ.get("HARNESS_ARN", ""):
        logger.warning(
            "Dispatcher misconfigured: HARNESS_ARN still unset. "
            "Set it to your Supervisor Harness ARN (workshop guide Step 13)."
        )


validate_config()


def format_incident_message(event: dict) -> str:
    source = event.get("source", "unknown")
    detail_type = event.get("detail-type", "unknown")
    detail = event.get("detail") or {}
    timestamp = event.get("time") or datetime.now(timezone.utc).isoformat()

    if source == "aws.cloudwatch" or "CloudWatch" in detail_type:
        return _format_cloudwatch(detail, timestamp)
    if source == "aws.config":
        return _format_config(detail, timestamp)
    if source == "aws.guardduty":
        return _format_guardduty(detail, timestamp)

    return (
        f"INCIDENT DETECTED — {detail_type}\n"
        f"Source: {source}\n"
        f"Detail Type: {detail_type}\n"
        f"Detail: {json.dumps(detail, default=str)}\n"
        f"Time: {timestamp}\n\n"
        f"Please classify this incident, recall any past occurrences, "
        f"reason about root cause and blast radius, then record the outcome."
    )


def _format_cloudwatch(detail: dict, timestamp: str) -> str:
    alarm = detail.get("alarm") or {}
    state = detail.get("state") or {}
    alarm_name = detail.get("alarmName") or alarm.get("name") or "unknown"
    alarm_state = state.get("value") or detail.get("newStateValue") or "ALARM"
    reason = state.get("reason") or detail.get("newStateReason") or "No reason provided"
    return (
        "INCIDENT DETECTED — CloudWatch Alarm\n"
        f"Alarm Name: {alarm_name}\n"
        f"New State: {alarm_state}\n"
        f"Reason: {reason}\n"
        f"Time: {timestamp}\n\n"
        "Please classify this incident, recall any past occurrences, "
        "reason about root cause and blast radius, then record the outcome."
    )


def _format_config(detail: dict, timestamp: str) -> str:
    evaluation = detail.get("newEvaluationResult") or {}
    rule_name = detail.get("configRuleName") or "unknown"
    resource_id = detail.get("resourceId") or "unknown"
    compliance = evaluation.get("complianceType") or "NON_COMPLIANT"
    return (
        "INCIDENT DETECTED — AWS Config Rule Violation\n"
        f"Rule: {rule_name}\n"
        f"Resource: {resource_id}\n"
        f"Compliance Status: {compliance}\n"
        f"Time: {timestamp}\n\n"
        "Please classify this security/compliance incident, recall any past "
        "occurrences, reason about severity and blast radius, then record the outcome."
    )


def _format_guardduty(detail: dict, timestamp: str) -> str:
    resource = detail.get("resource") or {}
    finding_type = detail.get("type") or "unknown"
    severity = detail.get("severity", 0)
    resource_type = resource.get("resourceType") or "unknown"
    return (
        "INCIDENT DETECTED — GuardDuty Security Finding\n"
        f"Finding Type: {finding_type}\n"
        f"Severity Score: {severity}/10\n"
        f"Affected Resource Type: {resource_type}\n"
        f"Time: {timestamp}\n\n"
        "Please classify this security incident, recall any past occurrences, "
        "assess the threat level and blast radius, then record the outcome."
    )


def _resolve_lab_session_id(event: dict) -> str:
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    for candidate in (
        event.get("session_id"),
        detail.get("session_id"),
        event.get("lab_session_id"),
        detail.get("lab_session_id"),
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()

    # Chaos Lambda embeds [CHAOS][session:<uuid>] in the CloudWatch StateReason.
    state = detail.get("state") if isinstance(detail.get("state"), dict) else {}
    reason = str(
        state.get("reason")
        or detail.get("newStateReason")
        or event.get("StateReason")
        or ""
    )
    marker = "[session:"
    if marker in reason:
        start = reason.index(marker) + len(marker)
        end = reason.find("]", start)
        if end > start:
            embedded = reason[start:end].strip()
            if embedded:
                return embedded
    return "default"


def save_incident_to_memory(
    incident_type: str,
    timestamp: str,
    agent_response: str,
    event: dict,
    session_id: str = "default",
) -> None:
    table_name = os.environ.get("MEMORY_TABLE")
    if not table_name:
        logger.error("MEMORY_TABLE is not set; skipping incident memory write")
        return

    try:
        table = dynamodb.Table(table_name)
        table.put_item(
            Item={
                "incident_type": incident_type,
                "timestamp": timestamp,
                "session_id": session_id,
                "pk": f"{session_id}#{incident_type}",
                "agent_response": (agent_response or "")[:AGENT_RESPONSE_MAX_CHARS],
                "source": event.get("source", "unknown"),
                "detail_type": event.get("detail-type", "unknown"),
                "raw_event": json.dumps(event, default=str)[:RAW_EVENT_MAX_CHARS],
                "outcome": "processed",
                "ttl": int(time.time()) + TTL_SECONDS,
            }
        )
        logger.info(
            "Incident saved to memory: type=%s timestamp=%s session=%s",
            incident_type,
            timestamp,
            session_id,
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.error(
            "DynamoDB PutItem ClientError (non-fatal): code=%s message=%s",
            error.get("Code", "Unknown"),
            error.get("Message", str(exc)),
        )
    except Exception:
        logger.error(
            "Failed to save incident to memory (non-fatal): %s",
            traceback.format_exc(),
        )


def _require_harness_arn() -> str:
    harness_arn = os.environ.get("HARNESS_ARN", "")
    if not harness_arn:
        raise ValueError(
            "HARNESS_ARN still unset. Set it to your Supervisor Harness ARN (Step 13)."
        )
    return harness_arn


def _resolve_incident_type(event: dict) -> str:
    detail = event.get("detail") or {}
    alarm = detail.get("alarm") or {}
    return (
        detail.get("alarmName")
        or alarm.get("name")
        or detail.get("configRuleName")
        or detail.get("type")
        or event.get("detail-type")
        or "unknown"
    )


def _run_agentcore_harness(harness_arn: str, session_id: str, incident_message: str) -> tuple[str, list]:
    response = bedrock_agentcore.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        messages=[{"role": "user", "content": [{"text": incident_message}]}],
        maxIterations=HARNESS_MAX_ITERATIONS,
        timeoutSeconds=HARNESS_TIMEOUT_SECONDS,
    )

    full_response = ""
    trace_log: list = []
    stream = response.get("stream")
    if stream is None:
        return full_response, trace_log

    for event_item in stream:
        if "contentBlockDelta" in event_item:
            delta = event_item["contentBlockDelta"].get("delta") or {}
            if "text" in delta:
                full_response += delta["text"]
        if "messageStop" in event_item:
            trace_log.append({"messageStop": event_item["messageStop"]})
        if "metadata" in event_item:
            trace_log.append({"metadata": event_item["metadata"]})
        for err_key in ("internalServerException", "validationException", "runtimeClientError"):
            if err_key in event_item:
                raise RuntimeError(f"AgentCore harness error ({err_key}): {event_item[err_key]}")

    return full_response.strip(), trace_log


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event, default=str))

    harness_arn = _require_harness_arn()
    lab_session_id = _resolve_lab_session_id(event or {})
    session_id = re.sub(r"[^a-zA-Z0-9\-_]", "_", f"{lab_session_id}_{context.aws_request_id}"[:100])
    if session_id and not session_id[0].isalnum():
        session_id = "oz" + session_id
    timestamp = datetime.now(timezone.utc).isoformat()
    incident_type = _resolve_incident_type(event)
    incident_message = format_incident_message(event)

    logger.info(
        "Dispatching incident: type=%s lab_session=%s agent_session=%s",
        incident_type,
        lab_session_id,
        session_id,
    )
    logger.info("Incident brief:\n%s", incident_message)

    try:
        full_response, trace_log = _run_agentcore_harness(harness_arn, session_id, incident_message)

        logger.info(
            "Agent response length=%d preview=%s",
            len(full_response),
            full_response[:500],
        )
        logger.info("Trace event count: %d", len(trace_log))
        save_incident_to_memory(
            incident_type, timestamp, full_response, event, lab_session_id
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "status": "ok",
                    "sessionId": session_id,
                    "labSessionId": lab_session_id,
                    "incidentType": incident_type,
                    "responseLength": len(full_response),
                    "traceEventCount": len(trace_log),
                }
            ),
        }

    except ValueError:
        raise

    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code", "Unknown")
        message = error.get("Message", str(exc))
        logger.error(
            "AgentCore ClientError (returning degraded 200): code=%s message=%s",
            code,
            message,
        )
        save_incident_to_memory(
            incident_type,
            timestamp,
            f"[DEGRADED] AgentCore ClientError {code}: {message}",
            event,
            lab_session_id,
        )
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "status": "degraded",
                    "sessionId": session_id,
                    "labSessionId": lab_session_id,
                    "incidentType": incident_type,
                    "errorCode": code,
                    "errorMessage": message,
                }
            ),
        }

    except Exception as exc:
        logger.error(
            "AgentCore invocation failed (returning degraded 200):\n%s",
            traceback.format_exc(),
        )
        save_incident_to_memory(
            incident_type,
            timestamp,
            f"[DEGRADED] {type(exc).__name__}: {exc}",
            event,
            lab_session_id,
        )
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "status": "degraded",
                    "sessionId": session_id,
                    "labSessionId": lab_session_id,
                    "incidentType": incident_type,
                    "errorMessage": str(exc),
                }
            ),
        }
