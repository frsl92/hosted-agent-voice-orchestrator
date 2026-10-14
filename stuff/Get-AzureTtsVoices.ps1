<#
.SYNOPSIS
    Lists the pt-PT text-to-speech voices available in an Azure region,
    authenticating with Microsoft Entra ID (no subscription key).

.DESCRIPTION
    Asks only for the region. It then:
      1. Finds a Speech-capable resource (SpeechServices / AIServices / CognitiveServices)
         in that region in your current subscription. If there are several, you pick one.
      2. Gets an Entra ID token (Az PowerShell module, or Azure CLI as a fallback).
      3. Calls the regional voices/list endpoint with the header
         Authorization: Bearer aad#<resourceId>#<token>
      4. Shows the voices for the locale (pt-PT by default).

    Entra auth for Speech is tied to a resource, which is why the script looks one up
    for you instead of asking.

    Prerequisites:
      - You're signed in:  Connect-AzAccount   (or)   az login
      - The resource has a custom subdomain.
      - Your account has the "Cognitive Services Speech User" role on it.

.EXAMPLE
    .\Get-AzureTtsVoices.ps1
#>
[CmdletBinding()]
param(
    [string] $Locale = 'pt-PT',
    [string] $OutCsv
)

$ErrorActionPreference = 'Stop'
$scopeResource = 'https://cognitiveservices.azure.com'
$speechKinds   = @('SpeechServices', 'AIServices', 'CognitiveServices')

# --- Ask for the region -------------------------------------------------------------
$Region = (Read-Host 'Azure region (e.g. swedencentral, westeurope)').Trim().ToLower() -replace '\s', ''
if (-not $Region) { throw 'No region entered.' }

# --- Pick the tooling ---------------------------------------------------------------
$useAz  = [bool](Get-Module -ListAvailable -Name Az.Accounts)
$useCli = [bool](Get-Command az -ErrorAction SilentlyContinue)
if (-not $useAz -and -not $useCli) {
    throw "Install either the Az PowerShell module ('Install-Module Az -Scope CurrentUser') or the Azure CLI (https://aka.ms/installazurecli)."
}

if ($useAz) {
    Import-Module Az.Accounts, Az.Resources -ErrorAction Stop
    if (-not (Get-AzContext)) { Connect-AzAccount | Out-Null }
    $sub = (Get-AzContext).Subscription.Name
} else {
    $sub = az account show --query name -o tsv
    if ($LASTEXITCODE -ne 0) { throw "Not signed in. Run 'az login' first." }
}

# --- Find a Speech resource in that region ------------------------------------------
Write-Host "Looking for Speech resources in '$Region' (subscription: $sub)..." -ForegroundColor Cyan
if ($useAz) {
    $candidates = Get-AzResource -ResourceType 'Microsoft.CognitiveServices/accounts' |
        Where-Object { $_.Location -eq $Region -and $_.Kind -in $speechKinds } |
        ForEach-Object { [pscustomobject]@{ Name = $_.Name; Kind = $_.Kind; ResourceGroup = $_.ResourceGroupName; Id = $_.ResourceId } }
} else {
    $candidates = az cognitiveservices account list -o json | ConvertFrom-Json |
        Where-Object { $_.location -eq $Region -and $_.kind -in $speechKinds } |
        ForEach-Object { [pscustomobject]@{ Name = $_.name; Kind = $_.kind; ResourceGroup = $_.resourceGroup; Id = $_.id } }
}
$candidates = @($candidates)

if ($candidates.Count -eq 0) {
    throw "No Speech, AI Services or Cognitive Services resource found in '$Region' in subscription '$sub'. Create one there, or switch subscription (Set-AzContext / az account set) and run again."
} elseif ($candidates.Count -eq 1) {
    $resource = $candidates[0]
} else {
    Write-Host "Several resources found:" -ForegroundColor Yellow
    for ($i = 0; $i -lt $candidates.Count; $i++) {
        Write-Host ("  [{0}] {1}  ({2}, rg: {3})" -f ($i + 1), $candidates[$i].Name, $candidates[$i].Kind, $candidates[$i].ResourceGroup)
    }
    $pick = [int](Read-Host "Which one? (1-$($candidates.Count))")
    if ($pick -lt 1 -or $pick -gt $candidates.Count) { throw 'Invalid choice.' }
    $resource = $candidates[$pick - 1]
}
Write-Host "Using '$($resource.Name)'." -ForegroundColor Cyan

# --- Get the Entra ID token ---------------------------------------------------------
if ($useAz) {
    $t = Get-AzAccessToken -ResourceUrl $scopeResource
    # Newer Az.Accounts versions return a SecureString.
    $token = if ($t.Token -is [System.Security.SecureString]) {
        [System.Net.NetworkCredential]::new('', $t.Token).Password
    } else { $t.Token }
} else {
    $token = az account get-access-token --resource $scopeResource --query accessToken -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $token) { throw "Couldn't get a token. Run 'az login' first." }
}

# --- Call the voices list endpoint --------------------------------------------------
$uri = "https://$Region.tts.speech.microsoft.com/cognitiveservices/voices/list"
$headers = @{ Authorization = "Bearer aad#$($resource.Id)#$token" }

try {
    $voices = Invoke-RestMethod -Uri $uri -Headers $headers -Method Get
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    switch ($code) {
        401 { throw "401 Unauthorized. '$($resource.Name)' probably has no custom subdomain, which Entra auth requires." }
        403 { throw "403 Forbidden. Give yourself the 'Cognitive Services Speech User' role on '$($resource.Name)' (it can take a few minutes to apply)." }
        default { throw }
    }
}

$filtered = $voices |
    Where-Object { $_.Locale -eq $Locale } |
    Select-Object ShortName, LocalName, Gender, VoiceType, Status,
                  @{ n = 'Styles'; e = { ($_.StyleList -join ', ') } } |
    Sort-Object VoiceType, ShortName

if (-not $filtered) {
    Write-Warning "No $Locale voices returned in '$Region'."
    return
}

Write-Host "`n$(@($filtered).Count) voice(s) for $Locale in $Region`n" -ForegroundColor Green
$filtered | Format-Table -AutoSize -Wrap

if ($OutCsv) {
    $filtered | Export-Csv -Path $OutCsv -NoTypeInformation -Encoding UTF8
    Write-Host "Saved to $OutCsv" -ForegroundColor Green
}