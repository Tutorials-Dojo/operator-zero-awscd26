# Operator Zero
## Build an Autonomous Amazon Bedrock Agent that Operates Itself

> **AWS Community Day Philippines 2026**  
> Hands-on workshop by [Tutorials Dojo](https://tutorialsdojo.com)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![AWS Region](https://img.shields.io/badge/AWS-us--east--1-orange)](https://us-east-1.console.aws.amazon.com/)
[![CDK](https://img.shields.io/badge/IaC-AWS%20CDK%20v2-blue)](https://docs.aws.amazon.com/cdk/)

---

## What This Is

This is the take-home repository from the Operator Zero workshop. You built
an autonomous multi-agent system on AWS that:

1. **Detects** a CloudWatch alarm firing on your ECS infrastructure
2. **Classifies** the incident using a Diagnostics Agent (Claude 3.5 Sonnet)
3. **Heals** the infrastructure by restarting the ECS service via a Remediation Agent
4. **Verifies** the fix worked by checking service health
5. **Records** the full incident report to DynamoDB — autonomously, with no human input

The agents do steps 2 through 5 themselves after you trigger step 1.

---

## Architecture

```
CloudWatch Alarm (ECS CPU > 60%)
        ↓ state change → ALARM
EventBridge Rule (operator-zero-alarm-router)
        ↓ invokes automatically
Lambda: operator-zero-dispatcher
        ↓ starts agent session
SUPERVISOR HARNESS (operator_zero_supervisor)
  Claude 3.5 Sonnet
  CLASSIFY → RECALL → REASON → DECIDE
        ↓ delegates to              ↓ delegates to
DIAGNOSTICS HARNESS            REMEDIATION HARNESS
  Queries DynamoDB history       Restarts ECS service
  Returns root cause +           Verifies recovery
  confidence score               Returns outcome
        ↓                              ↓
  Back to Supervisor ←────────────────┘
        ↓
  Writes full incident report to DynamoDB
  (what happened, what was fixed, outcome)
        ↓
ECS service recovers → CloudWatch alarm returns to OK
```

---

## What You Built

| Component | AWS Service | Purpose |
|---|---|---|
| Supervisor Harness | Amazon Bedrock AgentCore | Orchestrates the full incident lifecycle |
| Diagnostics Harness | Amazon Bedrock AgentCore | Root cause analysis and confidence scoring |
| Remediation Harness | Amazon Bedrock AgentCore | Autonomous ECS healing and verification |
| Incident Memory | Amazon DynamoDB | 90-day persistent incident history |
| Event Pipeline | Amazon EventBridge | Routes alarm events automatically |
| Entry Point | AWS Lambda (dispatcher) | Receives events, starts agent sessions |
| Tools | AWS Lambda (action-handler) | Handles all agent tool calls |
| Monitored Workload | Amazon ECS Fargate | nginx service being monitored |
| Infrastructure | AWS CDK v2 TypeScript | All of the above as code |

---

## Repository Structure

```
operator-zero-workshop/
├── cdk/                          Infrastructure as code (CDK v2 TypeScript)
│   ├── bin/operator-zero.ts      CDK app entry point
│   ├── lib/base-infra-stack.ts   All AWS resources defined here
│   ├── package.json
│   └── tsconfig.json
├── lambda/
│   ├── dispatcher/handler.py     EventBridge → Bedrock AgentCore entry point
│   ├── action-handler/handler.py All agent tool implementations
│   └── chaos-trigger/handler.py  Safe incident simulator
├── prompts/
│   ├── supervisor-system-prompt.txt    Paste into Supervisor Harness
│   ├── diagnostics-system-prompt.txt  Paste into Diagnostics Harness
│   └── remediation-system-prompt.txt  Paste into Remediation Harness
├── architecture/
│   └── architecture.txt          Full architecture diagram
├── scripts/
│   ├── deploy.sh                 One-command infrastructure deploy
│   └── update_dispatcher.sh     Wire your Harness ID into the dispatcher Lambda
└── README.md
```

---

## Prerequisites

Before deploying, confirm you have:

- [ ] AWS account with AdministratorAccess
- [ ] AWS CLI v2 installed and configured
- [ ] Node.js 20+ installed
- [ ] AWS CDK v2 installed globally: `npm install -g aws-cdk`
- [ ] AWS CloudShell access (or local terminal with AWS credentials)
- [ ] Amazon Bedrock AgentCore access in us-east-1

---

## Quick Start — Redeploy in Any Account

```bash
# 1. Clone this repo
git clone https://github.com/Tutorials-Dojo/operator-zero-workshop.git
cd operator-zero-workshop

# 2. Deploy base infrastructure (8–12 minutes)
bash scripts/deploy.sh

# 3. Create three AgentCore Harnesses in the AWS Console
#    Following the steps below

# 4. Wire your Supervisor Harness ID into the dispatcher Lambda
bash scripts/update_dispatcher.sh
```

---

## Step-by-Step: Create the AgentCore Harnesses

After `bash scripts/deploy.sh` completes, create three Harnesses in the
AWS Console. All steps are in **us-east-1 (N. Virginia)**.

### Navigate to AgentCore

1. AWS Console → search **Amazon Bedrock** → click it
2. In the left sidebar under **Build**, click **AgentCore ↗**
3. In the AgentCore sidebar under **Build**, click **Harness**

### Harness 1 — Supervisor (the orchestrator)

1. Click **Quick create Harness** → name: `operator_zero_supervisor` → **Create**
2. Click on the new Harness → **Edit**
3. **Model:** Claude 3.5 Sonnet
4. **System prompt:** paste full contents of `prompts/supervisor-system-prompt.txt`
5. **Save** → **Deploy**
6. Copy the **Harness ARN** — you need it in the next step

### Harness 2 — Diagnostics (root cause specialist)

1. **Quick create Harness** → name: `operator_zero_diagnostics` → **Create**
2. **Model:** Claude 3.5 Sonnet
3. **System prompt:** paste full contents of `prompts/diagnostics-system-prompt.txt`
4. Add tool: `query_incident_history` → Lambda: `operator-zero-action-handler`
5. **Save** → **Deploy**
6. Copy the **Harness ARN**

### Harness 3 — Remediation (autonomous healer)

1. **Quick create Harness** → name: `operator_zero_remediation` → **Create**
2. **Model:** Claude 3.5 Sonnet
3. **System prompt:** paste full contents of `prompts/remediation-system-prompt.txt`
4. Add tools:
   - `restart_ecs_service` → Lambda: `operator-zero-action-handler`
   - `verify_ecs_service_health` → Lambda: `operator-zero-action-handler`
5. **Save** → **Deploy**
6. Copy the **Harness ARN**

### Wire the Dispatcher

```bash
# Set your Harness ARNs
export SUPERVISOR_HARNESS_ARN=arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:harness/...
export DIAGNOSTICS_HARNESS_ARN=arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:harness/...
export REMEDIATION_HARNESS_ARN=arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:harness/...

bash scripts/update_dispatcher.sh
```

---

## Trigger the Autonomous Pipeline

```bash
# 1. Enable EventBridge
aws events enable-rule \
  --name operator-zero-alarm-router \
  --region us-east-1

# 2. Trigger a simulated incident
aws cloudwatch set-alarm-state \
  --alarm-name operator-zero-ECSHighCPU \
  --state-value ALARM \
  --state-reason "CPU at 91% — autonomous test" \
  --region us-east-1

# 3. Watch the outcome in DynamoDB (refresh every 30 seconds)
aws dynamodb scan \
  --table-name operator-zero-incidents \
  --region us-east-1 \
  --query 'Items[*].{Type:incident_type.S,Action:action_taken.S,Outcome:outcome.S,Time:timestamp.S}' \
  --output table

# 4. Disable EventBridge when done
aws events disable-rule \
  --name operator-zero-alarm-router \
  --region us-east-1
```

---

## See the Three Outputs

**Output A — Harness Playground** (live streaming)
- AgentCore → Harness → operator_zero_supervisor → **Test in playground**
- Watch the agents reason in real time

**Output B — CloudWatch Logs** (execution trace)
- CloudWatch → Log groups → `/aws/lambda/operator-zero-dispatcher`
- CloudWatch → Log groups → `/aws/lambda/operator-zero-action-handler`

**Output C — DynamoDB** (persistent incident report)
- DynamoDB → Tables → `operator-zero-incidents` → Explore table items
- See: `action_taken: REMEDIATED`, `outcome: RESOLVED`

---

## What to Build Next

| Extension | What it adds |
|---|---|
| GuardDuty event source | Security findings trigger the autonomous pipeline |
| AWS Config event source | Compliance violations trigger diagnosis and alerting |
| Bedrock Guardrails | Bound what the Remediation Agent is allowed to do |
| Knowledge Base | Agents search your runbooks before reasoning |
| Cost Sentinel Harness | Specialist agent for cost anomaly incidents |
| Bedrock Memory | Replace DynamoDB with native AgentCore Memory |

See the [full Operator Zero lab](https://tutorialsdojo.com) on Tutorials Dojo
PlayCloud for the complete guided experience.

---

## Cost to Keep Running

| Service | Idle cost | Per invocation |
|---|---|---|
| ECS Fargate | ~$0.40/day | — |
| Lambda | $0 | Negligible |
| DynamoDB | $0 (on-demand) | Negligible |
| Bedrock (Claude 3.5 Sonnet) | $0 | ~$0.01–$0.05 |
| CloudWatch | ~$0 | — |

**Destroy when done:**
```bash
cd cdk
cdk destroy OperatorZeroBaseStack --force
```

Also delete your three Harnesses in the AgentCore console.

---

## Workshop Details

| | |
|---|---|
| **Event** | AWS Community Day Philippines 2026 |
| **Workshop** | Build an Autonomous Amazon Bedrock Agent that Operates Itself |
| **Level** | Intermediate to Advanced |
| **Duration** | 90 minutes |
| **Region** | us-east-1 (N. Virginia) |
| **Model** | Claude 3.5 Sonnet via Amazon Bedrock AgentCore |
| **IaC** | AWS CDK v2 (TypeScript) |
| **By** | [Tutorials Dojo](https://tutorialsdojo.com) |

---

## License

MIT License — see [LICENSE](LICENSE)

© 2026 Tutorials Dojo
