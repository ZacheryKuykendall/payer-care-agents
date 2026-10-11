# Preview and GA status

Every Microsoft capability this architecture depends on, what state it is actually
in, and what that means for a production decision. Verified **10 October 2026**.

Two rules this document exists to enforce:

1. **Nothing is described as production-ready that Microsoft has not shipped as GA.**
   A reference architecture that quietly treats a preview feature as a control is
   worse than one that omits the feature, because it transfers risk without saying so.
2. **No performance number appears anywhere in this repository that was not measured
   by code in this repository.** Where Microsoft has published no figures, this
   document says so rather than filling the gap.

| Capability | Status | What that means here |
| --- | --- | --- |
| `Microsoft-Decision-1` in Microsoft Foundry | **GA** (8 Oct 2026) | Safe to build on. Calibration is unverified — see below. |
| Microsoft Foundry Hosted Agents | **GA** | The hosting service itself is supported. |
| `agent-framework-foundry-hosting` (Python) | **Preview / prerelease** | Install needs `--pre`. Pin the version; the package is pre-1.0 and the hosting API changed once already. |
| Network egress controls for hosted agents | **Public preview** | No preview SLA. Microsoft states it is not intended for production. Treat as defence in depth, never as the PHI boundary. |
| MCP servers in Azure API Management | **Available**, tools only | No MCP resources or prompts, and not supported in APIM workspaces. The architecture exposes tools only, which is why this constraint does not bind. |
| Tool governance via Microsoft Agent 365 / Tools Gateway | **Preview** | Not in the deployment path. Described in `GOVERNANCE.md` as the direction of travel for tenant-wide tool inventory. |
| Azure Health Data Services FHIR | **GA** | The clinical system of record. |
| Azure AI Search hybrid + semantic ranking | **GA** | Retrieval. |

---

## `Microsoft-Decision-1`

**Status: generally available in Microsoft Foundry since 8 October 2026.** Not a
preview feature. Text-only, 32,768-token context, built on the open-weight Qwen3.5-9B
and post-trained by Microsoft, with a stated intent to rebase on other backbones
including MAI and OpenAI models later.

Three question types, all used in this repository:

| Type | Question | Returns | Used for |
| --- | --- | --- | --- |
| `choice` | "Which option is it?" | one option plus a probability for every option | routing to a specialist |
| `score` | "How much?" | a probability-weighted position on an ordered scale | urgency, and the grounding rubric |
| `noul` | "Is this true?" | a probability in [0, 1] | clinician-required and prompt-injection gates |

### What Microsoft has published about how well it performs

Nothing numeric. The model catalogue's Benchmarks tab contains a methodology
paragraph and no figures. Microsoft states the model was evaluated on public and
community decision benchmarks plus held-out internal test sets, using accuracy,
calibration error, safety recall, false-positive rates and fairness consistency, with
option order varied and paired statistical tests applied, and reports qualitatively
that it "performs on par with leading decision models and ahead of other open
decision models evaluated with the same methodology."

That is the entire public performance record.

**So this repository publishes no benchmark number for it, invents none, and treats
its calibration as unverified.** The one property that makes a probability useful is
calibration, and no third party has published a figure for it. That is not a
criticism of the model — it is the reason `docs/EVALUATION.md` is built around
measuring calibration on your own labelled cases before any threshold depends on it,
and the reason `payer-agents evals` prints a threshold sweep rather than a grade.

### Known limitations Microsoft documents

- **Fairness.** The model can reflect biases from its base model and training data.
  Not to be used as the sole basis for a decision about a person.
- **Wording sensitivity.** Scores change with phrasing and option order. A badly
  framed question still returns a confident-looking number.
- **No rationale.** There is no explanation field. The probability is the whole
  output.
- **Reliability.** Calibration is strongest on familiar task types.

Microsoft's own guidance for consequential decisions about people — explicitly
including healthcare — is to use the model for decision *support* with meaningful
human review, never as the decision-maker. The gate design in
`src/payer_agents/routing/router.py` follows that: the model picks a queue, and a
licensed human makes every clinical determination.

### What this repository does about it

- An abstention option (`cannot_tell`) is in the option set, per Microsoft's guidance
  to offer one rather than force a choice.
- Thresholds are per-intent and set from the cost of being wrong, not from a single
  global number.
- `payer-agents evals` computes expected calibration error, maximum calibration
  error, a Brier score and a threshold sweep on labelled cases, so the threshold is a
  measurement rather than an opinion.
- Degradation is explicit: if the hosted model is unavailable, a lexical fallback
  takes over with its confidence capped below every threshold, so the system keeps
  classifying and stops auto-answering.

---

## Network egress controls for Foundry hosted agents

**Status: public preview. No preview SLA. Microsoft states it is not intended for
production.**

How it works, as deployed by `infra/modules/rai-egress-policy.bicep`:

- Rules live on a Cognitive Services **Responsible AI policy**, the same resource
  that carries content safety. Two separate switches on one resource:
  `properties.mode` is content safety, `properties.egressPolicy.mode` is network.
  Conflating them is the most common way to deploy an egress policy that does
  nothing.
