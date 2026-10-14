targetScope = 'resourceGroup'

@description('Prefix used to name resources.')
param namePrefix string = 'voicetester'

param location string = resourceGroup().location

@description('Container image (registry/repo:tag). Leave empty on the first pass to create only the platform resources.')
param image string = ''

@description('Resource group and name of the Foundry account the app talks to.')
param foundryResourceGroup string = 'rg-foundry'
param foundryAccountName string = 'flopes-swc-foundry'
param foundryProjectName string = 'swc-project'

@description('Shared password that gates access to the web app.')
@secure()
param appPassword string = ''

var suffix = uniqueString(resourceGroup().id)
var acrName = take('${replace(namePrefix, '-', '')}${suffix}', 50)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id'
  location: location
}

var acrPullRole = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, acrPullRole)
  scope: acr
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRole)
  }
}

module foundryRoles 'foundry-roles.bicep' = {
  name: 'foundry-roles'
  scope: resourceGroup(foundryResourceGroup)
  params: {
    foundryAccountName: foundryAccountName
    principalId: identity.properties.principalId
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (!empty(image)) {
  name: '${namePrefix}-app'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: [
        { name: 'app-password', value: appPassword }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'APP_PASSWORD', secretRef: 'app-password' }
            { name: 'VOICELIVE_ENDPOINT', value: 'https://${foundryAccountName}.services.ai.azure.com' }
            { name: 'FOUNDRY_PROJECT_NAME', value: foundryProjectName }
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: 'https://${foundryAccountName}.services.ai.azure.com/api/projects/${foundryProjectName}' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 } }
            { type: 'Readiness', httpGet: { path: '/healthz', port: 8000 } }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          { name: 'http', http: { metadata: { concurrentRequests: '20' } } }
        ]
      }
    }
  }
  dependsOn: [acrPull, foundryRoles]
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output identityPrincipalId string = identity.properties.principalId
output appName string = !empty(image) ? app.name : ''
output appUrl string = !empty(image) ? 'https://${app.properties.configuration.ingress.fqdn}' : ''
