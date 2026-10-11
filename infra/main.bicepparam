// Parameters for a development deployment.
//
//   az deployment group create -g rg-payer-agents-dev \
//     -f infra/main.bicep -p infra/main.bicepparam
//
// egressRules is loaded from a generated file so the deployed policy and the policy
// the tests exercise come from one source. Regenerate after editing
// data/policy/egress-policy.json:
//
//   python scripts/export_egress_policy.py

using './main.bicep'

param workloadName = 'payeragents'
param environmentName = 'dev'
param apimPublisherEmail = 'zachery@example.invalid'
param apimPublisherName = 'Payer Care Agents'

// Start in Audit. Review the "Network egress decision" records in Application
// Insights until no legitimate call appears as would-deny, then switch to Enforced.
param egressMode = 'Audit'

param egressRules = loadJsonContent('generated/egress-rules.json')

// Object IDs of the humans who operate the platform. Read-only on models and
// indexes; no FHIR data-plane grant.
param operatorObjectIds = []
