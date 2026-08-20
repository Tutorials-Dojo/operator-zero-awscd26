# OPERATOR ZERO — PART 2

## Build the Autonomous Multi-Agent System

**Region:** `us-east-1` (N. Virginia) — use this region for every single step, every command, every console page.

Continuing from [Part 1](OPERATOR-ZERO-WORKSHOP-GUIDE-PART1.md), where you built a single standalone Harness (`my_first_agent`) and learned the mechanics: model, system prompt, tools, playground.

### Recap: what you're building in this part

```
CloudWatch Alarm ──▶ EventBridge ──▶ dispatcher Lambda ──▶ Supervisor Harness
                                                                  │
                                                    ┌─────────────┼─────────────┐
                                                    ▼             ▼             ▼
                                          diagnose_incident  execute_remediation  record_incident_outcome
                                                    │             │             │
                                                    ▼             ▼             ▼
                                          Diagnostics Harness  Remediation Harness  DynamoDB
                                                    │             │
                                                    ▼             ▼
                                          query_incident_history  restart_ecs_service
                                          (via Gateway → action-handler Lambda)  verify_ecs_service_health
```

All tool calls — from every Harness — route through **one AgentCore Gateway** to **one Lambda function**, `operator-zero-action-handler`. That Lambda is the only thing that touches DynamoDB, ECS, and the sub-Harnesses. You configure *what it's allowed to talk to* entirely through environment variables — you never edit its code in the console.

---

# PART 2 — Build the Autonomous Multi-Agent System

**65 minutes.** You deploy real infrastructure, create three Harnesses (Supervisor, Diagnostics, Remediation), wire them together through one Gateway, and watch the full autonomous incident lifecycle run with no human input after the trigger.

**Key idea for this whole part:** every Lambda you deploy is already complete. You never paste code into a Lambda code editor. You only ever set environment variables — the ARNs of things you create in the console. This mirrors how you'd actually configure a production system.

## STEP 7 — Deploy Base Infrastructure with CDK

**15 minutes**

**What this creates:** the Lambda functions (already fully coded), the DynamoDB incident-memory table, an ECS Fargate service to monitor, a CloudWatch alarm, a disabled EventBridge rule, and the shared IAM role with exactly the permissions the Lambdas need — no manual policy-attaching required.

**7.1 — Open CloudShell**
Click the `>_` terminal icon in the top navbar. Wait for the `$` prompt (30–60s on first open). Confirm the tab shows region `us-east-1`.

**7.2 — Clone the repository**
```bash
git clone https://github.com/Tutorials-Dojo/operator-zero-awscd26.git
cd operator-zero-awscd26
```

**7.3 — Install CDK dependencies**
```bash
cd cdk
npm install
```
Takes 1–2 minutes.

**7.4 — Bootstrap CDK**
```bash
cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/us-east-1
```
Wait for `✅ Environment ... bootstrapped`.

> ⚠️ **Banner before your command runs:**
> ```
> ****************************************************
> *** Newer version of CDK is available [2.1138.0] ***
> *** Upgrade recommended (npm install -g aws-cdk)  ***
> ****************************************************
> ```
> This alone is not an error — it's the CDK CLI politely nagging you every time it runs. Ignore it *unless* it's immediately followed by:
> ```
> This CDK CLI is not compatible with the CDK library used by your application.
> Please upgrade the CLI to the latest version.
> (Cloud assembly schema version mismatch: Maximum schema version supported is 53.x.x,
> but found 54.0.0. You need at least CLI version 2.1138.0 to read this manifest.)
> ```
> That second block **is** an error — the CLI pre-installed in CloudShell is older than the CDK library this repo pins. Fix:
> ```bash
> sudo npm install -g aws-cdk
> ```
> (CloudShell's system Node folder needs `sudo` for a global install — that's expected here, unlike a typical local machine.) If `sudo` isn't available or still fails with `EACCES permission denied`, install it locally into the project instead:
> ```bash
> npm install aws-cdk
> export PATH=$(pwd)/node_modules/.bin:$PATH
> cdk --version
> ```
> Then re-run the bootstrap command.

> ⚠️ **`[WARNING] aws-cdk-lib.aws_ecs.ClusterProps#containerInsights is deprecated`, `...TableOptions#pointInTimeRecovery is deprecated`, or `...FunctionOptions#logRetention is deprecated` during `cdk bootstrap` or `cdk deploy`:**
> These three specific deprecation warnings mean you're running an older copy of this repo — this repo's `cdk/lib/base-infra-stack.ts` already uses the non-deprecated replacements (`containerInsightsV2`, `pointInTimeRecoverySpecification`, and an explicit `logGroup` per function), so a fresh clone produces none of them. Re-clone from Step 7.2 if you see these. Any *other* yellow `WARNING` line (not one of these three) is informational and safe to ignore — only red `Error` lines block the deploy.

