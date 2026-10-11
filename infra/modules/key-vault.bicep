// Key Vault.
//
// This architecture is designed so that almost nothing needs to live here: every
// service-to-service hop uses a managed identity. What remains is the small set of
// credentials for systems that cannot do Entra ID -- a legacy claims adapter, an
// SFTP key for an EDI partner. Keeping the vault nearly empty is the goal, and a
// growing secret count is a signal that an integration took the shortcut.

param location string
param tags object
param name string
param tenantId string

@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Disabled'

param logAnalyticsId string

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: publicNetworkAccess == 'Disabled' ? 'Deny' : 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: vault
  name: 'to-log-analytics'
  properties: {
    workspaceId: logAnalyticsId
    logs: [{ categoryGroup: 'audit', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output name string = vault.name
output uri string = vault.properties.vaultUri
