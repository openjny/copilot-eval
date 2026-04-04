// Pattern 1: Resource Explorer — Azure fixture
// Deploys a known set of resources for Copilot to discover and describe.
// Policy-compliant: Private Endpoints, HTTPS-only, Entra auth for SQL.

targetScope = 'resourceGroup'

@description('Azure region for all resources')
param location string

@description('Tags to apply to all resources')
param tags object = {}

@description('Unique suffix for resource names')
param nameSuffix string = uniqueString(resourceGroup().id)

@description('Entra admin object ID for SQL Server (Service Principal or user)')
param sqlAdminObjectId string

@description('Entra admin login name for SQL Server')
param sqlAdminLogin string = 'copilot-eval-runner'

// ─── VNet ───────────────────────────────────────────────────────────
resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: 'vnet-eval-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.0.0.0/16']
    }
    subnets: [
      {
        name: 'snet-app'
        properties: {
          addressPrefix: '10.0.1.0/24'
          delegations: [
            {
              name: 'delegation-appservice'
              properties: {
                serviceName: 'Microsoft.Web/serverFarms'
              }
            }
          ]
        }
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.0.2.0/24'
        }
      }
    ]
  }
}

// ─── App Service Plan ───────────────────────────────────────────────
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'plan-eval-${nameSuffix}'
  location: location
  tags: tags
  sku: {
    name: 'B1'
    tier: 'Basic'
  }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

// ─── App Service (HTTPS only, VNet integrated) ──────────────────────
resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: 'app-eval-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    virtualNetworkSubnetId: vnet.properties.subnets[0].id
    siteConfig: {
      linuxFxVersion: 'NODE|20-lts'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
    }
  }
}

// ─── Storage Account (HTTPS only, no public blob access) ────────────
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stevl${nameSuffix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
    }
  }
}

// ─── Storage Private Endpoint ───────────────────────────────────────
resource storagePe 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-storage-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: vnet.properties.subnets[1].id
    }
    privateLinkServiceConnections: [
      {
        name: 'plsc-storage'
        properties: {
          privateLinkServiceId: storageAccount.id
          groupIds: ['blob']
        }
      }
    ]
  }
}

// ─── SQL Server (Entra-only auth) ───────────────────────────────────
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: 'sql-eval-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    administrators: {
      azureADOnlyAuthentication: true
      administratorType: 'ActiveDirectory'
      login: sqlAdminLogin
      sid: sqlAdminObjectId
      tenantId: subscription().tenantId
      principalType: 'Application'
    }
    publicNetworkAccess: 'Disabled'
    minimalTlsVersion: '1.2'
  }
}

// ─── SQL Database ───────────────────────────────────────────────────
resource sqlDb 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sqlServer
  name: 'sqldb-eval'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
}

// ─── SQL Private Endpoint ───────────────────────────────────────────
resource sqlPe 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-sql-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: vnet.properties.subnets[1].id
    }
    privateLinkServiceConnections: [
      {
        name: 'plsc-sql'
        properties: {
          privateLinkServiceId: sqlServer.id
          groupIds: ['sqlServer']
        }
      }
    ]
  }
}

// ─── Log Analytics Workspace ────────────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-eval-${nameSuffix}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: 'Disabled'
  }
}

// ─── Application Insights ───────────────────────────────────────────
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-eval-${nameSuffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: 'Disabled'
  }
}

// ─── Outputs (for verification) ─────────────────────────────────────
output expectedResources array = [
  vnet.name
  appServicePlan.name
  webApp.name
  storageAccount.name
  sqlServer.name
  '${sqlServer.name}/${sqlDb.name}'
  logAnalytics.name
  appInsights.name
  storagePe.name
  sqlPe.name
]

output resourceCount int = 10
