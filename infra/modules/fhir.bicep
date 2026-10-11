// Azure Health Data Services FHIR service.
//
// The clinical system of record. Two properties carry most of the compliance weight:
//
//   smartProxyEnabled: false      no legacy proxy auth path
//   authenticationConfiguration   Entra ID only, with the service as its own audience
//
// `exportConfiguration` is present but unconfigured on purpose. Bulk export is the
// single highest-risk operation a FHIR service offers, and it should be turned on
// deliberately with its own storage account, its own RBAC and its own review -- not
// inherited from a template someone copied.

param location string
param tags object
param workspaceName string
param fhirServiceName string
param tenantId string
param logAnalyticsId string

@description('FHIR versions other than R4 would require revisiting every tool mapping.')
@allowed(['fhir-R4'])
param fhirVersion string = 'fhir-R4'

resource workspace 'Microsoft.HealthcareApis/workspaces@2024-03-31' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    publicNetworkAccess: 'Disabled'
  }
}

resource fhirService 'Microsoft.HealthcareApis/workspaces/fhirservices@2024-03-31' = {
  parent: workspace
  name: fhirServiceName
  location: location
  tags: tags
  kind: fhirVersion
  identity: { type: 'SystemAssigned' }
  properties: {
    authenticationConfiguration: {
      authority: '${environment().authentication.loginEndpoint}${tenantId}'
      audience: 'https://${workspaceName}-${fhirServiceName}.fhir.azurehealthcareapis.com'
      smartProxyEnabled: false
    }
    // CORS is empty: no browser talks to this service. Only API Management does.
    corsConfiguration: {
      origins: []
      headers: []
      methods: []
      allowCredentials: false
    }
    resourceVersionPolicyConfiguration: {
      // Versioned history on every resource. An auditor asking "what did this
      // authorization say on the day the member called" needs the old version.
      default: 'versioned'
    }
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: fhirService
  name: 'to-log-analytics'
  properties: {
    workspaceId: logAnalyticsId
    logs: [{ categoryGroup: 'audit', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output workspaceName string = workspace.name
output serviceName string = fhirService.name
output serviceUrl string = 'https://${workspaceName}-${fhirServiceName}.fhir.azurehealthcareapis.com'
output principalId string = fhirService.identity.principalId
