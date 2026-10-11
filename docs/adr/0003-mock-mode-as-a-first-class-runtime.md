# ADR 0003 — Mock mode is a runtime, not a test fixture

**Status:** accepted · **Date:** 2026-10-10

## Context

A reference architecture that needs an Azure subscription to run is a reference
architecture almost nobody runs. The people most likely to read this one — a hiring
manager, a reviewer on a different team, an engineer evaluating the approach — will
not provision a Foundry resource first.

## Decision

Every externally-backed capability sits behind a protocol with two implementations.
`orchestrator.build` is the only function that branches on mode, and nothing
downstream imports an Azure SDK.

Mock mode must satisfy three properties: no credential, no socket, deterministic.

## Why

- **The demo and the tests run the same code path.** Only the leaf adapters differ,
  so `make demo` exercises the orchestration, the gates, the tool governance chain
  and the audit trail that production uses.
- **It forces honest seams.** Every place mock mode is awkward is a place the
  abstraction was wrong. The judge needing two kinds of evidence surfaced because the
  offline judge scored prior-authorization answers as unsupported.
- **Determinism makes tests assert numbers.** A mock that returns random values can
  only be asserted loosely, and loose assertions do not catch calibration bugs.
- **CI needs no secrets.** The whole suite runs on a fork.

## Costs accepted

Mock mode loses real capability, and pretending otherwise would be the trap this ADR
is supposed to avoid:

| Lost | Consequence |
| --- | --- |
| Semantic recall | Hashed bag-of-words will not match "heart attack" to "myocardial infarction". The synthetic corpus is written around it. |
| Fluency | The extractive composer reads like an organised policy extract, not a representative. It also cannot fabricate, which is why grounding is provable locally. |
| Classifier quality | The offline router is tuned against the cases it is scored on. Its metrics describe the harness. |
| Judge coverage | The lexical judge grades cited claims. It cannot catch a fluent uncited fabrication — a capability of the real model. |

Every one of these is stated in `ARCHITECTURE.md` and in the eval report's own notes,
so a reader cannot mistake a harness number for a model number.

There is also real maintenance cost: two implementations of every seam, and the
second one can rot silently because mock mode exercises the first. Mitigated by
parity tests — `test_http_adapters.py` asserts the two FHIR clients agree, and it
caught a real ordering divergence.

## Rejected alternatives

| Alternative | Why not |
| --- | --- |
| Record/replay cassettes against real services | Requires a subscription to record, and the recordings would contain real responses |
| Azure-only, with tests skipped without credentials | The tests nobody runs are the tests that do not exist |
| A single `if mock:` branch inside each client | Scatters the mode decision across the codebase and guarantees drift |
