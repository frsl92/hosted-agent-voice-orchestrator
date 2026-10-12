<#
.SYNOPSIS
  Build and deploy the call-center agent tester to Azure Container Apps.

.DESCRIPTION
  1. Creates the resource group and the platform resources (ACR, Log Analytics, ACA environment,
     user-assigned identity + role assignments on the Foundry account).
  2. Builds the image in ACR (no local Docker needed).
  3. Deploys the container app.
  4. Protects the app with Microsoft Entra sign-in (Container Apps built-in authentication),
     restricted to the current tenant. Use -SkipAuth only for a throw-away private test.

  Re-running the script is safe: it updates the image and keeps the existing app registration.
#>
param(
  [string]$ResourceGroup = "rg-voice-tester",
  [string]$Location = "swedencentral",
  [string]$NamePrefix = "voicetester",
  [string]$FoundryResourceGroup = "rg-foundry",
  [string]$FoundryAccountName = "flopes-swc-foundry",
  [string]$FoundryProjectName = "swc-project",
  [string]$Tag = (Get-Date -Format "yyyyMMddHHmmss"),
  [switch]$SkipAuth
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$infra = Join-Path $root "infra\main.bicep"

function Invoke-Az {
  $out = & az @args 2>&1
  if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed:`n$out" }
  $out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }
}

$common = @(
  "--resource-group", $ResourceGroup, "--template-file", $infra,
  "--parameters", "namePrefix=$NamePrefix", "location=$Location",
  "foundryResourceGroup=$FoundryResourceGroup", "foundryAccountName=$FoundryAccountName",
  "foundryProjectName=$FoundryProjectName"
)

Write-Host "==> Resource group $ResourceGroup ($Location)"
Invoke-Az group create --name $ResourceGroup --location $Location --output none

Write-Host "==> Platform resources"
$platform = Invoke-Az deployment group create @common "image=" --name "platform" --query "properties.outputs" --output json | ConvertFrom-Json
$acr = $platform.acrName.value
Write-Host "    registry: $acr"

Write-Host "==> requirements.txt (uv export)"
Push-Location $root
try {
  uv export --no-dev --no-hashes --no-emit-project --frozen --format requirements-txt --output-file requirements.txt | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "uv export failed" }

  Write-Host "==> Building image in ACR"
  $image = "$acr.azurecr.io/$NamePrefix-web:$Tag"
  Invoke-Az acr build --registry $acr --resource-group $ResourceGroup --image "$NamePrefix-web:$Tag" --file Dockerfile . --no-logs --output none
}
finally {
  Pop-Location
}

Write-Host "==> Deploying container app ($image)"
$app = Invoke-Az deployment group create @common "image=$image" --name "app-$Tag" --query "properties.outputs" --output json | ConvertFrom-Json
$appName = $app.appName.value
$url = $app.appUrl.value

if (-not $SkipAuth) {
  Write-Host "==> Entra sign-in (built-in authentication)"
  $tenant = (Invoke-Az account show --query tenantId --output tsv).Trim()
  $displayName = "$NamePrefix-web"
  $clientId = (Invoke-Az ad app list --display-name $displayName --query "[0].appId" --output tsv | Out-String).Trim()
  if (-not $clientId) {
    $clientId = (Invoke-Az ad app create --display-name $displayName --sign-in-audience AzureADMyOrg --query appId --output tsv).Trim()
    Write-Host "    created app registration $clientId"
  }
  $redirect = "$url/.auth/login/aad/callback"
  Invoke-Az ad app update --id $clientId --web-redirect-uris $redirect --enable-id-token-issuance true --output none
  $secret = (Invoke-Az ad app credential reset --id $clientId --display-name "aca-easyauth" --years 1 --query password --output tsv).Trim()
  Invoke-Az containerapp secret set -g $ResourceGroup -n $appName --secrets "aad-client-secret=$secret" --output none
  Invoke-Az containerapp auth microsoft update -g $ResourceGroup -n $appName `
    --client-id $clientId --client-secret-name aad-client-secret `
    --issuer "https://login.microsoftonline.com/$tenant/v2.0" --yes --output none
  Invoke-Az containerapp auth update -g $ResourceGroup -n $appName --enabled true `
    --unauthenticated-client-action RedirectToLoginPage --redirect-provider aad --yes --output none
  Write-Host "    sign-in required for tenant $tenant"
}
else {
  Write-Warning "Authentication skipped: the URL is open to anyone and can talk to your agents."
}

Write-Host ""
Write-Host "Done. Open: $url"