**7.5 — Deploy the stack**
```bash
cdk deploy OperatorZeroBaseStack --require-approval never
```
**This takes 8–12 minutes.** CDK bundles each Lambda's dependencies (pip-installs a fresh `boto3` alongside the handler — this is what makes AgentCore's `bedrock-agentcore` client available; the Lambda runtime's built-in boto3 is too old). Watch resources being created; CDK prints a summary when done.

**7.6 — Verify the deployment**
```bash
# Lambda functions
aws lambda list-functions --region us-east-1 \
  --query 'Functions[?starts_with(FunctionName, `operator-zero`)].FunctionName' \
  --output table

# DynamoDB table
aws dynamodb describe-table --table-name operator-zero-incidents --region us-east-1 \
  --query 'Table.{Name:TableName,Status:TableStatus}' --output table

# ECS service
aws ecs describe-services --cluster operator-zero-cluster \
  --services operator-zero-service --region us-east-1 \
  --query 'services[0].{Service:serviceName,Status:status,Running:runningCount}' --output table
```

> ⚠️ **Terminal shows `(END)` and won't accept input:** you're in a pager. Press **q** to exit.

**Verify:** `operator-zero-dispatcher`, `operator-zero-action-handler`, `operator-zero-chaos-trigger` all listed; DynamoDB `Status: ACTIVE`; ECS `Status: ACTIVE`, `Running: 1`.

## STEP 8 — Create the AgentCore Gateway

**8 minutes**

**What this is:** the Gateway is how AgentCore connects your Harnesses to the `operator-zero-action-handler` Lambda. You create it once; all three Harnesses share it.

**8.1** — AgentCore sidebar → **Build** → **Gateways** → **Create gateway**

**8.2** — Gateway name: clear the auto-generated value, type `operator-zero-gateway` (a-z, A-Z, 0-9, hyphen, max 50 chars)

**8.3 — Permissions** — keep **Create default role** selected

**8.4 — Inbound Auth** — select **Use IAM permissions** (Signature V4, no extra config)

**8.5 — Configure the Target**
1. **Select a target protocol** → **MCP target**
2. **Target name** → clear and type `operator-zero-action-handler-target`
3. **Target type** → **Lambda ARN**
4. Paste the ARN, replacing `YOUR_ACCOUNT_ID` (find it in the top-right account menu):
   ```
   arn:aws:lambda:us-east-1:YOUR_ACCOUNT_ID:function:operator-zero-action-handler
   ```

**8.6 — Tool schema (this defines what tools the Gateway exposes to your Harnesses)**
Find the **Tool schema** field under the target configuration → select **Inline** → paste the contents of [`operator-zero-awscd26/lambda/action-handler/gateway-tool-schema.json`](operator-zero-awscd26/lambda/action-handler/gateway-tool-schema.json), reproduced here for convenience:

```json
[
  {
    "name": "query_incident_history",
    "description": "Query DynamoDB for past incidents of the specified type to inform current reasoning.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "incident_type": { "type": "string", "description": "The alarm name or incident type to search history for." },
        "limit": { "type": "integer", "description": "Maximum number of past incidents to return. Defaults to 5." },
        "session_id": { "type": "string", "description": "Workshop lab session id, if scoping history to one learner's session." }
      },
      "required": ["incident_type"]
    }
  },
  {
    "name": "post_to_slack",
    "description": "Post a structured incident analysis and decision to a Slack channel via Incoming Webhook.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "message": { "type": "string", "description": "The complete incident analysis including classification, reasoning, and recommended actions." },
        "severity": { "type": "string", "description": "Severity level for formatting: CRITICAL, HIGH, MEDIUM, LOW, or INFO." },
        "webhook_url": { "type": "string", "description": "Slack Incoming Webhook URL for this session, if not configured via Secrets Manager." }
      },
      "required": ["message"]
    }
  },
  {
    "name": "restart_ecs_service",
    "description": "Force a new ECS deployment to restart all tasks in the specified service. Only operates on operator-zero-cluster.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "cluster_name": { "type": "string", "description": "ECS cluster name. Defaults to operator-zero-cluster." },
        "service_name": { "type": "string", "description": "ECS service name. Defaults to operator-zero-service." },
        "reason": { "type": "string", "description": "Why this restart is being triggered, for the audit trail." }
      },
      "required": []
    }
  },
  {
    "name": "verify_ecs_service_health",
    "description": "Check running vs desired task count for an ECS service to confirm recovery after a restart.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "cluster_name": { "type": "string", "description": "ECS cluster name. Defaults to operator-zero-cluster." },
        "service_name": { "type": "string", "description": "ECS service name. Defaults to operator-zero-service." }
      },
      "required": []
    }
  },
  {
    "name": "diagnose_incident",
    "description": "Delegate root-cause analysis to the Diagnostics Harness. Returns a structured assessment with confidence score and recommended action.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "incident_description": { "type": "string", "description": "Full incident details: what triggered it, current state, and any known context." },
        "alarm_name": { "type": "string", "description": "The CloudWatch alarm or event name that triggered this incident." }
      },
      "required": ["incident_description"]
    }
  },
  {
    "name": "execute_remediation",
    "description": "Delegate healing to the Remediation Harness. Restarts the ECS service and verifies recovery. Only call after Diagnostics returns HIGH confidence REMEDIATE_AUTOMATICALLY.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "service_name": { "type": "string", "description": "ECS service name to remediate. Defaults to operator-zero-service." },
        "reason": { "type": "string", "description": "The root cause and justification from the Diagnostics assessment." }
      },
      "required": []
    }
  },
  {
    "name": "record_incident_outcome",
    "description": "Write the final incident report to DynamoDB. Always call this as the last step, regardless of outcome.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "incident_type": { "type": "string", "description": "The alarm name or incident type." },
        "classification": { "type": "string", "description": "Reliability, Cost, Security, or Unknown." },
        "diagnostics_summary": { "type": "string", "description": "Summary of what the Diagnostics Harness found." },
        "action_taken": { "type": "string", "description": "REMEDIATED, ESCALATED, or MONITORED." },
        "outcome": { "type": "string", "description": "RESOLVED, ESCALATED, or MONITORING." },
        "severity": { "type": "string", "description": "CRITICAL, HIGH, MEDIUM, LOW, or INFO." },
        "session_id": { "type": "string", "description": "Workshop lab session id." }
      },
      "required": ["incident_type", "action_taken", "outcome"]
    }
  }
]
```

