// PREVIEW: outbound network egress controls for Foundry hosted agents.
//
// Microsoft ships network egress controls on the *same* Responsible AI policy
// resource that carries content safety. Two switches, one resource, and conflating
// them is the most common way to deploy an egress policy that silently does nothing:
//
//   properties.mode                 content safety  (Blocking / Annotate)
//   properties.egressPolicy.mode    network         (Audit / Enforced)
//
// Rules are ordered and first match wins. Hosts may be exact or wildcards such as
// `*.contoso.com`. A policy supports at most 480 rules across all actions. Evaluation
// fails closed: if the policy cannot be evaluated the request is denied. Foundry's own
// platform domains are allow-listed by the service, so `defaultAction: Deny` does not
// sever the runtime's own connectivity.
//
// Rollout: deploy with egressMode 'Audit', review the "Network egress decision"
// records in Application Insights until no legitimate call shows as would-deny, then
// redeploy with 'Enforced'. Transform and Rewrite actions still run in Audit during
// preview, so do not depend on them.
//
// Attach by passing the policy's full ARM resource ID to a hosted agent version. The
// bare policy name is the wrong handle.
//
// Preview status, no preview SLA, and Microsoft states it is not intended for
// production. Treat it as one control in a stack, not as the PHI boundary.
// See docs/PREVIEW-STATUS.md.

param accountName string
param policyName string

@allowed(['Audit', 'Enforced'])
param egressMode string = 'Audit'

@allowed(['Allow', 'Deny'])
param defaultAction string = 'Deny'

@description('Content-safety mode. Separate switch from the network mode above.')
@allowed(['Blocking', 'Annotate'])
param contentSafetyMode string = 'Blocking'

param basePolicyName string = 'Microsoft.DefaultV2'

@description('''Ordered rules in ARM shape. Generate from data/policy/egress-policy.json:
  python scripts/export_egress_policy.py --format bicepparam''')
param egressRules array

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource raiPolicy 'Microsoft.CognitiveServices/accounts/raiPolicies@2026-05-15-preview' = {
  parent: account
  name: policyName
  properties: {
    basePolicyName: basePolicyName
    mode: contentSafetyMode
    egressPolicy: {
      mode: egressMode
      defaultAction: defaultAction
      rules: egressRules
    }
  }
}

@description('Pass this full resource ID to the hosted agent version, not the bare name.')
output policyId string = raiPolicy.id
output policyName string = raiPolicy.name
output ruleCount int = length(egressRules)
