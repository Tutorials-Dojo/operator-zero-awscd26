"""
Operator Zero — Dispatcher Lambda

Entry point for the incident pipeline. EventBridge invokes this function when
a CloudWatch alarm (or Config / GuardDuty finding) enters ALARM state. It
formats the event into a natural-language brief, invokes reasoning, and
persists the outcome to DynamoDB for 90-day incident memory.

Reasoning path is selected by AGENT_ID:
  AGENTCORE_HARNESS   → invoke_harness on HARNESS_ARN (the workshop path)
  COMPAT_CONVERSE     → Converse + tool-use compatibility mode
  <real Bedrock Agent id> → Bedrock Agents Classic invoke_agent

Environment:
  AGENT_ID              AGENTCORE_HARNESS, COMPAT_CONVERSE, or a Bedrock Agent ID
  HARNESS_ARN           Supervisor Harness ARN (AgentCore mode)
  AGENT_ALIAS_ID        Bedrock Agent Alias ID (Agents Classic mode only)
  BEDROCK_MODEL_ID      Inference profile id (compat mode)
  ACTION_HANDLER_NAME   Action-handler function name (compat mode)
  MEMORY_TABLE          DynamoDB table name
  SLACK_SECRET_NAME     Secrets Manager secret for Slack webhook
  AWS_ACCOUNT_ID        AWS account ID
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

bedrock_runtime = boto3.client("bedrock-agent-runtime")
bedrock_converse = boto3.client("bedrock-runtime")
bedrock_agentcore = boto3.client("bedrock-agentcore")
lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")

PLACEHOLDER = "PLACEHOLDER_UPDATE_AFTER_AGENT_CREATION"
COMPAT_AGENT_ID = "COMPAT_CONVERSE"
AGENTCORE_AGENT_ID = "AGENTCORE_HARNESS"
TTL_SECONDS = 90 * 24 * 60 * 60
AGENT_RESPONSE_MAX_CHARS = 4000
RAW_EVENT_MAX_CHARS = 2000
MAX_TOOL_ROUNDS = 8
DEFAULT_MODEL_ID = "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
PROMPT_PATH = Path(__file__).with_name("supervisor-system-prompt.txt")


def validate_config() -> None:
    agent_id = os.environ.get("AGENT_ID", "")
    if agent_id == AGENTCORE_AGENT_ID:
        harness_arn = os.environ.get("HARNESS_ARN", "")
        if not harness_arn or harness_arn == PLACEHOLDER:
            logger.warning(
                "Dispatcher misconfigured: HARNESS_ARN still unset or PLACEHOLDER. "
                "Set it to your Supervisor Harness ARN (workshop guide Step 14)."
            )
        return

    alias_id = os.environ.get("AGENT_ALIAS_ID", "")
    problems = []
    if not agent_id or agent_id == PLACEHOLDER:
        problems.append("AGENT_ID")
    if not alias_id or alias_id == PLACEHOLDER:
        problems.append("AGENT_ALIAS_ID")
    if problems:
        logger.warning(
            "Dispatcher misconfigured: %s still unset or set to PLACEHOLDER. "
            "Set env vars via aws lambda update-function-configuration.",
            ", ".join(problems),
        )


validate_config()

TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "query_incident_history",
                "description": (
                    "Query DynamoDB for past incidents of the specified type "
                    "to inform current reasoning"
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "incident_type": {
                                "type": "string",
                                "description": (
                                    "The alarm name or incident type to search history for"
                                ),
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of past incidents to return",
                            },
                        },
                        "required": ["incident_type"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "post_to_slack",
                "description": (
                    "Post a structured incident analysis and decision to the Slack channel"
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": (
                                    "The complete incident analysis including "
                                    "classification, reasoning, and recommended actions"
                                ),
                            },
                            "severity": {
                                "type": "string",
                                "description": (
                                    "Severity level for formatting: "
                                    "CRITICAL, HIGH, MEDIUM, LOW, or INFO"
                                ),
                            },
                        },
                        "required": ["message"],
                    }
                },
            }
        },
    ]
}


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
        f"reason about root cause and blast radius, then post your full "
        f"analysis and decision to Slack."
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
        "reason about root cause and blast radius, then post your full "
        "analysis and decision to Slack."
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
        "occurrences, reason about severity and blast radius, then post your "
        "full analysis and decision to Slack."
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
        "assess the threat level and blast radius, then post your full "
        "analysis and escalation recommendation to Slack."
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


def _require_agent_ids() -> tuple[str, str]:
    agent_id = os.environ.get("AGENT_ID", "")
    agent_alias_id = os.environ.get("AGENT_ALIAS_ID", "")

    if agent_id == AGENTCORE_AGENT_ID:
        harness_arn = os.environ.get("HARNESS_ARN", "")
        if not harness_arn or harness_arn == PLACEHOLDER:
            raise ValueError(
                "HARNESS_ARN still unset or a placeholder. "
                "Set it to your Supervisor Harness ARN (Step 14)."
            )
        return agent_id, agent_alias_id

    missing = [
        name
        for name, value in (("AGENT_ID", agent_id), ("AGENT_ALIAS_ID", agent_alias_id))
        if not value or value == PLACEHOLDER
    ]
    if missing:
        raise ValueError(
            f"{', '.join(missing)} still set to placeholder. "
            "Set AGENT_ID=AGENTCORE_HARNESS and HARNESS_ARN to use AgentCore, "
            "or provide a Bedrock Agents Classic AGENT_ID and AGENT_ALIAS_ID."
        )
    return agent_id, agent_alias_id


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


def _stream_agent_completion(completion: Any) -> tuple[str, list]:
    full_response = ""
    trace_log: list = []
    for stream_event in completion:
        if "chunk" in stream_event:
            payload = stream_event["chunk"].get("bytes") or b""
            if isinstance(payload, (bytes, bytearray)):
                full_response += payload.decode("utf-8", errors="replace")
            else:
                full_response += str(payload)
        if "trace" in stream_event:
            trace = stream_event["trace"]
            trace_log.append(trace)
    return full_response, trace_log


def _invoke_action_handler(function_name: str, params: dict) -> str:
    action_group = (
        "IncidentMemory"
        if function_name == "query_incident_history"
        else "SlackNotifications"
    )
    payload = {
        "actionGroup": action_group,
        "function": function_name,
        "parameters": [
            {"name": key, "type": "string", "value": str(value)}
            for key, value in params.items()
        ],
    }
    function = os.environ.get("ACTION_HANDLER_NAME", "operator-zero-action-handler")
    try:
        resp = lambda_client.invoke(
            FunctionName=function,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.error(
            "Action-handler invoke ClientError: code=%s message=%s",
            error.get("Code", "Unknown"),
            error.get("Message", str(exc)),
        )
        raise
    raw = resp["Payload"].read().decode("utf-8")
    try:
        body = json.loads(raw)
        return (
            body.get("functionResponse", {})
            .get("responseBody", {})
            .get("TEXT", {})
            .get("body")
            or body.get("message")
            or raw
        )
    except json.JSONDecodeError:
        return raw


def _run_converse_agent(incident_message: str) -> tuple[str, list]:
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    messages: list[dict] = [
        {"role": "user", "content": [{"text": incident_message}]}
    ]
    trace_log: list = []
    final_text_parts: list[str] = []

    for round_idx in range(MAX_TOOL_ROUNDS):
        logger.info("Converse round %d model=%s", round_idx + 1, model_id)
        response = bedrock_converse.converse(
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=messages,
            toolConfig=TOOL_CONFIG,
            inferenceConfig={"maxTokens": 4096, "temperature": 0.2},
        )
        output_message = response["output"]["message"]
        stop_reason = response.get("stopReason")
        messages.append(output_message)
        trace_log.append({"round": round_idx + 1, "stopReason": stop_reason})

        tool_uses = [
            block["toolUse"]
            for block in output_message.get("content", [])
            if "toolUse" in block
        ]
        final_text_parts.extend(
            block["text"]
            for block in output_message.get("content", [])
            if "text" in block
        )

        if stop_reason != "tool_use" or not tool_uses:
            break

        tool_results = []
        for tool in tool_uses:
            name = tool["name"]
            tool_use_id = tool["toolUseId"]
            tool_input = tool.get("input") or {}
            logger.info("Tool call: %s input=%s", name, json.dumps(tool_input))
            try:
                result_text = _invoke_action_handler(name, tool_input)
                status = "success"
            except Exception as exc:
                logger.error("Tool %s failed: %s", name, exc)
                result_text = f"Tool error: {exc}"
                status = "error"
            tool_results.append(
                {
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": [{"text": result_text}],
                        "status": status,
                    }
                }
            )
            trace_log.append({"tool": name, "status": status})

        messages.append({"role": "user", "content": tool_results})

    return "\n".join(p for p in final_text_parts if p).strip(), trace_log


def _run_bedrock_agent(
    agent_id: str,
    agent_alias_id: str,
    session_id: str,
    incident_message: str,
) -> tuple[str, list]:
    response = bedrock_runtime.invoke_agent(
        agentId=agent_id,
        agentAliasId=agent_alias_id,
        sessionId=session_id,
        inputText=incident_message,
        enableTrace=True,
    )
    return _stream_agent_completion(response.get("completion", []))


def _run_agentcore_harness(session_id: str, incident_message: str) -> tuple[str, list]:
    harness_arn = os.environ.get("HARNESS_ARN")
    if not harness_arn:
        raise ValueError("HARNESS_ARN is required when AGENT_ID=AGENTCORE_HARNESS")

    response = bedrock_agentcore.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        messages=[
            {
                "role": "user",
                "content": [{"text": incident_message}],
            }
        ],
        maxIterations=8,
        timeoutSeconds=180,
    )

    full_response = ""
    trace_log: list = []
    stream = response.get("stream")
    if stream is None:
        return full_response, trace_log

    for event in stream:
        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta") or {}
            if "text" in delta:
                full_response += delta["text"]
        if "messageStop" in event:
            trace_log.append({"messageStop": event["messageStop"]})
        if "metadata" in event:
            trace_log.append({"metadata": event["metadata"]})
        for err_key in (
            "internalServerException",
            "validationException",
            "runtimeClientError",
        ):
            if err_key in event:
                raise RuntimeError(f"AgentCore harness error ({err_key}): {event[err_key]}")

    return full_response.strip(), trace_log


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event, default=str))

    agent_id, agent_alias_id = _require_agent_ids()
    lab_session_id = _resolve_lab_session_id(event or {})
    session_id = re.sub(r"[^a-zA-Z0-9\-_]", "_", f"{lab_session_id}_{context.aws_request_id}"[:100])
    if session_id and not session_id[0].isalnum():
        session_id = "oz" + session_id
    timestamp = datetime.now(timezone.utc).isoformat()
    incident_type = _resolve_incident_type(event)
    incident_message = format_incident_message(event)
    if agent_id == AGENTCORE_AGENT_ID:
        mode = "agentcore"
    elif agent_id == COMPAT_AGENT_ID:
        mode = "compat"
    else:
        mode = "agents-api"

    logger.info(
        "Dispatching incident: type=%s lab_session=%s agent_session=%s mode=%s",
        incident_type,
        lab_session_id,
        session_id,
        mode,
    )
    logger.info("Incident brief:\n%s", incident_message)

    try:
        if agent_id == AGENTCORE_AGENT_ID:
            full_response, trace_log = _run_agentcore_harness(
                session_id, incident_message
            )
        elif agent_id == COMPAT_AGENT_ID:
            full_response, trace_log = _run_converse_agent(incident_message)
        else:
            full_response, trace_log = _run_bedrock_agent(
                agent_id, agent_alias_id, session_id, incident_message
            )

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
                    "mode": mode,
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
            "Bedrock ClientError (returning degraded 200): code=%s message=%s",
            code,
            message,
        )
        save_incident_to_memory(
            incident_type,
            timestamp,
            f"[DEGRADED] Bedrock ClientError {code}: {message}",
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
                    "mode": mode,
                    "errorCode": code,
                    "errorMessage": message,
                }
            ),
        }

    except Exception as exc:
        logger.error(
            "Bedrock invocation failed (returning degraded 200):\n%s",
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
                    "mode": mode,
                    "errorMessage": str(exc),
                }
            ),
        }
