"""
Operator Zero — Chaos Trigger Lambda

Simulates infrastructure incidents for the workshop and live demo.

Safety contract (non-negotiable):
  - NEVER describe, stop, scale, or otherwise mutate ECS tasks or services.
  - Only publish a custom CloudWatch metric and/or force an alarm state change.
  - Unknown actions return an error payload; this Lambda must not raise to the caller.
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

cloudwatch = boto3.client("cloudwatch")

METRIC_NAMESPACE = "OperatorZero/Chaos"
METRIC_NAME = "SimulatedCPUUtilization"
SIMULATED_CPU_VALUE = 95.0


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Required environment variable '{name}' is not set")
    return value


def trigger_cpu_spike(duration_seconds: int, session_id: str = "default") -> dict:
    cluster_name = _required_env("ECS_CLUSTER_NAME")
    service_name = _required_env("ECS_SERVICE_NAME")
    alarm_name = _required_env("ALARM_NAME")

    logger.info(
        "Triggering CPU spike simulation: duration=%ds alarm=%s "
        "cluster=%s service=%s session=%s",
        duration_seconds,
        alarm_name,
        cluster_name,
        service_name,
        session_id,
    )

    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": METRIC_NAME,
                    "Value": SIMULATED_CPU_VALUE,
                    "Unit": "Percent",
                    "Dimensions": [
                        {"Name": "ClusterName", "Value": cluster_name},
                        {"Name": "ServiceName", "Value": service_name},
                    ],
                }
            ],
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.error(
            "CloudWatch PutMetricData ClientError: code=%s message=%s",
            error.get("Code", "Unknown"),
            error.get("Message", str(exc)),
        )
        raise

    logger.info(
        "Published %s/%s=%.1f for cluster=%s service=%s session=%s",
        METRIC_NAMESPACE,
        METRIC_NAME,
        SIMULATED_CPU_VALUE,
        cluster_name,
        service_name,
        session_id,
    )

    try:
        cloudwatch.set_alarm_state(
            AlarmName=alarm_name,
            StateValue="ALARM",
            StateReason=(
                f"[CHAOS][session:{session_id}] Simulated CPU spike for "
                f"Operator Zero workshop. Duration: {duration_seconds}s"
            ),
        )
        logger.info("Alarm %s forced to ALARM (session=%s)", alarm_name, session_id)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.warning(
            "Could not set alarm state for %s: code=%s message=%s. "
            "Metric was published; alarm will trigger naturally.",
            alarm_name,
            error.get("Code", "Unknown"),
            error.get("Message", str(exc)),
        )
    except Exception as exc:
        logger.warning(
            "Could not set alarm state directly for %s: %s. "
            "Metric was published; alarm will trigger naturally.",
            alarm_name,
            exc,
        )

    return {
        "action": "spike_cpu",
        "status": "triggered",
        "alarm": alarm_name,
        "duration_seconds": duration_seconds,
        "session_id": session_id,
        "message": "CPU spike simulation triggered. Watch your Slack channel.",
    }


def reset_alarms(session_id: str = "default") -> dict:
    alarm_name = _required_env("ALARM_NAME")

    logger.info("Resetting alarm %s to OK (session=%s)", alarm_name, session_id)
    try:
        cloudwatch.set_alarm_state(
            AlarmName=alarm_name,
            StateValue="OK",
            StateReason=f"[CHAOS][session:{session_id}] Reset by chaos Lambda",
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        logger.error(
            "CloudWatch SetAlarmState ClientError: code=%s message=%s",
            error.get("Code", "Unknown"),
            error.get("Message", str(exc)),
        )
        raise

    return {
        "action": "reset",
        "status": "ok",
        "alarm": alarm_name,
        "session_id": session_id,
    }


def handler(event, context):
    logger.info("Chaos event: %s", json.dumps(event or {}, default=str))

    event = event or {}
    action = event.get("action", "spike_cpu")
    session_id = str(event.get("session_id") or "default")

    try:
        duration = int(event.get("duration_seconds", 120))
    except (TypeError, ValueError):
        duration = 120

    try:
        if action == "spike_cpu":
            result = trigger_cpu_spike(duration, session_id=session_id)
        elif action == "reset":
            result = reset_alarms(session_id=session_id)
        else:
            result = {
                "action": action,
                "status": "error",
                "session_id": session_id,
                "error": f"Unknown action '{action}'. Available: spike_cpu, reset",
            }
    except Exception as exc:
        logger.error("Chaos action failed: %s", exc, exc_info=True)
        result = {
            "action": action,
            "status": "error",
            "session_id": session_id,
            "error": str(exc),
        }

    logger.info("Chaos result: %s", json.dumps(result, default=str))
    return result
