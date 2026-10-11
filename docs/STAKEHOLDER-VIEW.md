# The business view

Written for the people who fund this, own the risk, and answer for it: member
services operations, utilization management, compliance and privacy, and the clinical
leadership who will be asked whether it is safe.

No performance claims appear here. Every number is either measured by code in this
repository on synthetic data, or shown as a formula with the inputs you would collect
from your own operation. A reference architecture that arrives with a benefit case
attached is selling something.

---

## The problem, in the operation's own terms

A health plan's member services queue is dominated by a small number of question
types asked in a large number of ways:

- *What will this cost me?*
- *Is this covered?*
- *What does the plan need before it will approve this?*
- *Where is my request?*

These are answerable from documents the plan already publishes and systems the plan
already runs. They are also the questions most likely to be answered inconsistently,
because the answer depends on which plan the member is on, which document version is
current, and which representative picked up.

Mixed into that queue is a much smaller number of calls where the right action is not
to answer at all: a member describing symptoms, a member in distress, a member who
wants to file a grievance. Those are the calls that matter most and the ones an
automation programme is most likely to get wrong.

**So the design question is not "can a model answer benefits questions".** It is:
*can we automate the answerable traffic without automating the three percent that
must reach a person?* Everything in this architecture follows from treating that as
the primary requirement rather than a safety afterthought.

---

## The four use cases, and what each is worth

| # | Use case | Who benefits | What it replaces | Risk tier |
| --- | --- | --- | --- | --- |
| 1 | **Benefits and cost share** — "what's my specialist copay, do I need a referral" | Members; member services | A representative reading the member's own Summary of Benefits aloud | 2 |
| 2 | **Medical policy criteria** — "what does the plan require before approving a lumbar MRI" | Provider offices; UM intake | A call to provider services, or a submission that gets returned for missing documentation | 2 |
| 3 | **Prior authorization status** — "where is my request" | Members; providers; UM staff | A status call that consumes a UM coordinator's time and produces no clinical value | 2 |
| 4 | **Safe escalation** — symptoms, distress, grievances, anything ambiguous | Members; clinical staff | Nothing. This is new capability, not displaced work. | n/a |

Use case 4 is the one worth dwelling on with a clinical audience. It is not a
fallback for when the other three fail — it is a designed path with its own quality
bar, its own queues, and its own handoff packet. A nurse receiving an escalation sees
the routing distribution, the gate that fired, what was already retrieved, and the
audit correlation id. They start where the agent stopped rather than from zero.

### What use case 2 is actually worth, and why it is underrated

Use case 2 looks like the smallest one. It may be the largest. A prior-authorization
request that arrives without the documentation the policy requires gets returned,
resubmitted, and re-reviewed — consuming UM staff time twice, delaying the member's
care, and generating the status calls that make up use case 3. Answering *"what does
the policy require"* accurately at the point of submission attacks the root cause of
all three downstream costs.

That is the kind of second-order argument worth making to an operations owner, and it
is not visible if you evaluate agents one answer at a time.

---

## How to build the benefit case

The formula, with the inputs you would pull from your own contact-centre and UM
reporting. **No values are supplied, because supplying them would be inventing them.**

```
Annual contacts in scope
  = total member and provider contacts
  × share falling into use cases 1-3          (from contact-reason coding)

Automatable volume
  = annual contacts in scope
  × automation rate at the chosen threshold   (from the threshold sweep -- see below)

Gross benefit
  = automatable volume × fully loaded handle cost per contact

Net benefit
  = gross benefit
  − model and platform run cost
  − human review cost on the deferred slice
  − the cost of errors on the automated slice
```

The last line is the one that gets skipped and the one that decides whether the
programme is real. An automated answer that is wrong does not cost zero — it costs a
callback, a complaint, sometimes an appeal, and occasionally a regulatory finding.

### The threshold sweep is the business artefact

`payer-agents evals` produces a curve, not a grade. Running against the synthetic
case set in this repository:

| Confidence threshold | Automated | Deferred to a human | Automation rate | Error rate on the automated slice |
| --- | --- | --- | --- | --- |
| 0.50 | 29 | 10 | 74% | 0.0% |
| 0.70 | 28 | 11 | 72% | 0.0% |
| 0.80 | 24 | 15 | 62% | 0.0% |
| 0.90 | 13 | 26 | 33% | 0.0% |

*(39 synthetic cases, offline mode. These describe the harness, not a production
system — see `EVALUATION.md`.)*

**This table is the conversation.** The architect does not choose the operating
point; the people who own the risk do, and this is what they need to choose it. Move
right and you automate less but are more certain of what you do automate. Move left
and the opposite. The curve makes the trade-off explicit instead of burying it in a
configuration file.

In a real deployment the right-hand column is non-zero and the shape of the curve is
the whole decision.

---

## Human in the loop, explained without jargon

