# bootstrap_mcp.ps1 — provisar MCP-serverns App Service ovanpå befintlig infra.
#
# Kräver att scripts/bootstrap.ps1 redan körts. Återanvänder samma RG, ASP, ACR
# och Key Vault. Skapar en EGEN App Service (app-finauto-mcp-$Suffix) som kör
# mcp_server.py --http (port 8080) med bara bearer-token-auth — ingen Easy Auth.
#
# Vad scriptet gör (idempotent):
#   1. Härleder resursnamn från befintligt RG (-Suffix kan anges manuellt).
#   2. Genererar bearer-token (32 bytes urlsafe) om inte redan i KV.
#   3. Lägger token som secret 'mcp-bearer-token' i kv-finauto-$Suffix.
#   4. Skapar app-finauto-mcp-$Suffix med placeholder-image om saknas.
#   5. Aktiverar System-assigned MI + ger den AcrPull + KV Secrets User.
#   6. Sätter app-settings (WEBSITES_PORT, DATABASE_URL, MCP_BEARER_TOKEN — båda
#      via @Microsoft.KeyVault-refs så token aldrig hamnar i plain-text appsetting).
#   7. Pull från ACR via MI (acrUseManagedIdentityCreds=true).
#   8. Skriver MCP_APP_NAME som GitHub Actions repo variable (om gh installerat).
#
# Användning:
#   .\scripts\bootstrap_mcp.ps1
#   .\scripts\bootstrap_mcp.ps1 -Suffix 6427
#   .\scripts\bootstrap_mcp.ps1 -Suffix 6427 -RotateToken     # tvinga ny token
#
# Efter att scriptet kört: pusha mcp_server.py / Dockerfile.mcp till main så
# .github/workflows/deploy-mcp.yml bygger + deployar containern.

