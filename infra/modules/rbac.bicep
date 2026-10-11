// Role assignments.
//
// The rule applied throughout: every grant is the narrowest built-in role that lets
// the principal do its job, scoped to one resource, and justified in a comment. If a
// grant needs a paragraph to justify, it is probably too broad.
//
// Note what API Management is *not* given. It gets FHIR Data Reader, never
// Contributor and never Data Writer, because the member-facing path has no reason to
// write to a clinical system. A provider-intake path that does write would be a
// separate gateway identity with a separate grant, so "who can write to FHIR" stays
// answerable from the role assignments alone.

param apimPrincipalId string
param foundryPrincipalId string
param foundryAccountName string
param searchName string
param keyVaultName string
param fhirWorkspaceName string
param fhirServiceName string

@description('Human operators granted read-only data-plane access.')
param operatorObjectIds array = []

var roles = {
  // Search
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  // Cognitive Services / Foundry
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  // Key Vault
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  // FHIR
  fhirDataReader: '4c8d0bbc-75d3-4935-991f-5f3c56d81508'
  // Event Hubs
  eventHubDataSender: '2b629674-e913-4c01-ae53-ef4638d8f975'
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource search 'Microsoft.Search/searchServices@2023-11-01' existing = {
  name: searchName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource fhirService 'Microsoft.HealthcareApis/workspaces/fhirservices@2024-03-31' existing = {
  name: '${fhirWorkspaceName}/${fhirServiceName}'
}

// -- API Management ----------------------------------------------------------

// Read clinical data on behalf of agents. Read only, by design.
resource apimFhirReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: fhirService
  name: guid(fhirService.id, apimPrincipalId, roles.fhirDataReader)
  properties: {
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', roles.fhirDataReader
    )
  }
}

// -- Foundry account ----------------------------------------------------------

// Query the retrieval indexes. Reader, not Contributor: the agent runtime reads the
// corpus; the ingestion pipeline is what writes it, under a different identity.
resource foundrySearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, foundryPrincipalId, roles.searchIndexDataReader)
  properties: {
    principalId: foundryPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader
    )
  }
}

resource foundryKeyVaultUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, foundryPrincipalId, roles.keyVaultSecretsUser)
  properties: {
    principalId: foundryPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser
    )
  }
}

// -- operators -----------------------------------------------------------------

// Read-only on the models and the indexes. Deliberately no FHIR grant: operating the
// platform does not require reading member records, and the easiest way to keep that
// true is to never grant it by default.
resource operatorModelUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for objectId in operatorObjectIds: {
    scope: foundryAccount
    name: guid(foundryAccount.id, objectId, roles.cognitiveServicesUser)
    properties: {
      principalId: objectId
      principalType: 'User'
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesUser
      )
    }
  }
]

resource operatorSearchReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for objectId in operatorObjectIds: {
    scope: search
    name: guid(search.id, objectId, roles.searchIndexDataReader)
    properties: {
      principalId: objectId
      principalType: 'User'
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader
      )
    }
  }
]

output apimFhirRoleAssignmentId string = apimFhirReader.id
output grantedOperatorCount int = length(operatorObjectIds)