**8.7 — Outbound Auth for the target** — select **IAM Role** (lets the Gateway invoke the Lambda with IAM auth)

**8.8 — Create** — scroll down, **Next → Create Gateway**. Takes 30–60 seconds.

> ⚠️ **"Gateway execution role lacks permission to invoke Lambda function ... Update the permission and retry"** — this is a known timing gap between when the Gateway's auto-generated role is created and when its policy attaches. Fix: scroll to **Targets** → **Add** → re-enter the same target config from 8.5–8.7 (protocol: MCP, name: `operator-zero-action-handler-target`, type: Lambda ARN, same ARN, same tool schema, Outbound Auth: IAM Role) → **Add target**. It succeeds the second time.

**8.9 — Copy the Gateway name**
On the Gateway detail page, confirm **Status: Ready** ✅. You only need the **gateway name** (`operator-zero-gateway`) for the next steps — you select it from a dropdown, no ARN copy-paste needed.

## STEP 9 — Create the Diagnostics Harness

**8 minutes**

**What this is:** a specialist. Its only job: analyze an incident, check DynamoDB for history, return a structured root-cause assessment with a confidence score. It never restarts anything and never talks to Slack.

**9.1** — AgentCore sidebar → **Build** → **Harness** → **Quick create Harness** dropdown → **Advanced create Harness**

**9.2** — Name: `operator_zero_diagnostics` (underscore, not hyphen — cannot be changed later)

**9.3 — Model** — confirm **Claude Sonnet 4.6 v1**

**9.4 — System prompt** — paste:

```
IDENTITY:
You are the OPERATOR ZERO Diagnostics Agent — a specialist in AWS incident
analysis. Your only job is to assess incidents and return a structured
diagnosis. You do not fix anything. You do not post anywhere. You analyze
and report back to the Supervisor Agent.

YOUR PROCESS — follow all steps in order, every time:

Step 1 — QUERY HISTORY
Call query_incident_history with the alarm name as incident_type.
Note: how many past incidents occurred, what patterns exist, what was done.
If first occurrence: state that explicitly.

Step 2 — ASSESS ROOT CAUSE
Based on the incident type and history, identify the most likely root causes.

For ECS CPU high, consider:
  - Container resource exhaustion (memory leak, thread accumulation)
  - Unexpected traffic spike (legitimate or attack)
  - Misconfigured task definition (too little CPU/memory allocated)
  - Runaway process inside the container
  - Dependency slowdown causing request queuing

For other incident types, reason from first principles.

Step 3 — RETURN STRUCTURED ASSESSMENT
Always return your response in EXACTLY this format — no deviation:

INCIDENT TYPE: [classification: Reliability / Cost / Security / Unknown]
SEVERITY: [CRITICAL / HIGH / MEDIUM / LOW]
ALARM NAME: [the alarm name you assessed]
ROOT CAUSE PRIMARY: [most likely cause — one sentence]
ROOT CAUSE SECONDARY: [second most likely cause — one sentence]
PAST OCCURRENCES: [number from history query, or "First occurrence"]
PATTERN DETECTED: [YES — describe pattern / NO]
CONFIDENCE: [HIGH / MEDIUM / LOW]
RECOMMENDED ACTION: [REMEDIATE_AUTOMATICALLY / ESCALATE_TO_HUMAN / MONITOR_ONLY]
REASONING: [2–3 sentences explaining your confidence and recommendation]

RULES:
  - Only return REMEDIATE_AUTOMATICALLY if confidence is HIGH
  - If history shows 3+ occurrences of the same type, increase confidence
  - If you cannot determine root cause, confidence must be LOW and action must be ESCALATE_TO_HUMAN
  - Never recommend REMEDIATE_AUTOMATICALLY for security incidents — always ESCALATE_TO_HUMAN
  - Keep your response concise — the Supervisor Agent reads it programmatically
```

