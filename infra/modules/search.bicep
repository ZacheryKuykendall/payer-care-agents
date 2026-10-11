// Azure AI Search.
//
// Two indexes, not one: plan benefits and medical policy have different owners,
// different refresh cadences and different review boards. Splitting them in the
// infrastructure makes "who approves a change to this content" answerable, and lets
// the benefits index carry plan-scoped filtering that medical policy does not need.
//
// Index schemas are not defined here. Index definitions change far more often than
// infrastructure, and putting them in the same deployment means every field addition
// becomes an infrastructure change request. They are applied by the ingestion job.

param location string
param tags object
param name string

@allowed(['basic', 'standard', 'standard2'])
param sku string = 'basic'

param replicaCount int = 1
param partitionCount int = 1

@allowed(['enabled', 'disabled'])
param publicNetworkAccess string = 'disabled'

param logAnalyticsId string

resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: name
  location: location
  tags: tags
  sku: { name: sku }
  identity: { type: 'SystemAssigned' }
  properties: {
    replicaCount: replicaCount
    partitionCount: partitionCount
    hostingMode: 'default'
    publicNetworkAccess: publicNetworkAccess
    // Entra-only. API keys in an index that holds plan documents are a key-rotation
    // problem nobody schedules.
    disableLocalAuth: true
    semanticSearch: 'standard'
    networkRuleSet: { ipRules: [] }
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: search
  name: 'to-log-analytics'
  properties: {
    workspaceId: logAnalyticsId
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output name string = search.name
output endpoint string = 'https://${search.name}.search.windows.net'
output principalId string = search.identity.principalId
