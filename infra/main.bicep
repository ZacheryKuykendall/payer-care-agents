// Payer Care Agents - Azure infrastructure.
//
// Deploy with:
//   az deployment group create -g <rg> -f infra/main.bicep -p infra/main.bicepparam
//
// Shape of the deployment, and why it is shaped that way:
//
//   Foundry account + project    the models. One account, data-zone deployments.
//   RAI policy (preview)         content safety AND outbound network egress rules.
//   Azure AI Search              benefits and medical policy indexes, two of them.
//   Health Data Services FHIR    the clinical system of record.
//   API Management               the only path from an agent to FHIR.
//   Event Hubs + Log Analytics   the audit sink and the telemetry sink, separate.
//   Key Vault                    no secret lives in app configuration.
//
// Three deliberate choices a reviewer should check:
//   1. Public network access is disabled on the data services. Agents reach them
//      through private endpoints or through API Management, never over the internet.
//   2. Every service-to-service hop uses a managed identity. There is no connection
//      string anywhere in this template.
//   3. The egress rules are generated from data/policy/egress-policy.json, the same
//      file the local simulator reads. See scripts/export_egress_policy.py.

targetScope = 'resourceGroup'

@description('Short name used to derive every resource name. Lowercase letters and digits.')
@minLength(3)
@maxLength(12)
param workloadName string = 'payeragents'

@description('Deployment environment. Drives SKUs and whether public access is allowed.')
@allowed(['dev', 'test', 'prod'])
param environmentName string = 'dev'

@description('Azure region for every resource.')
param location string = resourceGroup().location

@description('Entra ID tenant that issues agent access tokens.')
param entraTenantId string = subscription().tenantId

@description('Object IDs granted data-plane reader access for operating the platform.')
param operatorObjectIds array = []

@description('Publisher email on the API Management instance.')
param apimPublisherEmail string

@description('Publisher name on the API Management instance.')
param apimPublisherName string = 'Payer Care Agents'

@description('Ordered outbound egress rules. Generate with scripts/export_egress_policy.py.')
param egressRules array

@description('Egress enforcement mode. Start in Audit and only switch after reviewing would-deny records.')
@allowed(['Audit', 'Enforced'])
param egressMode string = 'Audit'

@description('Deployment name for the decision model used by the router and the grading judge.')
param decisionDeploymentName string = 'decision-router'

@description('Deployment name for the generation model used to compose grounded answers.')
param chatDeploymentName string = 'gpt-4.1-mini'

@description('Deployment name for the embedding model used by hybrid retrieval.')
param embeddingDeploymentName string = 'text-embedding-3-small'

@description('Tags applied to every resource.')
param tags object = {
  workload: workloadName
  environment: environmentName
  dataClassification: 'phi'
  managedBy: 'bicep'
}

var suffix = uniqueString(resourceGroup().id, workloadName)
var isProduction = environmentName == 'prod'
var names = {
  foundry: '${workloadName}-ai-${suffix}'
  search: '${workloadName}-search-${suffix}'
  apim: '${workloadName}-apim-${suffix}'
  health: '${workloadName}-hds-${suffix}'
  fhir: 'clinical'
  keyVault: take('${workloadName}kv${suffix}', 24)
  logAnalytics: '${workloadName}-logs-${suffix}'
  appInsights: '${workloadName}-ai-insights-${suffix}'
  eventHubNamespace: '${workloadName}-ehns-${suffix}'
  auditHub: 'tool-audit'
}

// ---------------------------------------------------------------- monitoring

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    tags: tags
    logAnalyticsName: names.logAnalytics
    appInsightsName: names.appInsights
    eventHubNamespaceName: names.eventHubNamespace
    auditHubName: names.auditHub
    // Audit records are evidence. Retaining them for less than the regulatory
    // minimum is a compliance finding, not a cost saving.
    auditRetentionDays: isProduction ? 7 : 1
    logRetentionDays: isProduction ? 365 : 30
  }
}

// ------------------------------------------------------------------- secrets