[CmdletBinding()]
param(
    [string]$Suffix,
    [string]$Location = 'swedencentral',
    [string]$BaseName = 'finauto',
    [string]$RepoSlug,
    [switch]$RotateToken,
    [switch]$SkipGitHubVars
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Info($msg) { Write-Host "  $msg" }
function Write-Ok($msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  ! $msg" -ForegroundColor Yellow }

function Invoke-Az {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & az @args
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "az-anrop misslyckades (exit $code): az $($args -join ' ')" }
    return $output
}

function Test-Az {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $output = & az @args 2>$errFile
        $code = $LASTEXITCODE
    } finally {
        Remove-Item $errFile -ErrorAction SilentlyContinue
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { $global:LASTEXITCODE = 0; return $null }
    return $output
}

function New-SuffixFromUser {
    $h = [System.Security.Cryptography.SHA1]::Create().ComputeHash(
        [System.Text.Encoding]::UTF8.GetBytes($env:USERNAME)
    )
    return ($h[0..1] | ForEach-Object { $_.ToString('x2') }) -join ''
}

# ---------------------------------------------------------------------------

Write-Step 'Förberedelser'

if (-not $Suffix) { $Suffix = New-SuffixFromUser }
if (-not $RepoSlug) {
    $remote = git remote get-url origin 2>$null
    if ($LASTEXITCODE -eq 0 -and $remote -match '[:/]([^/:]+)/([^/]+?)(?:\.git)?$') {
        $RepoSlug = "$($Matches[1])/$($Matches[2])"
    } else {
        Write-Warn2 'Kunde inte härleda RepoSlug — gh-stegen hoppas över.'
        $SkipGitHubVars = $true
    }
}

$account = (Invoke-Az account show --output json) | ConvertFrom-Json
Write-Info "Subscription : $($account.name)  ($($account.id))"
Write-Info "Suffix       : $Suffix"
if ($RepoSlug) { Write-Info "Repo         : $RepoSlug" }

$RG       = "rg-$BaseName-$Suffix"
$KV       = "kv-$BaseName-$Suffix"
$Acr      = "cr$BaseName$Suffix"
$Plan     = "asp-$BaseName-$Suffix"
$McpApp   = "app-$BaseName-mcp-$Suffix"
$TokenSecret = 'mcp-bearer-token'

# Verifiera att befintlig infra finns
foreach ($pair in @(@($RG,'resource group','az group show -n'), @($KV,'key vault','az keyvault show -n'),
                    @($Acr,'ACR','az acr show -n'), @($Plan,"plan (-g $RG)","az appservice plan show -g $RG -n"))) {
    $name, $label, $cmd = $pair
    $parts = $cmd -split ' '
    $args = $parts[1..($parts.Length-1)] + $name
    if (-not (Test-Az @args)) {
        throw "$label '$name' hittades inte. Kör scripts/bootstrap.ps1 -Suffix $Suffix först."
    }
}
Write-Ok 'Befintlig infra (RG, KV, ACR, plan) verifierad'

# ---------------------------------------------------------------------------
Write-Step "Bearer-token i Key Vault ($TokenSecret)"
# ---------------------------------------------------------------------------

$existingToken = Test-Az keyvault secret show --vault-name $KV --name $TokenSecret --query value --output tsv
if ($existingToken -and -not $RotateToken) {
    Write-Info 'Token finns redan i Key Vault (använd -RotateToken för att byta).'
    $tokenHash = ([System.BitConverter]::ToString(
        [System.Security.Cryptography.SHA256]::Create().ComputeHash(
            [System.Text.Encoding]::UTF8.GetBytes($existingToken.Trim())
        )
    ) -replace '-','').Substring(0,12).ToLower()
    Write-Info "Token-hash (sha256[:12]): $tokenHash"
} else {
    # Skapa en URL-säker token, 32 bytes → 43 base64url-tecken
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $newToken = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    Invoke-Az keyvault secret set --vault-name $KV --name $TokenSecret --value $newToken --output none
    $tokenHash = ([System.BitConverter]::ToString(
        [System.Security.Cryptography.SHA256]::Create().ComputeHash(
            [System.Text.Encoding]::UTF8.GetBytes($newToken)
        )
    ) -replace '-','').Substring(0,12).ToLower()
    Write-Ok 'Token sparad i KV'
    Write-Info "Token-hash (sha256[:12]): $tokenHash"
    Write-Host ''
    Write-Host '  >>> KOPIERA TOKEN NU — visas bara denna gång <<<' -ForegroundColor Yellow
    Write-Host "  $newToken" -ForegroundColor White
    Write-Host ''
}

# ---------------------------------------------------------------------------
Write-Step "App Service ($McpApp)"
# ---------------------------------------------------------------------------

$placeholderImage = 'mcr.microsoft.com/appsvc/staticsite:latest'
if (-not (Test-Az webapp show --name $McpApp --resource-group $RG --output json)) {
    Invoke-Az webapp create `
        --name $McpApp --resource-group $RG --plan $Plan `
        --container-image-name $placeholderImage `
        --output none
    Write-Ok "Skapade $McpApp (placeholder-image — deploy-mcp.yml byter ut)"
} else {
    Write-Info "$McpApp finns redan"
}

$miPrincipalId = (Invoke-Az webapp identity assign --name $McpApp --resource-group $RG --query principalId --output tsv).Trim()
$mcpHost = "$McpApp.azurewebsites.net"
Write-Info "MCP MI principal: $miPrincipalId"
Write-Info "MCP host        : https://$mcpHost"

$acrId = (Invoke-Az acr show --name $Acr --resource-group $RG --query id --output tsv).Trim()
$kvId  = (Invoke-Az keyvault show --name $KV --resource-group $RG --query id --output tsv).Trim()

function Ensure-Role($principal, $role, $scope, $label) {
    $existing = Test-Az role assignment list --assignee $principal --scope $scope --role $role --output json
    if (-not $existing -or $existing -eq '[]') {
        Invoke-Az role assignment create --assignee-object-id $principal --assignee-principal-type ServicePrincipal --role $role --scope $scope --output none
        Write-Ok "MI fick $role på $label"
    } else {
        Write-Info "MI har redan $role på $label"
    }
}
Ensure-Role $miPrincipalId 'AcrPull'                $acrId 'ACR'
Ensure-Role $miPrincipalId 'Key Vault Secrets User' $kvId  'Key Vault'

Write-Info 'Väntar 30s på RBAC-propagering innan KV-refs i app settings...'
Start-Sleep -Seconds 30

$kvRefDb    = "@Microsoft.KeyVault(SecretUri=https://$KV.vault.azure.net/secrets/database-url/)"
$kvRefToken = "@Microsoft.KeyVault(SecretUri=https://$KV.vault.azure.net/secrets/$TokenSecret/)"
$settingsArr = @(
    @{ name = 'WEBSITES_PORT';     value = '8080';        slotSetting = $false }
    @{ name = 'DATABASE_URL';      value = $kvRefDb;      slotSetting = $false }
    @{ name = 'MCP_BEARER_TOKEN';  value = $kvRefToken;   slotSetting = $false }
)
$tmpSettings = (New-TemporaryFile).FullName
try {
    [System.IO.File]::WriteAllText($tmpSettings, ($settingsArr | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))
    Invoke-Az webapp config appsettings set --name $McpApp --resource-group $RG --settings "@$tmpSettings" --output none
} finally {
    Remove-Item $tmpSettings -ErrorAction SilentlyContinue
}
Write-Ok 'App settings satta (WEBSITES_PORT, DATABASE_URL@KV, MCP_BEARER_TOKEN@KV)'

# Pull från ACR via MI
$tmpAcrCfg = (New-TemporaryFile).FullName
try {
    [System.IO.File]::WriteAllText($tmpAcrCfg, '{"acrUseManagedIdentityCreds":true}', [System.Text.UTF8Encoding]::new($false))
    Invoke-Az webapp config set --name $McpApp --resource-group $RG --generic-configurations "@$tmpAcrCfg" --output none
} finally {
    Remove-Item $tmpAcrCfg -ErrorAction SilentlyContinue
}
Write-Ok 'MCP-app pullar från ACR via MI'

# ---------------------------------------------------------------------------
Write-Step 'GitHub-variabler (för deploy-mcp.yml)'
# ---------------------------------------------------------------------------

if ($SkipGitHubVars) {
    Write-Info 'Hoppar över gh-stegen (-SkipGitHubVars eller saknad gh-CLI).'
} else {
    $ghPath = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $ghPath) {
        Write-Warn2 'gh CLI saknas — sätt MCP_APP_NAME manuellt i repo-vars:'
        Write-Warn2 "  Settings → Secrets and variables → Actions → New repository variable"
        Write-Warn2 "  Name=MCP_APP_NAME Value=$McpApp"
    } else {
        & gh -R $RepoSlug variable set MCP_APP_NAME --body $McpApp 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Satte repo-var MCP_APP_NAME=$McpApp"
        } else {
            Write-Warn2 "gh-anropet misslyckades — sätt MCP_APP_NAME=$McpApp manuellt."
        }
    }
}

# ---------------------------------------------------------------------------
Write-Step 'Klart'
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '  MCP-endpoint för Claude.ai Custom Connector:' -ForegroundColor Cyan
Write-Host "    URL:    https://$mcpHost/mcp" -ForegroundColor White
Write-Host '    Auth:   Bearer-token (visas en gång ovan vid första körningen)'
Write-Host ''
Write-Host '  Nästa steg:' -ForegroundColor Cyan
Write-Host '    1. Pusha mcp_server.py / Dockerfile.mcp / deploy-mcp.yml till main.' -ForegroundColor White
Write-Host '       Workflow:n bygger imagen och deployar till App Service.'
Write-Host "    2. Verifiera: curl https://$mcpHost/healthz   (förväntat: 'ok')"
Write-Host '    3. Ge URL + token till kollegan. I Claude.ai:'
Write-Host '       Settings → Connectors → Add custom connector → klistra URL + token.'
