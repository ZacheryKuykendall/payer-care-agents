# AI governance and architecture standards

What an enterprise architect would publish so that the tenth agent a company builds
is as safe as the first — and so that the teams building agents two through ten do
not have to re-litigate the same decisions.

The test of a standard is whether it is *enforceable*. Every standard below names the
mechanism that enforces it, and most of them are executed by code in this repository
rather than reviewed by a person reading a document. Standards nobody can check are
aspirations.

---

## 1. The ten standards

### S1 — Every agent has a named owner and a declared purpose

An agent without an accountable owner cannot be deprecated, audited, or improved.
Registration carries: business owner, technical owner, the business process it
serves, the data classification it touches, and its risk tier (below).

**Enforced by:** platform registration gate. In this repository the smaller version
is visible in `ToolSpec` — every tool declares `risk`, `writes`, `touches_phi` and
`purpose_of_use`, so a tool cannot be added without someone answering the governance
questions about it.

### S2 — A cheap decision step gates every expensive or consequential action

Classification, urgency, safety screening and routing happen before a generative
model or a tool is invoked. This is a cost control and a safety control at once.

**Enforced by:** `routing/router.py`. The gates evaluate in a fixed order, and
`tests/test_routing.py` asserts that order.

### S3 — Confidence thresholds are set from the cost of being wrong, and measured

A threshold is a bet that "0.80 confidence" means "right about 80% of the time". That
bet has to be checked against labelled data, per intent, and re-checked when the
model or the taxonomy version changes.

**Enforced by:** `evals/runner.py` produces expected calibration error, Brier score
and a threshold sweep; CI fails if routing accuracy or human-referral recall regress.
See `EVALUATION.md`.

### S4 — A human makes every consequential decision about a person

The assistant may report a determination that a licensed reviewer already made. It
may never make, predict, expedite, or appeal one. This is not a product preference;
it reflects how utilization management actually works — only a physician or
appropriate clinical peer may issue a medical-necessity denial.

**Enforced by:** `HandoffReason` gates, agent prompts and tool descriptions that
state their own limits, and the grounding judge's unsafe-content question, which asks
specifically whether the answer asserts an approval, denial or eligibility outcome
not already present in the evidence.

### S5 — Every answer is grounded in a citable source or an audited tool call

No claim without provenance. Two kinds of provenance, both required to be present:
documents (citations) and systems of record (audited tool calls).

**Enforced by:** `AgentAnswer` carries `citations` and `facts` separately; the judge
grades against both; an ungrounded answer is withheld and the turn is handed off.

### S6 — Agents hold gateway credentials, never system-of-record credentials

An agent that holds a FHIR credential is an agent whose blast radius is the FHIR
service. An agent that holds a gateway credential has the blast radius the gateway
permits, which is reviewable in one place.

**Enforced by:** API Management authenticates to FHIR with its own managed identity
(`infra/apim/policies/mcp-server.xml`); the egress policy explicitly denies the FHIR
backend hostname to agents, so a direct call fails even if someone hard-codes the URL.

### S7 — Every tool call is authorised, rate limited and audited at one choke point

Not per agent. One policy chain, one audit schema, one place to change the rules.

**Enforced by:** the API Management policy chain, mirrored executably in
`gateway/apim_sim.py` so the controls run in the demo and are asserted in tests.
Rejections are logged as well as successes — logging only successes is how tool abuse
becomes invisible.

### S8 — The audit trail is complete, tamper-evident and PHI-free

Complete: written by the orchestrator, not by each agent, so a new agent cannot
forget. Tamper-evident: hash-chained, so altering record *n* invalidates every record
after it. PHI-free: the log holds counts, categories, decisions and correlation ids,
never member text.

**Enforced by:** `phi/audit.py`. `AuditLog.record` scans its own payload and raises
`PhiLeakError` rather than trusting callers. `AuditLog.verify()` re-walks the chain.

### S9 — Outbound network access is an allow-list, deployed from one source

The destinations an agent may reach are a policy artefact, reviewed like any other,
not an emergent property of whatever the code happens to call.

**Enforced by:** `data/policy/egress-policy.json` drives both the local simulator and
the Azure RAI policy; `scripts/export_egress_policy.py --check` runs in CI so they
cannot drift. Rolled out in Audit mode first — see `infra/README.md`.

### S10 — Preview features are labelled, and nothing load-bearing depends on one

A preview feature can be a layer. It cannot be the control a compliance position
rests on.

**Enforced by:** `PREVIEW-STATUS.md`, re-verified with a dated check, and a test that
fails if a known preview feature loses its label.

---

## 2. Risk tiers

The tier determines how much process an agent attracts. Most agents should be tier 1
and should not need an architecture review.