- Rules are ordered, first match wins, and match on **host only** — exact or
  wildcards such as `*.contoso.com`. Service tags and IP ranges are announced but not
  available.
- Maximum **480 rules** per policy across all actions.
- Modes are `Audit` and `Enforced`. `Transform` and `Rewrite` actions still run in
  Audit during preview, so do not depend on them.
- Evaluation **fails closed**: if the policy cannot be evaluated, the request is
  denied.
- Foundry's own platform domains are allow-listed by the service, so a `Deny` default
  does not sever the runtime's own connectivity.
- Decisions are emitted to Application Insights as `Network egress decision`.
- Attach by passing the policy's **full ARM resource ID** to a hosted agent version.
  A bare policy name is the wrong handle.
- ARM API version at time of writing: `2026-05-15-preview`.

**What it does and does not answer.** Egress control answers one question out of
three: *is this the right destination?* It does not answer *is the caller authorised
there* or *is this appropriate data to send*. Allowing a host does not grant access
on it, and the payload is not inspected. In this architecture those other two
questions are answered by API Management (authorisation, rate limiting, audit) and by
the redaction and minimisation rules in `PHI-BOUNDARY.md`.

**Position taken here:** egress control is a useful, cheap, auditable layer and it is
wired into both the local simulator and the Bicep. It is not load-bearing. If it were
withdrawn tomorrow, the PHI boundary would still hold, because the boundary is private
networking plus gateway authorisation plus data minimisation — not an FQDN list.

---

## Agent Framework hosting on Foundry

**Status: the hosting service is GA. The Python integration package is prerelease.**

- Microsoft Foundry Hosted Agents, the managed hosting service, is generally
  available.
- `agent-framework-foundry-hosting` is prerelease and installs with `pip install
  --pre`. It provides `ResponsesHostServer` (OpenAI-compatible `POST /responses`) and
  `InvocationsHostServer` (`POST /invocations`).
- The package layout already changed once: the original single `agent-framework`
  package split into `agent-framework-core`, `-openai`, `-foundry` and
  `-orchestrations`, `AzureAIAgentClient` became `FoundryChatClient`, `ChatAgent`
  became `Agent`, and the `@ai_function` decorator became `@tool`. An earlier hosting
  backend was retired outright.
- Python 3.10 or later.

**Position taken here:** the orchestrator is a plain Python object behind a protocol,
and Agent Framework is an optional extra rather than a dependency. That is a direct
consequence of the churn above. A reference architecture whose core cannot be run
without a pre-1.0 package is a reference architecture that stops running. Porting the
orchestrator to `Agent` + `FoundryChatClient` + `ResponsesHostServer` is a contained
change — it replaces the composition root in `orchestrator.build`, not the agents, the
gates, the tools or the audit trail.

---

## MCP in Azure API Management

**Status: available. Tools only.**

- Two ways to expose an MCP server: turn an existing managed REST API's operations
  into tools, or front an existing MCP-compatible server.
- **Tools only** — no MCP resources and no prompts. This architecture exposes tools
  only, so the limitation does not bind.
- Not supported in API Management workspaces.
- Policies apply to the MCP server as a whole, not per tool. Per-tool authorisation
  in this repository is therefore expressed in the policy body and in the tool
  registry's governance metadata, not as separate policy scopes.
- Response-body logging at the All APIs scope **interferes with MCP transport** and
  must stay off. `infra/apim/policies/global.xml` keeps it off, and
  `tests/test_evals_and_infra.py` asserts it.

## Tool governance via Microsoft Agent 365

**Status: preview.**

A Global Administrator can connect an API Management or AI Gateway instance in the
Microsoft 365 admin center under **Agents → Settings → Gateways**. Discovered MCP
servers then appear under **Agents → Tools**, where they can be allowed or blocked,
with tool-level granularity for servers registered in an AI Gateway tier. Servers
added to the gateway later are discovered automatically.

Not in this deployment. It is described in `GOVERNANCE.md` because it is the answer to
a question this architecture raises and does not solve: *who maintains the inventory
of every agent tool across the tenant, not just the ones in this repository?* Betting
a compliance programme on a preview admin surface would be the wrong call today;
designing so that adopting it later is a configuration change is the right one.

---

## How to re-verify this page

Status moves. Before relying on any row:

```bash
# Model availability and deployment SKUs in your region
az cognitiveservices model list --location eastus2 \
  --query "[?name=='Microsoft-Decision-1'].{name:name, skus:skus[].name}" -o json

# Current API versions for the RAI policy resource
az provider show --namespace Microsoft.CognitiveServices \
  --query "resourceTypes[?resourceType=='accounts/raiPolicies'].apiVersions" -o tsv

# Package status
pip index versions agent-framework-foundry-hosting
```

Preview API versions are withdrawn rather than deprecated, so
`2026-05-15-preview` will stop working at some point without a warning period.