**9.5 — Add the Gateway as a tool**
1. Scroll to **Tools** → expand → toggle to **Gateway**
2. **Select gateway** → `operator-zero-gateway`
3. **Outbound Auth** → **IAM role**
4. A chip appears under **Selected tools**
5. Scroll down → **Create Harness**. Provisioning takes a few minutes.

**9.6 — Copy the Harness ARN**
Once created, copy the **Harness resource ARN** from the detail page — you need it in Step 11. It looks like:
```
arn:aws:bedrock-agentcore:us-east-1:ACCOUNT_ID:harness/operator_zero_diagnostics-XXXXXXXX
```

## STEP 10 — Create the Remediation Harness

**8 minutes**

**What this is:** the healer. Its only job: restart the ECS service when Diagnostics returns HIGH confidence, verify recovery, report exactly what happened. It only ever acts on `operator-zero-cluster`.

**10.1** — **Harness** → **Quick create Harness** → **Advanced create Harness**

**10.2** — Name: `operator_zero_remediation`

**10.3 — Model** — confirm **Claude Sonnet 4.6 v1**

**10.4 — System prompt** — paste:

```
IDENTITY:
You are the OPERATOR ZERO Remediation Agent — a specialist in safe, bounded
AWS infrastructure healing. Your only job is to execute approved fixes and
verify recovery. You act only when the Supervisor Agent delegates to you
after the Diagnostics Agent returns HIGH confidence.

YOUR AUTHORIZED ACTIONS (nothing else):
  - Restart ECS services on operator-zero-cluster ONLY
  - Verify ECS service health after restart
  - Report exactly what was done and the outcome

YOUR UNAUTHORIZED ACTIONS (never do these, ever):
  - Modify IAM roles or policies
  - Delete any resource
  - Change any configuration
  - Act on any resource outside operator-zero-cluster
  - Claim success without running the verification step

YOUR PROCESS — follow all steps in order, every time:

Step 1 — EXECUTE RESTART
Call restart_ecs_service with:
  cluster_name: operator-zero-cluster
  service_name: [the service name provided to you]
  reason: [the reason provided to you from the diagnostics assessment]

Step 2 — VERIFY RECOVERY
Wait for the restart to propagate (the tool handles timing).
Call verify_ecs_service_health with:
  cluster_name: operator-zero-cluster
  service_name: [the same service name]

Step 3 — REPORT OUTCOME
Always return your response in EXACTLY this format — no deviation:

ACTION TAKEN: ECS service restart via force new deployment
SERVICE: [service name]
CLUSTER: operator-zero-cluster
RESTART STATUS: [SUCCESS / FAILED]
RECOVERY STATUS: [HEALTHY / STILL_RECOVERING / FAILED]
RUNNING TASKS: [number] of [desired number]
OUTCOME: [RESOLVED / ESCALATE]
DETAILS: [What happened step by step — what you called, what was returned]
NEXT CHECK: [If STILL_RECOVERING — recommend checking again in 60 seconds]

RULES:
  - Never claim OUTCOME: RESOLVED if running tasks < desired tasks
  - If restart_ecs_service fails, return OUTCOME: ESCALATE immediately
  - If verify_ecs_service_health shows FAILED, return OUTCOME: ESCALATE
  - Always run verify_ecs_service_health — never skip the verification step
  - Your tone is precise and factual — state exactly what happened
```

**10.5 — Add the Gateway as a tool** — same as Step 9.5: Gateway → `operator-zero-gateway` → Outbound Auth: IAM role → **Create Harness**.

**10.6 — Copy the Harness ARN** — save it alongside the Diagnostics ARN from Step 9.6.

## STEP 11 — Wire the Sub-Harness ARNs into the Action Handler

**3 minutes**

**What this is:** `operator-zero-action-handler` already contains the code to call the Diagnostics and Remediation Harnesses (`diagnose_incident`, `execute_remediation`) — it just needs to know their ARNs. This is an environment-variable edit, not a code edit.