module keyVault 'modules/key-vault.bicep' = {
  name: 'key-vault'
  params: {
    location: location
    tags: tags
    name: names.keyVault
    tenantId: entraTenantId
    publicNetworkAccess: isProduction ? 'Disabled' : 'Enabled'
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

// ------------------------------------------------------------------- Foundry

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  params: {
    location: location
    tags: tags
    accountName: names.foundry
    projectName: '${workloadName}-project'
    decisionDeploymentName: decisionDeploymentName
    chatDeploymentName: chatDeploymentName
    embeddingDeploymentName: embeddingDeploymentName
    publicNetworkAccess: isProduction ? 'Disabled' : 'Enabled'
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

// PREVIEW. Network egress controls for Foundry hosted agents are in public preview.
// Microsoft states they carry no preview SLA and are not intended for production.
// See docs/PREVIEW-STATUS.md before relying on this as a compensating control.
module egressPolicy 'modules/rai-egress-policy.bicep' = {
  name: 'rai-egress-policy'
  params: {
    accountName: foundry.outputs.accountName
    policyName: '${workloadName}-egress'
    egressMode: egressMode
    egressRules: egressRules
  }
}

// -------------------------------------------------------------------- search

module search 'modules/search.bicep' = {
  name: 'search'
  params: {
    location: location
    tags: tags
    name: names.search
    sku: isProduction ? 'standard' : 'basic'
    replicaCount: isProduction ? 2 : 1
    publicNetworkAccess: isProduction ? 'disabled' : 'enabled'
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

// ---------------------------------------------------------------------- FHIR

module fhir 'modules/fhir.bicep' = {
  name: 'fhir'
  params: {
    location: location
    tags: tags
    workspaceName: replace(names.health, '-', '')
    fhirServiceName: names.fhir
    tenantId: entraTenantId
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

// ----------------------------------------------------------------------- APIM

module apim 'modules/apim.bicep' = {
  name: 'apim'
  params: {
    location: location
    tags: tags
    name: names.apim
    publisherEmail: apimPublisherEmail
    publisherName: apimPublisherName
    sku: isProduction ? 'Standard' : 'Developer'
    entraTenantId: entraTenantId
    fhirServiceUrl: fhir.outputs.serviceUrl
    fhirServiceName: '${replace(names.health, '-', '')}-${names.fhir}'
    appInsightsId: monitoring.outputs.appInsightsId
    appInsightsKey: monitoring.outputs.appInsightsConnectionString
    eventHubNamespaceName: monitoring.outputs.eventHubNamespaceName
    auditHubName: names.auditHub
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

// ----------------------------------------------------------------------- RBAC

module rbac 'modules/rbac.bicep' = {
  name: 'rbac'
  params: {
    apimPrincipalId: apim.outputs.principalId
    foundryPrincipalId: foundry.outputs.principalId
    foundryAccountName: foundry.outputs.accountName
    searchName: search.outputs.name
    keyVaultName: keyVault.outputs.name
    fhirWorkspaceName: replace(names.health, '-', '')
    fhirServiceName: names.fhir
    operatorObjectIds: operatorObjectIds
  }
}

// -------------------------------------------------------------------- outputs

@description('Set as FOUNDRY_PROJECT_ENDPOINT.')
output foundryEndpoint string = foundry.outputs.endpoint

@description('Set as AZURE_SEARCH_ENDPOINT.')
output searchEndpoint string = search.outputs.endpoint

@description('Set as MCP_GATEWAY_ENDPOINT. Agents never call the FHIR URL directly.')
output mcpEndpoint string = '${apim.outputs.gatewayUrl}/mcp'

@description('Backend FHIR URL. Present for operators; the egress policy denies it to agents.')
output fhirServiceUrl string = fhir.outputs.serviceUrl

output appInsightsConnectionString string = monitoring.outputs.appInsightsConnectionString
output raiPolicyId string = egressPolicy.outputs.policyId
output raiPolicyName string = egressPolicy.outputs.policyName