Four questions are asked about every message, by one cheap model call, before
anything expensive or consequential happens:

1. **Which specialist should handle this?** — with an explicit *"I can't tell"*
   option, so the system can decline to classify rather than guess.
2. **How urgent is the member's situation?** — rated Routine, Elevated, Urgent or
   Emergent.
3. **Does answering this safely need a licensed clinician?**
4. **Is this message trying to manipulate the assistant?**

Any of four conditions sends the turn to a person:

| Condition | Where it goes | Why |
| --- | --- | --- |
| Urgency is Urgent or above | Nurse triage, urgent | Safety instruction first, plan information second |
| A clinician is needed to answer safely | Nurse triage | The assistant does not practise medicine |
| The message looks manipulative | Security review | A manipulated request must not reach a tool |
| Confidence is below the bar for that question type | Member services | Guessing is worse than deferring |

And one more, after an answer exists but before the member sees it:

| Condition | Where it goes | Why |
| --- | --- | --- |
| The answer is not supported by its own sources | Member services | An answer nobody can check is not an answer |

The bar differs by question type because the cost of being wrong differs. A wrong
copay is correctable. A wrong statement about someone's authorization lands on their
care. So prior authorization requires 0.80 confidence to answer automatically and
benefits requires 0.65 — and escalation requires only 0.50, because the cost of an
unnecessary escalation is a few minutes of a nurse's time.

**The commitment to put in front of a clinical governance committee:** the assistant
reports determinations that a licensed reviewer already made. It never makes,
predicts, expedites or appeals one. That is not a prompt instruction — it is enforced
by the tool permissions (the write tool is blocked for this identity), by the agent
design, and by a grading question that specifically asks whether the answer asserted
an outcome that was not already in the evidence.

---

## Scaling beyond the first use case

The point of a reference architecture is the second agent, not the first. These
components are the ones that should not be rebuilt:

| Shared component | Why it is shared |
| --- | --- |
| The decision step and gate framework | Every agent needs routing, urgency and safety screening. Rebuilding them per agent means four inconsistent definitions of "urgent". |
| The tool gateway | One place where tool authorisation, rate limiting and audit are defined. Per-agent implementations cannot be audited together. |
| The audit schema | An investigator should not need to learn four log formats. |
| The evaluation harness | Comparable metrics across agents only exist if the measurement is shared. |
| The PHI boundary | The expensive thing to get right, and the one nobody should get right twice. |

What a new agent actually adds: a specialist, a retrieval index or a tool, some
labelled cases, and a threshold decision. That is the test of whether the
architecture earned its name.

A sequencing that works, ordered by increasing risk tier rather than by calendar:

1. **Informational agents** (tier 1) — published content, no member data. Provider
   manual lookup, formulary questions. Lowest risk, fastest to a measurable result,
   and they prove the retrieval and evaluation machinery.
2. **Member-contextual agents** (tier 2) — this repository. Reads a specific member's
   data, writes nothing.
3. **Staff-facing assistance** (tier 2) — a UM coordinator's prep assistant that
   assembles a case file against the criteria. High value, and the user is a trained
   professional who can spot a bad answer, which is a meaningfully different risk
   profile from a member-facing surface.
4. **Transactional agents** (tier 3) — anything that writes. Different review board,
   human approval on every write, and a rollback path. Not a configuration change
   from tier 2.

The jump from 3 to 4 is where most agent programmes get into trouble, because it
looks like an increment and is not.

---

## Questions a stakeholder should ask, and where the answer lives

| Question | Answer |
| --- | --- |
| "How do we know it isn't making things up?" | Every claim cites a source or an audited system call, and every answer is graded before the member sees it. Ungrounded answers are withheld. `EVALUATION.md` |
| "What happens when it doesn't know?" | It says so and connects a person, with a packet showing what it already found. Four scenarios in `make demo` show exactly this. |
| "Can it deny a claim?" | No. It has no write access to any clinical system, and the one write tool in the catalogue is blocked for its identity. You can watch the gateway refuse it in the demo. |
| "Where does member data go?" | `PHI-BOUNDARY.md`, including what each control does *not* cover. |
| "Who can see what it did?" | A tamper-evident audit record for every decision, retrieval, tool call and handoff. Metadata only, never member text. |
| "What if the model is wrong a lot?" | Measured on labelled cases, with the operating point chosen from a published curve rather than asserted. |
| "What if the model goes down?" | It degrades to a simpler classifier whose confidence is capped below every threshold, so it keeps routing to the right queue and stops auto-answering. |
| "What does it cost to run?" | A decision model call per turn plus a graded answer, with generation only on the traffic that passes the gates. The architecture is shaped around that being the cheap path. |
| "Is any of this preview technology?" | Yes, and it is labelled: `PREVIEW-STATUS.md`. Nothing load-bearing depends on a preview feature. |