1. Lambda console → search `operator-zero-action-handler` → open it
2. **Configuration** tab → **Environment variables** → **Edit**
3. Set:
   - `DIAGNOSTICS_HARNESS_ARN` → your Diagnostics Harness ARN from Step 9.6
   - `REMEDIATION_HARNESS_ARN` → your Remediation Harness ARN from Step 10.6
4. **Save**

That's it — no **Code** tab, no pasting `elif` blocks. `MEMORY_TABLE` and `SLACK_SECRET_NAME` were already set by CDK in Step 7.

## STEP 12 — Create the Supervisor Harness

**8 minutes**

**What this is:** the orchestrator. It receives every incident from the dispatcher Lambda, delegates to Diagnostics and Remediation, and writes the final report to DynamoDB. It's the only Harness the dispatcher calls directly.

**12.1** — **Harness** → **Quick create Harness** → **Advanced create Harness**

**12.2** — Name: `operator_zero_supervisor`

**12.3 — Model** — confirm **Claude Sonnet 4.6 v1**

**12.4 — System prompt** — paste:

```
IDENTITY:
You are OPERATOR ZERO, an autonomous AWS infrastructure monitoring agent
embedded in the Tutorials Dojo cloud learning platform.
Your purpose is to detect, classify, diagnose, and — when authorized — heal
AWS infrastructure incidents with the precision of a senior Site Reliability Engineer.

You are the Supervisor. You do not query history, restart services, or
analyze root cause yourself — you orchestrate two specialist Harnesses that
do that work, then record the final outcome.

YOUR 4-STEP REASONING PROCESS:
You MUST follow all four steps, in order, for every incident. Do not skip steps.

Step 1 — CLASSIFY: Determine incident type
  Reliability: ECS, EC2, RDS, Lambda health/performance
  Cost: budget anomalies, unexpected spend, over-provisioning
  Security: GuardDuty findings, Config violations, IAM changes, S3 exposure
  Unknown: cannot clearly classify — treat as medium severity

Step 2 — RECALL: Call diagnose_incident
  Delegate to the Diagnostics Harness with the incident description and alarm name.
  It returns a structured assessment: root cause, past occurrences, confidence,
  and a recommended action (REMEDIATE_AUTOMATICALLY / ESCALATE_TO_HUMAN / MONITOR_ONLY).

Step 3 — REASON: Evaluate the Diagnostics assessment
  What is the recommended action and confidence level it returned?
  What is the blast radius if you act on it? What if you do not act?
  Do not second-guess a HIGH confidence REMEDIATE_AUTOMATICALLY unless the
  incident is Security — Security incidents are never auto-remediated.

Step 4 — DECIDE: Act or escalate
  If ALL of the following are true, you MUST call execute_remediation:
    - Diagnostics returned RECOMMENDED ACTION: REMEDIATE_AUTOMATICALLY
    - Incident type is Reliability
    - Resource is operator-zero-service on operator-zero-cluster
    - Confidence is HIGH
  Otherwise: escalate to human and explain why remediation was skipped.

  Always finish by calling record_incident_outcome, regardless of outcome,
  with: incident_type, classification, diagnostics_summary, action_taken
  (REMEDIATED / ESCALATED / MONITORED), outcome (RESOLVED / ESCALATED /
  MONITORING), and severity.

  Then return a complete incident summary in your response including:
    - Incident summary (type, affected resource, severity)
    - What the Diagnostics Harness found (root cause, past occurrences, confidence)
    - What the Remediation Harness did, if delegated, and its verified outcome
    - Your decision: REMEDIATED or ESCALATED and why
    - Specific recommended next actions for a human engineer

YOUR AUTHORIZED SCOPE:
  - Call diagnose_incident to delegate root-cause analysis
  - Call execute_remediation for ECS CPU incidents on operator-zero-cluster,
    only after a HIGH confidence REMEDIATE_AUTOMATICALLY from Diagnostics
  - Call record_incident_outcome to write the incident report to DynamoDB
  - Classify and prioritize incidents by severity

OUTSIDE YOUR SCOPE (always requires human approval):
  - Remediating any resource outside operator-zero-cluster
  - Changing IAM policies, security groups, or network configurations
  - Any action that increases or decreases AWS spend
  - Accessing, reading, or modifying application data
  - Security incidents — always escalate, never auto-remediate

RULES YOU MUST NEVER BREAK:
  1. Never claim you took an action you did not take
  2. Always show your full reasoning chain — never just your conclusion
  3. If Diagnostics returns LOW or MEDIUM confidence, escalate — do not remediate
  4. If an incident exceeds your scope, escalate immediately and say why
  5. Every response must end with at least one specific next action for a human
  6. Your tone is calm, precise, and professional — no panic, no drama
  7. When in doubt, escalate. A false escalation is safer than a missed incident.

SEVERITY LEVELS:
  CRITICAL: service is down or data is at risk
  HIGH: service is degraded, user impact likely
  MEDIUM: warning state, no immediate user impact
  LOW: informational, monitor and review
  INFO: test or simulation event
```

