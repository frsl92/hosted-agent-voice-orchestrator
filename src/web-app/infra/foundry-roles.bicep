targetScope = 'resourceGroup'

param foundryAccountName string
param principalId string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

// Azure AI User: invoke agents / read agent session logs. Cognitive Services User: Voice Live.
var roles = [
  { name: 'azure-ai-user', id: '53ca6127-db72-4b80-b1b0-d745d6d5456d' }
  { name: 'cognitive-services-user', id: 'a97b65f3-24c7-4388-baec-2e87135dc908' }
]

resource assignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in roles: {
  name: guid(account.id, principalId, r.id)
  scope: account
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r.id)
  }
}]
