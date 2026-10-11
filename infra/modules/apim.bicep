// API Management: the only path from an agent to a clinical system.
//
// This is the control point the whole architecture leans on. The agent holds a
// gateway credential and nothing else; API Management authenticates to FHIR with its
// own managed identity. That single indirection is what makes authentication, rate
// limiting, tool-level authorisation and audit enforceable in one place instead of
// re-implemented in each agent.
//
// Policy XML lives in ../apim/policies/*.xml and is loaded with loadTextContent so
// the files stay reviewable as files -- inline XML in Bicep is unreadable in a diff
// and unlintable in CI.

param location string
param tags object
param name string
param publisherEmail string
param publisherName string

@allowed(['Developer', 'Basic', 'Standard', 'Premium'])
param sku string = 'Developer'

param skuCapacity int = 1
param entraTenantId string
param fhirServiceUrl string
param fhirServiceName string
param appInsightsId string
param appInsightsKey string
param eventHubNamespaceName string
param auditHubName string
param logAnalyticsId string

@description('Browser origin allowed to call the gateway. Tighten per environment.')
param memberPortalOrigin string = 'https://members.example.invalid'

@description('Client application IDs permitted to call the MCP tools.')
param memberAssistantClientId string = '00000000-0000-0000-0000-000000000000'
param providerIntakeClientId string = '00000000-0000-0000-0000-000000000000'
param mcpApiAudience string = 'api://payer-agents-mcp'

@description('Agent subnet range allowed through the gateway IP filter.')
param agentSubnetStart string = '10.10.2.0'
param agentSubnetEnd string = '10.10.2.255'

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: name
  location: location
  tags: tags
  sku: { name: sku, capacity: skuCapacity }
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
    // TLS 1.2 floor on every protocol, and the legacy cipher suites off.
    customProperties: {
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls10': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls11': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls10': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls11': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Protocols.Server.Http2': 'true'
    }
  }
}

// -- named values referenced by the policy XML -------------------------------

var namedValues = [
  { name: 'entra-tenant-id', value: entraTenantId }
  { name: 'fhir-service-name', value: fhirServiceName }
  { name: 'member-portal-origin', value: memberPortalOrigin }
  { name: 'member-assistant-client-id', value: memberAssistantClientId }
  { name: 'provider-intake-client-id', value: providerIntakeClientId }
  { name: 'mcp-api-audience', value: mcpApiAudience }
  { name: 'agent-subnet-start', value: agentSubnetStart }
  { name: 'agent-subnet-end', value: agentSubnetEnd }
]

resource values 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = [
  for item in namedValues: {
    parent: apim
    name: item.name
    properties: {
      displayName: item.name
      value: item.value
      secret: false
    }
  }
]

// -- observability -----------------------------------------------------------

resource apimInsights 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'app-insights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsightsId
    credentials: { connectionString: appInsightsKey }
  }
}

resource eventHubNamespaceRef 'Microsoft.EventHub/namespaces@2024-01-01' existing = {
  name: eventHubNamespaceName
}

resource auditLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'payer-agents-audit'
  properties: {
    loggerType: 'azureEventHub'
    description: 'Tool-call audit stream. Metadata only, never request or response bodies.'
    credentials: {
      // Managed identity, not a connection string: the audit path should not have a
      // credential that can be copied out of the gateway configuration.
      endpointAddress: '${eventHubNamespaceRef.name}.servicebus.windows.net'
      identityClientId: 'SystemAssigned'
      name: auditHubName
    }
  }
}

resource diagnostics 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = {
  parent: apim
  name: 'applicationinsights'
  properties: {
    loggerId: apimInsights.id
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
    // Bodies are never logged. Response-body logging also breaks MCP transport.
    frontend: {
      request: { headers: ['x-correlation-id', 'x-agent-id'], body: { bytes: 0 } }
      response: { headers: ['x-correlation-id'], body: { bytes: 0 } }
    }
    backend: {
      request: { headers: [], body: { bytes: 0 } }
      response: { headers: [], body: { bytes: 0 } }
    }
  }
}

resource serviceDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: apim
  name: 'to-log-analytics'
  properties: {
    workspaceId: logAnalyticsId
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

// -- backend and APIs ---------------------------------------------------------

resource fhirBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  parent: apim
  name: 'fhir-backend'
  properties: {
    protocol: 'http'
    url: fhirServiceUrl
    tls: { validateCertificateChain: true, validateCertificateName: true }
  }
}

resource globalPolicy 'Microsoft.ApiManagement/service/policies@2024-05-01' = {
  parent: apim
  name: 'policy'
  properties: {
    value: loadTextContent('../apim/policies/global.xml')
    format: 'rawxml'
  }
  dependsOn: [values]
}

resource fhirApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'fhir-r4'
  properties: {
    displayName: 'FHIR R4 (read-only for agents)'
    path: 'fhir'
    protocols: ['https']
    serviceUrl: fhirServiceUrl
    subscriptionRequired: true
    apiType: 'http'
  }
}

resource fhirApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: fhirApi
  name: 'policy'
  properties: {
    value: loadTextContent('../apim/policies/fhir-api.xml')
    format: 'rawxml'
  }
  dependsOn: [values, globalPolicy]
}

// The MCP server itself is created from the FHIR API's operations in the portal or
// via the management API, selecting which operations become tools. Its policy
// document is apim/policies/mcp-server.xml. See infra/README.md for the exact step
// and why it is not expressed here.

output name string = apim.name
output gatewayUrl string = apim.properties.gatewayUrl
output principalId string = apim.identity.principalId
output fhirApiName string = fhirApi.name
