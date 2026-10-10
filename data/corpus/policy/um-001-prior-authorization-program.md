---
doc_id: um-001-prior-authorization-program
title: "Utilization Management UM-001: 2026 Prior Authorization Program"
doc_type: utilization_management
plan_ids: []
effective_date: 2026-01-01
url: https://docs.example.invalid/northwind/um/um-001
owner: Utilization Management
review_cycle: annual
---

> SYNTHETIC SAMPLE DOCUMENT. Invented program rules for demonstration only.

## Services that require prior authorization

| Category | Examples |
| --- | --- |
| Advanced imaging | MRI, CT, PET, nuclear cardiology |
| Elective inpatient admission | Scheduled surgical and medical admissions |
| Outpatient surgery | Spinal procedures, joint replacement, bariatric surgery |
| Post-acute care | Skilled nursing facility, inpatient rehabilitation, home health |
| Durable medical equipment | PAP devices, power mobility, continuous glucose monitors |
| Specialty drugs | Medical benefit infusions and injectables on the specialty list |
| Behavioral health | Inpatient and residential treatment, applied behavior analysis |

Emergency services never require prior authorization. Post-stabilization care
following an emergency admission requires notification within 24 hours rather than
prior authorization.

## How to submit

Providers submit through the provider portal, by secure fax, or through an X12 278
transaction from their practice management system. Each submission receives a
reference number in the form `PA-` followed by eight digits. The reference number is
the identifier the member sees on correspondence.

## Determination timeframes

| Request type | Timeframe |
| --- | --- |
| Standard pre-service | 14 calendar days from receipt |
| Expedited pre-service | 72 hours from receipt |
| Standard Part B drug | 72 hours from receipt |
| Expedited Part B drug | 24 hours from receipt |
| Concurrent review | 24 hours from receipt |

A request is expedited when applying the standard timeframe could seriously
jeopardize the member's life, health, or ability to regain maximum function. A
member, their representative, or the treating provider may request expedited review.

## Statuses a member may see

| Status | Meaning |
| --- | --- |
| Received | The request is logged and in intake review |
| Pending information | The plan has requested additional clinical documentation |
| In clinical review | A nurse reviewer or medical director is evaluating the request |
| Approved | The service is authorized; the letter lists the approved units and dates |
| Partially approved | Some requested units or services were authorized |
| Denied | The service was not authorized; appeal rights are included in the letter |
| Withdrawn | The requesting provider withdrew the request |

## Who may deny a request

Only a physician or other appropriate licensed clinical peer reviewer may issue a
denial based on medical necessity. Non-clinical staff and automated systems may
approve requests that clearly meet criteria, but may never deny one. This rule is the
reason the automated assistant in this reference architecture is permitted to report
a determination and never to make one.

## Gold carding

Providers whose requests in a given service category were approved at a rate above
95% over the preceding 12 months, with a minimum volume of 20 requests, are exempt
from prior authorization in that category for the following plan year.
