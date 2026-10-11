// Microsoft Foundry account, project, and the three model deployments.
//
// Model choices and the reasoning behind each:
//
//   Microsoft-Decision-1  routing and grading. A single-pass scorer that emits zero
//                         output tokens is what makes "classify and grade every turn"
//                         affordable; a generative model in this slot costs more and
//                         returns prose you then have to threshold.
//   gpt-4.1-mini          answer composition only, and only on grounded passages.
//   text-embedding-3-small  hybrid retrieval.
//
// Deployment type is DataZoneStandard where available so inference stays inside the
// data zone. That is a data-residency decision, not a latency one -- benchmark before
// claiming either.

param location string
param tags object
param accountName string
param projectName string
param decisionDeploymentName string
param chatDeploymentName string
param embeddingDeploymentName string

@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Disabled'

param logAnalyticsId string

@description('GlobalStandard is available everywhere; DataZoneStandard keeps inference in-zone.')
@allowed(['GlobalStandard', 'DataZoneStandard'])
param deploymentSku string = 'GlobalStandard'

param decisionCapacity int = 10
param chatCapacity int = 30
param embeddingCapacity int = 30

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: accountName
    publicNetworkAccess: publicNetworkAccess
    // Key auth off. Every caller authenticates with Entra ID, which means every
    // call is attributable to a principal in the audit trail.
    disableLocalAuth: true
    networkAcls: {
      defaultAction: publicNetworkAccess == 'Disabled' ? 'Deny' : 'Allow'
    }
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Payer Care Agents'
    description: 'Governed multi-agent member services assistant. Synthetic data only.'
  }
}

resource decision 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: decisionDeploymentName
  sku: { name: deploymentSku, capacity: decisionCapacity }
  properties: {
    model: {
      format: 'Microsoft'
      name: 'Microsoft-Decision-1'
      version: '1'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: chatDeploymentName
  sku: { name: deploymentSku, capacity: chatCapacity }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-4.1-mini' }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  // Serialise deployments: the control plane rejects concurrent writes to one account.
  dependsOn: [decision]
}

resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: embeddingDeploymentName
  sku: { name: 'Standard', capacity: embeddingCapacity }
  properties: {
    model: { format: 'OpenAI', name: 'text-embedding-3-small' }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [chat]
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: account
  name: 'to-log-analytics'
  properties: {
    workspaceId: logAnalyticsId
    logs: [
      { categoryGroup: 'audit', enabled: true }
      { categoryGroup: 'allLogs', enabled: true }
    ]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output accountName string = account.name
output endpoint string = account.properties.endpoint
output principalId string = account.identity.principalId
output projectPrincipalId string = project.identity.principalId
output decisionDeployment string = decision.name
