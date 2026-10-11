# ADR 0004 — One egress policy document, two enforcement points

**Status:** accepted · **Date:** 2026-10-10

## Context

Microsoft Foundry can apply outbound network egress rules to a hosted agent: ordered
FQDN rules with a default action, evaluated first-match-wins inside the managed
sandbox. It is a useful control and it is **public preview** with no SLA.

The obvious implementation is to write the rules into the Bicep template. The
obvious consequence is that nothing local exercises them, and the first time anyone
discovers the policy is wrong is in Azure.

## Decision

`data/policy/egress-policy.json` is the single source of truth. It drives:

1. `GuardedTransport` in the local runtime, which evaluates every outbound request
   before it leaves the process.
2. `EgressPolicy.to_arm_properties()`, projected by
   `scripts/export_egress_policy.py` into `infra/generated/egress-rules.json` and
   deployed by `infra/modules/rai-egress-policy.bicep`.

`--check` runs in CI, so the two cannot drift.

The local model mirrors the Azure resource deliberately: ordered rules, first match
wins, `Fqdn` matching on host with wildcards, a default action, an `Audit`/`Enforced`
switch, and fail-closed evaluation.

## Why

- **The control is executed, not described.** It runs in the demo and is asserted by
  tests. A policy that only exists in a template is a policy nobody has watched fire.
- **Drift is a build failure.** Two copies of a security control in two languages is
  the normal way this goes wrong.
- **The simulator is where the rollout is rehearsed.** Audit mode, review the
  would-deny records, then Enforced — practised locally before it is practised on
  production traffic.

## Costs accepted

- **The simulator is not the enforcer.** It guards the HTTP client this code uses. A
  subprocess, a library with its own transport, or a DNS lookup bypasses it entirely.
  It models the policy; the Azure resource enforces it.
- **Mirroring a preview resource shape means the shape can change.** The ARM API
  version is `2026-05-15-preview`, and preview versions are withdrawn rather than
  deprecated.
- **Two artefacts to keep aligned** — mitigated by generating one from the other.

## The position on preview

Egress control is wired in, exercised, and deployed. **Nothing in the PHI boundary
depends on it.** If the feature were withdrawn tomorrow the boundary would still hold
on private networking, gateway authorisation and data minimisation. It also only
answers one of three questions — *is this the right destination* — and not *is the
caller authorised there* or *is this appropriate data to send*.

That is the general rule this ADR is an instance of: a preview feature can be a
layer, never the control a compliance position rests on.

## Rejected alternatives

| Alternative | Why not |
| --- | --- |
| Rules inline in Bicep | Nothing local exercises them; no test can assert them |
| Local simulator only | Does not protect the deployed agent |
| Two hand-maintained copies | Drifts on the first change, and the drift is silent |
| Skip egress control until GA | Loses a cheap auditable layer and the chance to rehearse the rollout |
