<#
.SYNOPSIS
    Lists the Azure AI Speech text-to-speech voices available to a Speech resource,
    authenticating with Microsoft Entra ID (no subscription key).

.DESCRIPTION
    1. Gets an Entra ID access token for Cognitive Services, using the Az PowerShell
       module if it's installed, or the Azure CLI otherwise.
    2. Builds the Speech-specific header:  Authorization: Bearer aad#<resourceId>#<token>
    3. Calls the regional voices/list endpoint and filters by locale.

    Prerequisites:
      - The Speech (or Foundry / AIServices) resource has a custom subdomain
        (required for Entra ID auth).
      - Your identity has the "Cognitive Services Speech User" (or "... Speech Contributor")
        role on the resource.
      - You're signed in:  Connect-AzAccount   (or)   az login

.EXAMPLE
    .\Get-AzureTtsVoices.ps1 -ResourceGroup rg-speech -ResourceName my-speech

.EXAMPLE
    .\Get-AzureTtsVoices.ps1 -ResourceId "/subscriptions/<sub>/resourceGroups/rg-speech/providers/Microsoft.CognitiveServices/accounts/my-speech" -Region swedencentral -Locale pt-PT -OutCsv voices.csv
#>
[CmdletBinding(DefaultParameterSetName = 'ByName')]
param(
    [Parameter(Mandatory, ParameterSetName = 'ByName')]
    [string] $ResourceGroup,

    [Parameter(Mandatory, ParameterSetName = 'ByName')]
    [string] $ResourceName,

    [Parameter(Mandatory, ParameterSetName = 'ById')]
    [string] $ResourceId,

    [string] $Region = 'swedencentral',

    [string] $Locale = 'pt-PT',

    [string] $OutCsv
)

$ErrorActionPreference = 'Stop'
$scopeResource = 'https://cognitiveservices.azure.com'

function Test-Command($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

$useAz  = (Get-Module -ListAvailable -Name Az.Accounts) -ne $null
$useCli = Test-Command 'az'
if (-not $useAz -and -not $useCli) {
    throw "Neither the Az PowerShell module nor the Azure CLI was found. Install one: 'Install-Module Az -Scope CurrentUser' or https://aka.ms/installazurecli"
}

# --- Resolve the resource ID (and region, if we can) ---------------------------------
if ($PSCmdlet.ParameterSetName -eq 'ByName') {
    Write-Host "Looking up resource '$ResourceName' in '$ResourceGroup'..." -ForegroundColor Cyan
    if ($useAz) {
        Import-Module Az.Accounts -ErrorAction Stop
        if (-not (Get-AzContext)) { Connect-AzAccount | Out-Null }
        $res = Get-AzResource -ResourceGroupName $ResourceGroup -Name $ResourceName `
                              -ResourceType 'Microsoft.CognitiveServices/accounts'
        $ResourceId = $res.ResourceId
        $resLocation = $res.Location
    } else {
        $res = az cognitiveservices account show -g $ResourceGroup -n $ResourceName -o json | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw "Couldn't find the resource. Are you logged in ('az login')?" }
        $ResourceId = $res.id
        $resLocation = $res.location
    }
    if ($resLocation -and -not $PSBoundParameters.ContainsKey('Region')) {
        $Region = $resLocation
    } elseif ($resLocation -and $resLocation -ne $Region) {
        Write-Warning "Resource is in '$resLocation' but -Region is '$Region'. Entra tokens only work against the resource's own region."
    }
}

# --- Get the Entra ID token ---------------------------------------------------------
Write-Host "Getting Entra ID token..." -ForegroundColor Cyan
$token = $null
if ($useAz) {
    Import-Module Az.Accounts -ErrorAction Stop
    if (-not (Get-AzContext)) { Connect-AzAccount | Out-Null }
    $t = Get-AzAccessToken -ResourceUrl $scopeResource
    # Az.Accounts 5.x+ returns a SecureString; older versions return plain text.
    if ($t.Token -is [System.Security.SecureString]) {
        $token = [System.Net.NetworkCredential]::new('', $t.Token).Password
    } else {
        $token = $t.Token
    }
} else {
    $token = az account get-access-token --resource $scopeResource --query accessToken -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $token) { throw "Couldn't get a token. Run 'az login' first." }
}

# --- Call the voices list endpoint --------------------------------------------------
$uri = "https://$Region.tts.speech.microsoft.com/cognitiveservices/voices/list"
$headers = @{ Authorization = "Bearer aad#$ResourceId#$token" }

Write-Host "Querying $uri ..." -ForegroundColor Cyan
try {
    $voices = Invoke-RestMethod -Uri $uri -Headers $headers -Method Get
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    switch ($code) {
        401 { throw "401 Unauthorized. Check that the resource has a custom subdomain and that -Region matches the resource's region." }
        403 { throw "403 Forbidden. Assign yourself the 'Cognitive Services Speech User' role on the resource (it can take a few minutes to apply)." }
        default { throw }
    }
}

$filtered = $voices |
    Where-Object { -not $Locale -or $_.Locale -eq $Locale } |
    Select-Object ShortName, LocalName, Gender, VoiceType, Status,
                  @{ n = 'Styles'; e = { ($_.StyleList -join ', ') } },
                  @{ n = 'SecondaryLocales'; e = { ($_.SecondaryLocaleList -join ', ') } } |
    Sort-Object VoiceType, ShortName

if (-not $filtered) {
    Write-Warning "No voices found for locale '$Locale' in '$Region'."
    return
}

Write-Host "`n$($filtered.Count) voice(s) for $Locale in $Region`n" -ForegroundColor Green
$filtered | Format-Table -AutoSize -Wrap

if ($OutCsv) {
    $filtered | Export-Csv -Path $OutCsv -NoTypeInformation -Encoding UTF8
    Write-Host "Saved to $OutCsv" -ForegroundColor Green
}