| Tier | Definition | Required before release |
| --- | --- | --- |
| **1 — Informational** | Answers from published content. No member data, no writes. | Grounding evaluation, citations, owner |
| **2 — Member-contextual** | Reads a specific member's data to answer. No writes. | Tier 1, plus PHI review, tool allow-list, audit, per-intent thresholds |
| **3 — Transactional** | Writes to a system of record, or its output drives one. | Tier 2, plus human approval on every write, rollback path, four-eyes review of the tool definition |
| **4 — Consequential** | Output influences a coverage, clinical, or benefit decision about a person. | Tier 3, plus clinical governance sign-off, bias review, and a documented human decision-maker |

**The assistant in this repository is tier 2 and stays there by construction.** It
reads, reports and cites. The one write tool in the registry is blocked for its
identity, and the demo shows the gateway refusing it — because a control nobody has
watched fire is a control nobody should trust.

A tier-3 or tier-4 agent is a different conversation with a different review board,
not a configuration change.

---

## 3. Model selection standard

Pick the cheapest model that produces a checkable output.

| Job | Model class | Why |
| --- | --- | --- |
| Classify, route, prioritise, gate | Decision model (`Microsoft-Decision-1`) | Returns a probability you can threshold. Single pass, zero output tokens. A generative model here costs more and returns prose you then have to parse. |
| Grade a response against a rubric | Decision model | Same reason, and it runs on every turn. An LLM judge is a nightly-sample tool; a decision model is a production one. |
| Compose a member-facing answer | Small generative model, grounded | Fluency matters here and nowhere else in the pipeline. |
| Summarise, rewrite, explain | Generative | The job is prose. |
| Retrieve | Embedding + hybrid search | Not a model-selection question. |

The anti-pattern this standard exists to prevent: routing a request by asking a large
generative model to "respond with one of these labels" and parsing the result. It
costs more, it is slower, the output shape is not guaranteed, and it gives you no
calibrated number to threshold.

---

## 4. Evaluation standard

Summarised here; `EVALUATION.md` has the method.

1. **Labelled cases exist before the agent ships**, and at least a fifth of them are
   ambiguous or adversarial. A suite of well-formed questions measures nothing about
   a gate.
2. **Two metrics gate release**, not one: routing accuracy *and* human-referral
   recall. An agent that is 97% accurate and misses one escalation is worse than one
   that is 85% accurate and catches them all.
3. **Grounding is scored on every turn in production**, not sampled. That is
   affordable because the grader is a decision model.
4. **Calibration is measured, not assumed.** Microsoft publishes no calibration
   figures for `Microsoft-Decision-1`; thresholds come from your own labelled data.
5. **Thresholds are published as a sweep**, so the people who own the risk can pick
   the operating point. "Pick 0.8" is an opinion. "At 0.8 we automate 62% with a 0%
   error rate on the automated slice, and here is the curve" is a decision.

---

## 5. Change control

| Artefact | Versioned how | Why it matters |
| --- | --- | --- |
| Option set and question wording | `TAXONOMY_VERSION`, written to every audit record | The first question an incident review asks is "did the taxonomy change?" |
| Thresholds | Configuration, reviewed by the owning governance body | Changing a threshold changes the automation/risk trade-off |
| Tool definitions | Code review plus the risk-tier gate | A tool description is part of the security surface: an MCP client decides when to call a tool from its description |
| Egress rules | One JSON file, generated into Bicep, checked in CI | Two enforcement points, one source |
| Prompts and rubrics | Code review; rubric changes invalidate historical grades | A grade is only comparable within one rubric version |
| Medical policy corpus | Owned by the Medical Policy Committee, not by engineering | The content has a review board; the index should not route around it |

---

## 6. Where this architecture stops

An honest governance document names what it does not cover.

- **Tenant-wide tool inventory.** This repository governs its own tools well. It says
  nothing about the other agents a company runs. Microsoft Agent 365's Tools Gateway
  is the emerging answer — connect an API Management or AI Gateway instance in the
  Microsoft 365 admin center and discovered MCP servers appear in a central inventory
  that can be allowed or blocked, with tool-level granularity in the AI Gateway tier.
  It is **preview** today (`PREVIEW-STATUS.md`), so it is the direction of travel
  rather than the plan of record. The design position: keep tools behind API
  Management so that adopting it later is a configuration change.
- **Bias and fairness testing.** The gates reduce exposure by keeping consequential
  decisions with humans, but this repository contains no fairness evaluation.
  Microsoft's own model guidance calls out that decision scores can reflect base-model
  bias and should not be the sole basis for a decision about a person. A tier-4 agent
  needs a real fairness programme, and that is a specialist discipline.
- **Red teaming.** One prompt-injection gate with a labelled test set is not an
  adversarial programme.
- **Member consent and notice.** Microsoft's guidance includes telling affected users
  when AI contributes to a decision. Designing that notice is a product and
  compliance question this repository does not answer.
- **Data residency and BAA coverage.** The deployment keeps inference in a data zone
  and all processing inside the boundary, but the agreements that make that
  compliant are legal artefacts, not architectural ones.