**12.5 — Add the Gateway as a tool** — same as before: Gateway → `operator-zero-gateway` → Outbound Auth: IAM role → **Create Harness**.

**12.6 — Copy the Harness ARN** — you wire this into the dispatcher next.

## STEP 13 — Wire the Dispatcher to the Supervisor Harness

**2 minutes**

The dispatcher Lambda receives every alarm event from EventBridge and invokes whichever Harness `HARNESS_ARN` points to. This is the only wiring step left — same as Step 11, it's an environment-variable edit in the Lambda console, not a code change.

1. Lambda console → search `operator-zero-dispatcher` → open it
2. **Configuration** tab → **Environment variables** → **Edit**
3. Set `HARNESS_ARN` → your Supervisor Harness ARN from Step 12.6
4. **Save**

**Verify:** back on the **Configuration → Environment variables** page, confirm `HARNESS_ARN` shows your full `arn:aws:bedrock-agentcore:us-east-1:ACCOUNT_ID:harness/operator_zero_supervisor-XXXXXXXX` — not blank or a placeholder.

## STEP 14 — Test the Full Pipeline Manually (Output A)

**5 minutes**

Before the live demo, test the full chain end-to-end: dispatcher → Supervisor → Diagnostics → Remediation → DynamoDB.

**14.1** — AgentCore sidebar → **Test** → **Harness playground** → select **operator_zero_supervisor** from the Harness dropdown → **Test Harness**

**14.2 — Paste the test incident**

```
MANUAL PIPELINE TEST:
Incident: operator-zero-ECSHighCPU
State: ALARM
Reason: ECS CPU at 89% for 3 minutes on operator-zero-service
Cluster: operator-zero-cluster

Please classify this incident, delegate to diagnostics, and if confidence
is high enough, delegate to remediation. Record the full outcome.
```

Click **Invoke**.

**14.3 — Watch the Agent response panel stream:**
- Supervisor **CLASSIFYING** the incident
- `diagnose_incident` called → Diagnostics Harness runs → calls `query_incident_history`
- Diagnostics returns its structured assessment with a confidence score
- Supervisor evaluates: REMEDIATE_AUTOMATICALLY or ESCALATE?
- If remediating: `execute_remediation` called → Remediation Harness runs `restart_ecs_service` then `verify_ecs_service_health`
- `record_incident_outcome` called → written to DynamoDB
- Final summary in the response

> ⚠️ **This can take 60–120 seconds** — the Supervisor is coordinating three Harnesses in sequence. Don't click anything while it runs.

> ⚠️ **"AccessDenied" on ECS or bedrock-agentcore calls in CloudWatch Logs:** these permissions are already granted by the CDK stack from Step 7 (`ecs:UpdateService`/`DescribeServices` scoped to `operator-zero-service`, and `bedrock-agentcore:InvokeHarness` scoped to your harnesses). If you still see AccessDenied, confirm you deployed the CDK stack from this repo (not an older copy) and that the IAM role `operator-zero-lambda-role` shows both statements under its inline policy.

> ⚠️ **"DIAGNOSTICS_HARNESS_ARN not configured" or "REMEDIATION_HARNESS_ARN not configured":** go back to Step 11 and confirm both environment variables are saved on `operator-zero-action-handler`, with the full `arn:aws:bedrock-agentcore:...` string.

## STEP 15 — Check Output B: CloudWatch Logs

**3 minutes**

1. CloudWatch console → **Logs** → **Log groups**
2. Open `/aws/lambda/operator-zero-action-handler` → most recent log stream. You'll see each tool call received, the DynamoDB queries, the ECS restart command, the health check result.
3. Open `/aws/lambda/operator-zero-dispatcher` → most recent log stream. You'll see: `Received event`, `Dispatching incident`, `Agent response length`, `Trace event count`, `Incident saved to memory`.

> ⚠️ **Log stream empty:** CloudWatch Logs has a slight delay — wait 30 seconds and refresh. If still empty after confirming Step 14 succeeded in the playground, check that `operator-zero-gateway` is attached as a tool on the Supervisor Harness.

## STEP 16 — Check Output C: DynamoDB Incident Report

**2 minutes**

