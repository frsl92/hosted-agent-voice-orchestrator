<#
.SYNOPSIS
  Build and deploy the call-center agent tester to Azure Container Apps.

.DESCRIPTION
  1. Creates the resource group and the platform resources (ACR, Log Analytics, ACA environment,
     user-assigned identity + role assignments on the Foundry account).
  2. Builds the image in ACR (no local Docker needed).
  3. Deploys the container app.
  4. Gates the app with a shared password (-AppPassword, stored as a Container Apps secret and
     exposed to the app as APP_PASSWORD). Anyone who knows it can use the app and your agents.

  Re-running the script is safe. To change the password, re-run with a new -AppPassword.
#>
param(
  [string]$ResourceGroup = "rg-voice-tester",
  [string]$Location = "swedencentral",
  [string]$NamePrefix = "voicetester",
  [string]$FoundryResourceGroup = "rg-foundry",
  [string]$FoundryAccountName = "flopes-swc-foundry",
  [string]$FoundryProjectName = "swc-project",
  [string]$Tag = (Get-Date -Format "yyyyMMddHHmmss"),
  [string]$AppPassword = "meovoicedemo"
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
  "foundryProjectName=$FoundryProjectName", "appPassword=$AppPassword"
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

# Earlier versions used Entra built-in authentication; make sure it is switched off.
$authOn = (& az containerapp auth show -g $ResourceGroup -n $appName --query "platform.enabled" --output tsv 2>$null | Out-String).Trim()
if ($authOn -eq "true") {
  Write-Host "==> Disabling the old Entra built-in authentication"
  Invoke-Az containerapp auth update -g $ResourceGroup -n $appName --enabled false --yes --output none
}

Write-Host ""
Write-Host "Done. Open: $url"
