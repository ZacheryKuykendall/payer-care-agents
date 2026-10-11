# ADR 0001 — A decision model, not an LLM, in front of every turn

**Status:** accepted · **Date:** 2026-10-10

## Context

Every member turn needs a classification before anything expensive happens: which
specialist handles it, how urgent it is, whether it needs a clinician, whether it is
adversarial. The conventional approach is to ask a generative model to reply with one
of a set of labels and parse the result.

`Microsoft-Decision-1` went GA in Microsoft Foundry on 8 October 2026 for exactly this
shape of problem: a `state`, a question with a fixed set of options, and a calibrated
probability for each option. Single pass, 32,768-token context, zero output tokens,
no prose.

## Decision

Use a decision model for routing, urgency scoring, both safety gates and answer
grading. Use a generative model only to compose answers, and only on passages that
retrieval already produced.

Put it behind a `DecisionClient` protocol with three implementations: the Foundry
client, a deterministic offline scorer, and a heuristic fallback.

## Why

- **The output is a number you can threshold.** Parsing a label out of prose gives
  you a string with no confidence attached. Thresholding, sweeping and measuring
  calibration all require a probability.
- **It is cheap enough to run on every turn, twice.** Routing and grading both run on
  100% of traffic. That is only affordable with a model that emits no output tokens.
- **The response shape is guaranteed.** No retry loop for malformed JSON.
- **An abstention option is a first-class concept.** Microsoft's guidance is to offer
  one rather than force a choice, which is precisely the behaviour a member-services
  assistant needs on an ambiguous message.

## Costs accepted

- **No rationale.** The model returns a number and no explanation. When the score is
  surprising, you go and look. Mitigated by keeping the evidence set attached to the
  turn.
- **Unverified calibration.** Microsoft publishes no calibration figures, and the
  property the thresholds depend on is therefore vendor-asserted. Mitigated by
  measuring it ourselves (`EVALUATION.md`) and by never letting the model make a
  consequential decision alone.
- **Wording sensitivity.** Scores move with phrasing and option order. Mitigated by
  versioning the taxonomy and writing the version into every audit record.
- **A new single point of failure.** Mitigated by a circuit breaker and a fallback
  whose confidence is capped below every threshold.

## Rejected alternatives

| Alternative | Why not |
| --- | --- |
| Generative model with a label instruction | More expensive, slower, no calibrated probability, output shape not guaranteed |
| Fine-tuned classifier of our own | Better calibration, at the cost of a training pipeline, a labelled corpus, and an MLOps surface — for a routing problem with five options |
| Rules and keywords only | What the fallback already is. Brittle, but valuable precisely because it is independent of the hosted model |
