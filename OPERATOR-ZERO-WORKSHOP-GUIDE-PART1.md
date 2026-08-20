# OPERATOR ZERO — PART 1

## Build an Autonomous Amazon Bedrock Agent that Operates Itself

**Level:** Intermediate to Advanced
**Duration:** 90 minutes total (Part 1: 25 min · [Part 2](OPERATOR-ZERO-WORKSHOP-GUIDE-PART2.md): 65 min)
**Region:** `us-east-1` (N. Virginia) — use this region for every single step, every command, every console page.
**Model:** Claude Sonnet 4.6 via Amazon Bedrock AgentCore
**IaC:** AWS CDK v2 (TypeScript), Lambda runtime Python 3.14
**Take-home:** CDK repo + system prompts + architecture diagram

---

## What You Are Building

By the end of this workshop you will have a running autonomous system that:

1. **Detects** a CloudWatch alarm firing on your ECS infrastructure
2. **Classifies** the incident type automatically
3. **Recalls** past incidents from DynamoDB memory
4. **Reasons** about root cause using Claude Sonnet 4.6
5. **Heals** the infrastructure by restarting the ECS service
6. **Verifies** the fix worked
7. **Records** the full incident report — what happened, what was done, the outcome — to DynamoDB

No human does steps 2 through 7. You trigger step 1. The agents do the rest.

### The Three Outputs You Will See

| Output | Where | What it shows |
|---|---|---|
| **A** | Harness playground (Bedrock console) | Live streaming response as the agent reasons |
| **B** | CloudWatch Logs | Full execution trace of every Lambda invocation and agent action |
| **C** | DynamoDB | Persistent incident record: classification, reasoning, action taken, outcome |

### Time Allocation

| Part | Content | Time |
|---|---|---|
| **Part 1** (this file) | Single AgentCore Harness | 25 min |
| [Part 2](OPERATOR-ZERO-WORKSHOP-GUIDE-PART2.md) | Infrastructure deployment (CDK) | 15 min |
| [Part 2](OPERATOR-ZERO-WORKSHOP-GUIDE-PART2.md) | Multi-agent system build | 30 min |
| [Part 2](OPERATOR-ZERO-WORKSHOP-GUIDE-PART2.md) | Autonomous demo + verification | 15 min |
| [Part 2](OPERATOR-ZERO-WORKSHOP-GUIDE-PART2.md) | Wrap-up: architecture review + take-home | 5 min |
| **Total** | | **90 min** |

### Architecture (what you build across both parts)

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

Part 1 (below) builds nothing from that diagram yet — it's a warm-up on a single standalone Harness so the mechanics (model, system prompt, tools, playground) are familiar before Part 2 builds the real thing.

---

# PART 1 — Build Your First AgentCore Harness

**25 minutes.** Goal: understand what a Harness is before building the multi-agent system in Part 2.

## STEP 1 — Log Into Your AWS Account

**3 minutes**

Your sandbox AWS account is fully isolated. Nothing you create is visible to other attendees.

1. Open the SSO login URL provided to you
2. Enter your username and password
3. Click your account name → **Management console** next to AdministratorAccess

**Verify:** top-right corner shows a 12-digit account ID. Write it down.

## STEP 2 — Set Your Region to N. Virginia

**1 minute**

Top-right of the console, confirm the region reads **N. Virginia (us-east-1)**. Every resource in this workshop lives here — if you switch regions at any point, nothing will connect.

## STEP 3 — Model Access

**1 minute**

Nothing to do. Bedrock models are enabled automatically on first invocation — the old "Model access" request page is retired.

## STEP 4 — Create Your First Harness

**10 minutes**

**What this is:** In Amazon Bedrock AgentCore, agents are called **Harnesses**. A Harness is a fully managed agent runtime — you define the model, system prompt, and tools, and AWS handles the reasoning loop. Model + instructions + tools = autonomous agent.

**4.1 — Navigate**
1. Amazon Bedrock console → left sidebar, under **Build** → **AgentCore ↗**
2. In the AgentCore sidebar, under **Build** → **Harness**
3. Click **Quick create Harness**

**4.2 — Name it**
1. Clear the auto-generated name → type `my_first_agent`
   - Only `a-z A-Z 0-9 _` — no hyphens, must start with a letter
   - **The name cannot be changed after creation**
2. Click **Create**

**4.3 — Select the model**
The playground opens automatically with a **Configs** panel on the right.
1. Under **Model & system prompt**, confirm **Model** shows **Claude Sonnet 4.6 v1**
2. If not, click the pencil icon ✏️ next to the model name and select it

**4.4 — Write the system prompt**
1. Click inside the **System prompt** field, select all, delete
2. Paste:

```
You are a helpful AWS cloud assistant. Your job is to answer questions about
AWS services, cloud architecture, and best practices clearly and accurately.

When someone asks you a question:
- Give a direct, specific answer first
- Then explain the reasoning or context
- Use concrete examples where helpful
- If you are not certain about something, say so clearly
- Keep responses focused and practical
```

**4.5 — Test it**
1. Click the input box at the bottom: *"Write a prompt (Enter = send...)"*
2. Type: `What is Amazon Bedrock, and what makes it different from just calling an AI API directly?`
3. Press **Enter** and watch the response stream in

**Verify:** the agent responds with a relevant, structured answer.

## STEP 5 — The Harness Playground Is the Test Panel (Output A)

There is no separate "Test" button in AgentCore — the playground **is** the test panel, and you already used it in Step 4. Try one more question to see it again:

1. Input box: `What is the difference between a Bedrock Agent and a Bedrock Knowledge Base?`
2. Press **Enter**

The **Configs** panel on the right shows your system prompt and model. The top bar shows token counts and latency after each response.

## STEP 6 — See How Model Choice Changes Behavior

**5 minutes**

**6.1** — In the Configs panel, click the pencil icon ✏️ next to the model → pick a smaller model (**Claude Haiku** or **Nova Lite**). Applies immediately, no save needed.

**6.2** — Ask the exact same question from Step 5 again. Compare: shorter response? Faster latency (check the top bar)?

**6.3** — Switch back: pencil icon ✏️ → **Claude Sonnet 4.6 v1**.

**What you learned:** same prompt, different model, different depth. Claude Sonnet 4.6 is what you use for the rest of this workshop — your autonomous incident agent needs to reason through multiple steps and call tools reliably. A smaller model skips steps.

---

Continue to [Part 2 — Build the Autonomous Multi-Agent System](OPERATOR-ZERO-WORKSHOP-GUIDE-PART2.md).