1. DynamoDB console → **Tables** → `operator-zero-incidents` → **Explore table items** → **Run** (Scan doesn't auto-run)
2. Click the most recent record and examine:
   - `incident_type`, `classification`, `diagnostics_summary`
   - `action_taken` — REMEDIATED / ESCALATED / MONITORED
   - `outcome` — RESOLVED / ESCALATED / MONITORING
   - `severity`, `timestamp`, `ttl` (90 days out)

## STEP 17 — The Autonomous Event: Watch It Operate Itself

**10 minutes**

This is the payoff. You set the alarm. Then you do nothing.

**17.1 — Open three browser tabs**
- **Tab 1:** CloudWatch → **Alarms** → find `operator-zero-ECSHighCPU`
- **Tab 2:** CloudWatch → **Logs** → **Log groups** → `/aws/lambda/operator-zero-dispatcher` → most recent stream
- **Tab 3:** DynamoDB → **Tables** → `operator-zero-incidents` → **Explore table items** → **Run**

**17.2 — Enable EventBridge**
```bash
aws events enable-rule --name operator-zero-alarm-router --region us-east-1
aws events describe-rule --name operator-zero-alarm-router --region us-east-1 \
  --query '{Name:Name,State:State}' --output table
```
Confirm `State: ENABLED` before proceeding.

**17.3 — Trigger the incident**
```bash
aws cloudwatch set-alarm-state \
  --alarm-name operator-zero-ECSHighCPU \
  --state-value ALARM \
  --state-reason "CPU utilization sustained at 94% — ECS service operator-zero-service on cluster operator-zero-cluster. Memory pressure detected. Container appears to be leaking." \
  --region us-east-1
```

**Put your hands in your lap. Do not type anything.**

**17.4 — Watch it work**
- **Tab 1:** alarm flips to **In alarm** (red)
- **Tab 2:** `Received event` → `Dispatching incident: type=operator-zero-ECSHighCPU` → `Agent response length=...` → `Trace event count` → `Incident saved to memory`
- **Tab 3:** refresh every 30s — a new record appears with today's timestamp; open it and check `action_taken: REMEDIATED`, `outcome: RESOLVED`, `agent_response` (the full reasoning chain), `source: supervisor-agent`

**Total time from trigger to resolution: roughly 60–120 seconds. No human did any of this.**

**17.5 — Disable EventBridge** (always do this after any demo)
```bash
aws events disable-rule --name operator-zero-alarm-router --region us-east-1
aws events describe-rule --name operator-zero-alarm-router --region us-east-1 \
  --query '{Name:Name,State:State}' --output table
```
Confirm `State: DISABLED`. If left enabled, every future alarm state change fires the full pipeline.

## STEP 18 — Prove Agent Memory

**3 minutes**

Trigger it a second time. The Diagnostics Harness will find the first incident in DynamoDB and its reasoning will reference the history.

```bash
aws events enable-rule --name operator-zero-alarm-router --region us-east-1

aws cloudwatch set-alarm-state \
  --alarm-name operator-zero-ECSHighCPU \
  --state-value ALARM \
  --state-reason "CPU at 91% — recurring issue" \
  --region us-east-1
```

Wait 60–90 seconds, then:

```bash
aws dynamodb scan \
  --table-name operator-zero-incidents \
  --filter-expression "#src = :s" \
  --expression-attribute-names '{"#src":"source"}' \
  --expression-attribute-values '{":s":{"S":"supervisor-agent"}}' \
  --region us-east-1 \
  --query 'Items[*].{type:incident_type.S,time:timestamp.S,action:action_taken.S,outcome:outcome.S}' \
  --output table
```

You should see two records now. Disable EventBridge again:
```bash
aws events disable-rule --name operator-zero-alarm-router --region us-east-1
```

**Same alarm. Different reasoning. Because it remembers.**

## STEP 19 — Architecture Verification

**3 minutes**

```bash
echo "1. Lambda Functions" && aws lambda list-functions --region us-east-1 \
  --query 'Functions[?starts_with(FunctionName, `operator-zero`)].FunctionName' --output table

echo "2. DynamoDB — supervisor-agent records" && aws dynamodb scan \
  --table-name operator-zero-incidents \
  --filter-expression "#src = :s" \
  --expression-attribute-names '{"#src":"source"}' \
  --expression-attribute-values '{":s":{"S":"supervisor-agent"}}' \
  --region us-east-1 \
  --query 'Items[*].{Type:incident_type.S,Action:action_taken.S,Outcome:outcome.S}' --output table

echo "3. ECS Service Health" && aws ecs describe-services \
  --cluster operator-zero-cluster --services operator-zero-service --region us-east-1 \
  --query 'services[0].{Service:serviceName,Status:status,Running:runningCount,Desired:desiredCount}' --output table

echo "4. CloudWatch Alarm" && aws cloudwatch describe-alarms \
  --alarm-names operator-zero-ECSHighCPU --region us-east-1 \
  --query 'MetricAlarms[*].{Alarm:AlarmName,State:StateValue}' --output table

echo "5. EventBridge Rule" && aws events describe-rule \
  --name operator-zero-alarm-router --region us-east-1 \
  --query '{Name:Name,State:State}' --output table
```

Also confirm in the console: AgentCore → Harness → all three (`operator_zero_supervisor`, `operator_zero_diagnostics`, `operator_zero_remediation`) show **✅ Ready**.

| Component | AWS Service | Status |
|---|---|---|
| Entry point | Lambda (dispatcher) | ✅ Active |
| Tool handler | Lambda (action-handler) | ✅ Active |
| Incident simulator | Lambda (chaos-trigger) | ✅ Active |
| Supervisor / Diagnostics / Remediation | AgentCore Harness | ✅ Ready |
| Incident memory | DynamoDB | ✅ Records written |
| Monitored workload | ECS Fargate | ✅ Running |
| Alarm | CloudWatch | ✅ Exists |
| Event router | EventBridge | ✅ Disabled |

## STEP 20 — What You Built and Your Take-Home

**2 minutes**

| Component | AWS Service | Purpose |
|---|---|---|
| Supervisor Harness | AgentCore (Claude Sonnet 4.6) | Orchestrates the incident lifecycle |
| Diagnostics Harness | AgentCore (Claude Sonnet 4.6) | Specialist root-cause analysis |
| Remediation Harness | AgentCore (Claude Sonnet 4.6) | Autonomous healing via ECS restart |
| Incident Memory | DynamoDB | 90-day incident history |
| Event Pipeline | EventBridge | Routes alarm changes automatically |
| Infrastructure | Lambda + ECS + CloudWatch | Monitored workload and tooling |
| IaC | CDK v2 TypeScript | Reproducible infrastructure |

**Download your repo:**
```bash
cd ~
zip -r operator-zero-awscd26.zip operator-zero-awscd26/
```
CloudShell → **Actions** → **Download file** → `/home/cloudshell-user/operator-zero-awscd26.zip`

**What to build next:**
- Add **GuardDuty findings** as an event source for security incidents
- Add **AWS Config violations** as an event source for compliance incidents
- Add **Bedrock Guardrails** to bound the Remediation Harness
- Add a **Knowledge Base** with your runbooks
- Add a **Cost Sentinel** sub-agent for cost anomalies
- Replace DynamoDB with native **AgentCore Memory**

---

## CLEANUP

**Cost while idle:** ~$0.40/day (ECS Fargate).

**Destroy the CDK stack:**
```bash
cd ~/operator-zero-awscd26/cdk
cdk destroy OperatorZeroBaseStack --force
```

**Delete the Harnesses manually** (console only):
1. AgentCore → Harness → `operator_zero_supervisor` → Delete
2. AgentCore → Harness → `operator_zero_diagnostics` → Delete
3. AgentCore → Harness → `operator_zero_remediation` → Delete
4. AgentCore → Harness → `my_first_agent` → Delete
5. AgentCore → Gateways → `operator-zero-gateway` → Delete

---

## TROUBLESHOOTING

| Symptom | Likely cause | Fix |
|---|---|---|
| `cdk bootstrap`/`deploy` fails on region | Wrong region set | Confirm `us-east-1` everywhere, including CloudShell's region tab |
| `cdk deploy` fails on CloudFormation error | IAM/EIP quota, or stale stack | Read the actual error — usually a quota; the VPC in this stack uses `natGateways: 0` on purpose to avoid Elastic IP limits |
| Harness playground blank / no response | Harness still provisioning | Wait for **Status: Ready**, refresh the page |
| `diagnose_incident`/`execute_remediation` returns "not configured" | Step 11 skipped | Set `DIAGNOSTICS_HARNESS_ARN` / `REMEDIATION_HARNESS_ARN` on `operator-zero-action-handler` |
| Every gateway-routed tool call returns `Unknown function`, but direct playground tests of the Lambda work fine | AgentCore Gateway namespaces tool names per target as `{targetName}___{toolName}` — the Lambda never strips it | Fixed in this repo's `action-handler/handler.py` (`_normalize_invocation` strips the `___` prefix). Redeploy from this repo if you're on an older copy. |
| ECS restart fails with AccessDenied | Deployed an old copy of the stack without the `RemediationEcsAccess` IAM statement | Redeploy from this repo's `cdk/lib/base-infra-stack.ts` |
| `bedrock-agentcore:InvokeHarness` AccessDenied | Same as above — `InvokeSubHarnesses` IAM statement missing | Redeploy from this repo |
| DynamoDB record not appearing | `record_incident_outcome` not reached, or Gateway target not synced | Check `operator-zero-action-handler` CloudWatch Logs for the actual error; re-invoke from Step 14 |
| Second incident looks identical to first | Memory working correctly | Diagnostics queried DynamoDB — check `PAST OCCURRENCES` in its assessment |
| EventBridge not triggering | Rule is `DISABLED` | `aws events enable-rule --name operator-zero-alarm-router --region us-east-1` |
| CloudWatch Logs empty | Ingestion delay | Wait 30 seconds, refresh |
| Gateway target creation error on first try | Known IAM role propagation timing gap (Step 8.8) | Re-add the target once — it succeeds on retry |
