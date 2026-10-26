// One ADLS Gen2 storage account (hierarchical namespace) for Artemis web data.
// Lifecycle: raw/ -> Cool -> Archive; text/tokens stay Hot. Grants the pipeline
// managed identity "Storage Blob Data Contributor".  NOT deployed by this repo.

@description('Storage account name (3-24 lowercase letters/digits, globally unique)')
param name string
param location string
@description('Principal ID of the user-assigned managed identity used by Batch tasks')
param principalId string
param coolAfterDays int = 30
param archiveAfterDays int = 90
param deleteRawAfterDays int = 0
param containers array = [
  'raw'
  'text'
  'filtered'
  'dedup'
  'scrubbed'
  'tokens'
  'manifests'
  'logs'
]
param tags object = {}

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    isHnsEnabled: true               // ADLS Gen2 hierarchical namespace
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false      // identity (Entra ID) auth only
    supportsHttpsTrafficOnly: true
    largeFileSharesState: 'Disabled'
    networkAcls: {
      defaultAction: 'Allow'         // tighten to VNet/private endpoints for production
      bypass: 'AzureServices'
    }
  }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: sa
  name: 'default'
}

resource ctr 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for c in containers: {
  parent: blob
  name: c
  properties: {
    publicAccess: 'None'
  }
}]

var rawActions = union({
  tierToCool: { daysAfterModificationGreaterThan: coolAfterDays }
  tierToArchive: { daysAfterModificationGreaterThan: archiveAfterDays }
}, deleteRawAfterDays > 0 ? { delete: { daysAfterModificationGreaterThan: deleteRawAfterDays } } : {})

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: sa
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'raw-to-cool-then-archive'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [ 'blockBlob' ]
              prefixMatch: [ 'raw/' ]
            }
            actions: {
              baseBlob: rawActions
            }
          }
        }
        {
          name: 'intermediates-to-cool'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [ 'blockBlob' ]
              prefixMatch: [ 'text/', 'filtered/', 'dedup/', 'logs/' ]
            }
            actions: {
              baseBlob: {
                tierToCool: { daysAfterModificationGreaterThan: coolAfterDays }
              }
            }
          }
        }
      ]
    }
  }
}

// Storage Blob Data Contributor
var blobContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')

resource ra 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sa
  name: guid(sa.id, principalId, blobContributor)
  properties: {
    roleDefinitionId: blobContributor
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

output id string = sa.id
output name string = sa.name
output dfsEndpoint string = sa.properties.primaryEndpoints.dfs
