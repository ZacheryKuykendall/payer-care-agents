// Telemetry and audit sinks.
//
// Two sinks, not one, and the split is intentional. Application telemetry is
// operational data that engineers query freely. The tool-call audit stream is
// evidence: it goes to a dedicated Event Hub with its own consumer group, its own
// retention, and its own access control, so "give the on-call engineer log access"
// does not also mean "give the on-call engineer the access-audit trail".

param location string
param tags object
param logAnalyticsName string
param appInsightsName string
param eventHubNamespaceName string
param auditHubName string

@description('Event Hub retention in days. The hub is a transport; the durable copy lands in the workspace.')
@minValue(1)
@maxValue(7)
param auditRetentionDays int = 7

@description('Log Analytics retention in days.')
param logRetentionDays int = 365

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: logRetentionDays
    features: { enableLogAccessUsingOnlyResourcePermissions: true }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    // Disabling local auth forces Entra-authenticated ingestion, so a leaked
    // instrumentation key cannot be used to write into the workspace.
    DisableLocalAuth: true
    IngestionMode: 'LogAnalytics'
  }
}

resource eventHubNamespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: eventHubNamespaceName
  location: location
  tags: tags
  sku: { name: 'Standard', tier: 'Standard', capacity: 1 }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource auditHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: eventHubNamespace
  name: auditHubName
  properties: {
    messageRetentionInDays: auditRetentionDays
    partitionCount: 2
  }
}

resource auditConsumerGroup 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2024-01-01' = {
  parent: auditHub
  name: 'compliance-archive'
}

resource namespaceDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: eventHubNamespace
  name: 'to-log-analytics'
  properties: {
    workspaceId: logAnalytics.id
    logs: [{ categoryGroup: 'audit', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output logAnalyticsId string = logAnalytics.id
output appInsightsId string = appInsights.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output eventHubNamespaceName string = eventHubNamespace.name
output auditHubName string = auditHub.name
