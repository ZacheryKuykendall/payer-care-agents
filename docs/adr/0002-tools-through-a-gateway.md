# ADR 0002 — Agents reach clinical data only through an API Management MCP server

**Status:** accepted · **Date:** 2026-10-10

## Context

The assistant needs a member's coverage and the status of their prior-authorization
requests. Both live in a FHIR service. The direct route — give the agent runtime a
FHIR credential and let it call the service — is one hop shorter and a three-line
change.

## Decision

Agents call MCP tools. The MCP server is hosted by Azure API Management, which
authenticates to FHIR with its own managed identity. The agent holds a gateway
credential and never a clinical-system credential.

The egress policy additionally **denies the FHIR backend hostname to the agent**, so
a hard-coded URL fails even though the service is inside the network boundary.

## Why

- **One choke point for four controls.** Authentication, tool-level authorisation,
  per-agent rate limiting and audit are defined once, in policy, rather than
  re-implemented per agent.
- **Blast radius becomes reviewable.** "What can this agent reach" is answered by one
  policy document instead of by reading every agent's code.
- **The audit record is uniform.** One schema, emitted on success and on rejection,
  into a sink with its own retention and access control.
- **Being inside the network is not an authorisation decision.** Denying the backend
  to the agent makes that explicit rather than cultural.
- **Tool-level control has somewhere to live.** The write tool is blocked for the
  member-facing identity at the gateway, and the demo shows it being refused.

## Costs accepted

- **A hop, and a cost centre.** API Management is not free and adds latency. For a
  workload whose unit of value is a member interaction, that is the right trade.
- **MCP in API Management is tools-only** — no resources, no prompts, and no
  workspace support. This architecture exposes tools only, so the limitation does not
  bind today. It would bind on a design that wanted MCP resources.
- **Policies apply to the whole MCP server, not per tool.** Per-tool authorisation is
  expressed in the policy body and in the registry's governance metadata rather than
  as separate scopes.
- **Response-body logging must stay off at the All APIs scope**, because it interferes
  with MCP transport. Enforced in `global.xml` and asserted in tests.

## Rejected alternatives

| Alternative | Why not |
| --- | --- |
| Agent calls FHIR directly with a managed identity | Blast radius is the FHIR service; audit and rate limiting become per-agent problems |
| A bespoke tool service we write ourselves | Re-implements a gateway, badly, and it is the component most likely to be under-tested |
| MCP server hosted in the agent process | Nothing in front of it. The governance story is the gateway. |
