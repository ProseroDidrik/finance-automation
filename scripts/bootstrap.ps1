# bootstrap.ps1 — skapa hela Azure-infran för finance-automation i ett svep.
#
# Idempotent: varje resurs kollas med `az ... show` innan den skapas. Du kan
# köra om skriptet hur många gånger som helst utan att förstöra något (med
# undantag för Postgres-admin-lösenordet, se nedan).
#
# Skriptet täcker fas 1 (infra) + fas 2 (auth-app + Easy Auth) + fas 3 (GitHub
# OIDC). Det som inte automatiseras:
#   * Easy Auth groups-claim på App Registration kan kräva ett portal-besök
#     (Token configuration → Add groups claim → SecurityGroup, både ID + Access)
#     om Graph-PATCH-en faller. Skriptet flaggar det.
#   * Faktisk dataflytt körs separat: scripts/migrate_duckdb_to_postgres.py
#     och scripts/push_master.py.
#   * Första container-deployen sker via .github/workflows/deploy.yml efter
#     att secrets/vars satts (skriptet pushar dem via gh CLI om tillgängligt).
#
# MAESTRO_GROUP_ID-problemet i privat tenant: Maestro-gruppen finns inte i
# didrik.wachtmeister@gmail.com-tenanten. Skapa en throwaway säkerhetsgrupp,
# lägg in dig själv som medlem, och skicka in dess Object ID som -MaestroGroupId.
# Vid Prosero-migrationen ersätts den med riktiga Maestro-OID:n.
#
# Användning:
#   .\scripts\bootstrap.ps1 -MaestroGroupId <guid>
#   .\scripts\bootstrap.ps1 -MaestroGroupId <guid> -Suffix abc1
#   .\scripts\bootstrap.ps1 -MaestroGroupId <guid> -SkipGitHubSecrets

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, HelpMessage = 'Object ID på säkerhetsgruppen som gatear /api/* (sätts som MAESTRO_GROUP_ID).')]
    [string]$MaestroGroupId,

    [string]$Suffix,

    [string]$Location = 'swedencentral',

    [string]$BaseName = 'finauto',

    [string]$RepoSlug,

    [switch]$SkipGitHubSecrets
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Info($msg) { Write-Host "  $msg" }
function Write-Ok($msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

function Invoke-Az {
    # az returnerar JSON på stdout men sätter $LASTEXITCODE vid fel.
    # PS 5.1 wrappar stderr som NativeCommandError vilket triggar
    # $ErrorActionPreference='Stop'. Skydda med lokal Continue.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & az @args
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) {
        throw "az-anrop misslyckades (exit $code): az $($args -join ' ')"
    }
    return $output
}

function Test-Az {
    # Som Invoke-Az men sväljer fel; returnerar $null vid icke-noll exit.
    # Skickar stderr till en temp-fil för att undvika att PS wrappar det
    # som NativeCommandError under Stop-mode.
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
    if ($code -ne 0) {
        $global:LASTEXITCODE = 0
        return $null
    }
    return $output
}

function New-Suffix {
    # 4 hex-tecken deterministiskt från username — samma maskin = samma suffix.
    $h = [System.Security.Cryptography.SHA1]::Create().ComputeHash(
        [System.Text.Encoding]::UTF8.GetBytes($env:USERNAME)
    )
    return ($h[0..1] | ForEach-Object { $_.ToString('x2') }) -join ''
}

function Get-MyPublicIp {
    try {
        return (Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 5)
    } catch {
        throw "Kunde inte hämta publik IP från ipify: $_"
    }
}

function New-StrongPassword {
    # 28 tecken alfanumeriskt + få utvalda symboler. Undviker cmd.exe-meta
    # (& | < > ^ ( ) " %) och URL-osäkra tecken (@ : / ? # & = + space %)
    # som annars bryter både az.cmd-anropet och DATABASE_URL-parsningen.
    $upper = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZ'
    $lower = [char[]]'abcdefghijkmnpqrstuvwxyz'
    $digit = [char[]]'23456789'
    $sym   = [char[]]'_-.'
    $all   = $upper + $lower + $digit + $sym
    $rng   = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 28
    $rng.GetBytes($bytes)
    $chars = foreach ($b in $bytes) { $all[$b % $all.Length] }
    # Säkerställ minst en av varje grupp (Postgres-krav: 3 av 4)
    $chars[0] = $upper | Get-Random
    $chars[1] = $lower | Get-Random
    $chars[2] = $digit | Get-Random
    $chars[3] = $sym   | Get-Random
    return -join $chars
}

# ---------------------------------------------------------------------------
# Förberedelser
# ---------------------------------------------------------------------------

Write-Step 'Förberedelser'

if (-not $Suffix) { $Suffix = New-Suffix }
if (-not $RepoSlug) {
    $remote = git remote get-url origin 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $remote) {
        throw 'Kunde inte hämta git remote origin. Ange -RepoSlug owner/repo.'
    }
    if ($remote -match '[:/]([^/:]+)/([^/]+?)(?:\.git)?$') {
        $RepoSlug = "$($Matches[1])/$($Matches[2])"
    } else {
        throw "Kunde inte parsa repo-slug ur remote: $remote"
    }
}

# Verifiera az-login + subscription
$accountJson = Invoke-Az account show --output json
$account = $accountJson | ConvertFrom-Json
$SubscriptionId = $account.id
$TenantId = $account.tenantId
$MyOid = (Invoke-Az ad signed-in-user show --query id --output tsv).Trim()

Write-Info "Subscription : $($account.name)  ($SubscriptionId)"
Write-Info "Tenant       : $TenantId"
Write-Info "Inloggad som : $($account.user.name)  (oid $MyOid)"
Write-Info "Region       : $Location"
Write-Info "Suffix       : $Suffix"
Write-Info "Repo         : $RepoSlug"

# ---------------------------------------------------------------------------
# Resursnamn
# ---------------------------------------------------------------------------

$RG       = "rg-$BaseName-$Suffix"
$PgServer = "psql-$BaseName-$Suffix"
$PgDb     = 'finance'
$PgAdmin  = 'pgadmin'
$Storage  = "st$BaseName$Suffix"            # 24 tecken max, lowercase alfanum
$KV       = "kv-$BaseName-$Suffix"          # 24 tecken max
$Acr      = "cr$BaseName$Suffix"            # alfanum 5-50
$Plan     = "asp-$BaseName-$Suffix"
$WebApp   = "app-$BaseName-$Suffix"
$WebAuthApp   = "$BaseName-webapp-$Suffix"      # display-name för App Registration (user-login)
$WebDeployApp = "$BaseName-gh-deploy-$Suffix"   # display-name för App Registration (GH OIDC)
$MasterContainer = 'master'
$MasterBlobName  = 'Dotterbolagslista.xlsx'

# Validera namn-längder — fail-fast hellre än kryptiskt fel från az
foreach ($pair in @(@($Storage, 24, 'storage'), @($KV, 24, 'keyvault'), @($Acr, 50, 'acr'))) {
    if ($pair[0].Length -gt $pair[1]) {
        throw "Namn '$($pair[0])' ($($pair[2])) är $($pair[0].Length) tecken — max $($pair[1])."
    }
}

# ---------------------------------------------------------------------------
# Provider-registrering (engång per subscription)
# ---------------------------------------------------------------------------

Write-Step 'Provider-registrering'
$providers = @(
    'Microsoft.DBforPostgreSQL',
    'Microsoft.Storage',
    'Microsoft.KeyVault',
    'Microsoft.ContainerRegistry',
    'Microsoft.Web'
)
$pendingProviders = @()
foreach ($p in $providers) {
    $state = (Invoke-Az provider show -n $p --query registrationState -o tsv).Trim()
    if ($state -eq 'Registered') {
        Write-Info "$p : Registered"
    } else {
        Write-Info "$p : $state - registrerar..."
        Invoke-Az provider register -n $p --output none
        $pendingProviders += $p
    }
}
foreach ($p in $pendingProviders) {
    while ((Invoke-Az provider show -n $p --query registrationState -o tsv).Trim() -ne 'Registered') {
        Start-Sleep -Seconds 10
    }
    Write-Ok "$p registrerad"
}

# ---------------------------------------------------------------------------
# Resource Group
# ---------------------------------------------------------------------------

Write-Step 'Resource Group'
if (-not (Test-Az group show --name $RG --output none)) {
    Invoke-Az group create --name $RG --location $Location --output none
    Write-Ok "Skapade $RG"
} else {
    Write-Info "$RG finns redan"
}

# ---------------------------------------------------------------------------
# Postgres Flexible Server
# ---------------------------------------------------------------------------

Write-Step 'Postgres Flexible Server'
$pgPassword = $null
$existingPg = Test-Az postgres flexible-server show --resource-group $RG --name $PgServer --output json
if (-not $existingPg) {
    $pgPassword = New-StrongPassword
    Write-Info "Skapar $PgServer (Standard_B1ms, 32 GB, ~`$15/mån). Detta tar 3-5 min..."
    Invoke-Az postgres flexible-server create `
        --resource-group $RG `
        --name $PgServer `
        --location $Location `
        --admin-user $PgAdmin `
        --admin-password $pgPassword `
        --sku-name Standard_B1ms `
        --tier Burstable `
        --storage-size 32 `
        --version 16 `
        --yes `
        --output none
    Write-Ok "Skapade $PgServer"
} else {
    Write-Warn2 "$PgServer finns redan — admin-lösenord kan inte återskapas. Hämtas från Key Vault om secret finns."
}

# Skapa databasen om den saknas
$existingDb = Test-Az postgres flexible-server db show --resource-group $RG --server-name $PgServer --database-name $PgDb --output json
if (-not $existingDb) {
    Invoke-Az postgres flexible-server db create --resource-group $RG --server-name $PgServer --database-name $PgDb --output none
    Write-Ok "Skapade db $PgDb"
} else {
    Write-Info "Db $PgDb finns redan"
}

# Firewall: tillåt Azure-tjänster (App Service outbound) + min publika IP
$allowAzureRule = 'AllowAllAzureServicesAndResourcesWithinAzureIps'
if (-not (Test-Az postgres flexible-server firewall-rule show --resource-group $RG --name $PgServer --rule-name $allowAzureRule --output json)) {
    Invoke-Az postgres flexible-server firewall-rule create `
        --resource-group $RG --name $PgServer `
        --rule-name $allowAzureRule `
        --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 `
        --output none
    Write-Ok "Firewall: tillåt Azure-tjänster"
}

$myIp = Get-MyPublicIp
$myIpRule = "MyIp-$($myIp -replace '\.','-')"
if (-not (Test-Az postgres flexible-server firewall-rule show --resource-group $RG --name $PgServer --rule-name $myIpRule --output json)) {
    Invoke-Az postgres flexible-server firewall-rule create `
        --resource-group $RG --name $PgServer `
        --rule-name $myIpRule `
        --start-ip-address $myIp --end-ip-address $myIp `
        --output none
    Write-Ok "Firewall: tillåt $myIp (din IP, för migrate-skript)"
}

$pgFqdn = (Invoke-Az postgres flexible-server show --resource-group $RG --name $PgServer --query fullyQualifiedDomainName --output tsv).Trim()
Write-Info "Postgres FQDN: $pgFqdn"

# ---------------------------------------------------------------------------
# Key Vault + database-url-secret
# ---------------------------------------------------------------------------

Write-Step 'Key Vault'
if (-not (Test-Az keyvault show --name $KV --resource-group $RG --output json)) {
    Invoke-Az keyvault create `
        --name $KV --resource-group $RG --location $Location `
        --enable-rbac-authorization true `
        --output none
    Write-Ok "Skapade $KV"
} else {
    Write-Info "$KV finns redan"
}

$kvId = (Invoke-Az keyvault show --name $KV --resource-group $RG --query id --output tsv).Trim()

# Ge dig själv `Key Vault Administrator` så du kan sätta secrets
$existingKvRole = Test-Az role assignment list --assignee $MyOid --scope $kvId --role 'Key Vault Administrator' --output json
if (-not $existingKvRole -or $existingKvRole -eq '[]') {
    Invoke-Az role assignment create --assignee $MyOid --role 'Key Vault Administrator' --scope $kvId --output none
    Write-Ok 'Du fick rollen Key Vault Administrator'
    Start-Sleep -Seconds 30  # RBAC-propagering hinner ofta inte före nästa secret-set
}

# Sätt database-url-secret om Postgres just skapades
if ($pgPassword) {
    $databaseUrl = "postgresql://${PgAdmin}:${pgPassword}@${pgFqdn}:5432/${PgDb}?sslmode=require"
    Invoke-Az keyvault secret set --vault-name $KV --name 'database-url' --value $databaseUrl --output none
    Write-Ok "Lagrade database-url i $KV"
    Remove-Variable pgPassword, databaseUrl
} elseif (-not (Test-Az keyvault secret show --vault-name $KV --name 'database-url' --output json)) {
    Write-Warn2 'database-url-secret saknas i Key Vault. Postgres-servern fanns redan men secreten gör inte det.'
    Write-Warn2 'Du måste antingen återställa lösenordet (az postgres flexible-server update --admin-password) eller dropa servern och köra om.'
}

# ---------------------------------------------------------------------------
# Storage Account + master-container
# ---------------------------------------------------------------------------

Write-Step 'Storage Account + master-container'
if (-not (Test-Az storage account show --name $Storage --resource-group $RG --output json)) {
    Invoke-Az storage account create `
        --name $Storage --resource-group $RG --location $Location `
        --sku Standard_LRS --kind StorageV2 `
        --allow-blob-public-access false `
        --output none
    Write-Ok "Skapade $Storage"
} else {
    Write-Info "$Storage finns redan"
}

$storageId = (Invoke-Az storage account show --name $Storage --resource-group $RG --query id --output tsv).Trim()

# Container med RBAC-autentiserat anrop (kräver Storage Blob Data-roll på dig)
$existingMyStorageRole = Test-Az role assignment list --assignee $MyOid --scope $storageId --role 'Storage Blob Data Contributor' --output json
if (-not $existingMyStorageRole -or $existingMyStorageRole -eq '[]') {
    Invoke-Az role assignment create --assignee $MyOid --role 'Storage Blob Data Contributor' --scope $storageId --output none
    Write-Ok 'Du fick rollen Storage Blob Data Contributor'
    Start-Sleep -Seconds 30
}

if (-not (Test-Az storage container show --name $MasterContainer --account-name $Storage --auth-mode login --output json)) {
    Invoke-Az storage container create --name $MasterContainer --account-name $Storage --auth-mode login --output none
    Write-Ok "Skapade container $MasterContainer"
} else {
    Write-Info "Container $MasterContainer finns redan"
}

$MasterBlobUrl = "https://$Storage.blob.core.windows.net/$MasterContainer/$MasterBlobName"

# ---------------------------------------------------------------------------
# Container Registry
# ---------------------------------------------------------------------------

Write-Step 'Container Registry'
if (-not (Test-Az acr show --name $Acr --resource-group $RG --output json)) {
    Invoke-Az acr create --name $Acr --resource-group $RG --location $Location --sku Basic --output none
    Write-Ok "Skapade $Acr"
} else {
    Write-Info "$Acr finns redan"
}
$acrId = (Invoke-Az acr show --name $Acr --resource-group $RG --query id --output tsv).Trim()

# ---------------------------------------------------------------------------
# App Service Plan + Webapp (Linux container)
# ---------------------------------------------------------------------------

Write-Step 'App Service Plan + Webapp'
if (-not (Test-Az appservice plan show --name $Plan --resource-group $RG --output json)) {
    Invoke-Az appservice plan create `
        --name $Plan --resource-group $RG --location $Location `
        --is-linux --sku B1 `
        --output none
    Write-Ok "Skapade $Plan (B1)"
} else {
    Write-Info "$Plan finns redan"
}

$placeholderImage = 'mcr.microsoft.com/appsvc/staticsite:latest'
if (-not (Test-Az webapp show --name $WebApp --resource-group $RG --output json)) {
    Invoke-Az webapp create `
        --name $WebApp --resource-group $RG --plan $Plan `
        --container-image-name $placeholderImage `
        --output none
    Write-Ok "Skapade $WebApp (placeholder-image)"
} else {
    Write-Info "$WebApp finns redan"
}

# Slå på system-assigned MI och hämta dess principalId
$miPrincipalId = (Invoke-Az webapp identity assign --name $WebApp --resource-group $RG --query principalId --output tsv).Trim()
$webappId = (Invoke-Az webapp show --name $WebApp --resource-group $RG --query id --output tsv).Trim()
$webappHost = "$WebApp.azurewebsites.net"
Write-Info "Webapp MI principal: $miPrincipalId"
Write-Info "Webapp host        : $webappHost"

# Tilldela MI-roller
function Ensure-Role($principal, $role, $scope, $label) {
    $existing = Test-Az role assignment list --assignee $principal --scope $scope --role $role --output json
    if (-not $existing -or $existing -eq '[]') {
        Invoke-Az role assignment create --assignee-object-id $principal --assignee-principal-type ServicePrincipal --role $role --scope $scope --output none
        Write-Ok "MI fick $role på $label"
    }
}
Ensure-Role $miPrincipalId 'AcrPull'                   $acrId     'ACR'
Ensure-Role $miPrincipalId 'Key Vault Secrets User'    $kvId      'Key Vault'
Ensure-Role $miPrincipalId 'Storage Blob Data Reader'  $storageId 'Storage'

Write-Info 'Väntar 30s på RBAC-propagering innan KV-referens i app settings...'
Start-Sleep -Seconds 30

# Sätt app settings (efter MI-roller, annars resolv:as KV-referensen inte).
# Skickas via JSON-fil eftersom KV-refsens '(' ')' är cmd.exe-meta och bryter
# inline --settings KEY=VAL-format.
$kvRefDb = "@Microsoft.KeyVault(SecretUri=https://$KV.vault.azure.net/secrets/database-url/)"
$settingsArr = @(
    @{ name = 'WEBSITES_PORT';    value = '8080';           slotSetting = $false }
    @{ name = 'DATABASE_URL';     value = $kvRefDb;         slotSetting = $false }
    @{ name = 'MAESTRO_GROUP_ID'; value = $MaestroGroupId;  slotSetting = $false }
    @{ name = 'MASTER_BLOB_URL';  value = $MasterBlobUrl;   slotSetting = $false }
)
$tmpSettings = (New-TemporaryFile).FullName
try {
    [System.IO.File]::WriteAllText($tmpSettings, ($settingsArr | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))
    Invoke-Az webapp config appsettings set --name $WebApp --resource-group $RG --settings "@$tmpSettings" --output none
} finally {
    Remove-Item $tmpSettings -ErrorAction SilentlyContinue
}
Write-Ok 'App settings satta (WEBSITES_PORT, DATABASE_URL->KV, MAESTRO_GROUP_ID, MASTER_BLOB_URL)'

# Kör webapp:en på ACR via MI istället för admin-creds (JSON-fil av samma skäl)
$tmpAcrCfg = (New-TemporaryFile).FullName
try {
    [System.IO.File]::WriteAllText($tmpAcrCfg, '{"acrUseManagedIdentityCreds":true}', [System.Text.UTF8Encoding]::new($false))
    Invoke-Az webapp config set --name $WebApp --resource-group $RG --generic-configurations "@$tmpAcrCfg" --output none
} finally {
    Remove-Item $tmpAcrCfg -ErrorAction SilentlyContinue
}
Write-Ok 'Webapp pull:ar från ACR via MI'

# ---------------------------------------------------------------------------
# App Registration för Easy Auth (user-login)
# ---------------------------------------------------------------------------

Write-Step 'App Registration: Easy Auth (user-login)'

$replyUrl = "https://$webappHost/.auth/login/aad/callback"
$existingAuthApp = Test-Az ad app list --display-name $WebAuthApp --query "[0]" --output json
if (-not $existingAuthApp -or $existingAuthApp -eq 'null') {
    Invoke-Az ad app create `
        --display-name $WebAuthApp `
        --sign-in-audience AzureADMyOrg `
        --web-redirect-uris $replyUrl `
        --enable-id-token-issuance true `
        --output none
    Write-Ok "Skapade App Registration $WebAuthApp"
} else {
    Write-Info "App Registration $WebAuthApp finns redan"
}

$authAppId = (Invoke-Az ad app list --display-name $WebAuthApp --query '[0].appId' --output tsv).Trim()
$authObjectId = (Invoke-Az ad app list --display-name $WebAuthApp --query '[0].id' --output tsv).Trim()

# Säkerställ att rätt redirect-URI finns
Invoke-Az ad app update --id $authAppId --web-redirect-uris $replyUrl --output none

# groupMembershipClaims=SecurityGroup + optionalClaims med groups på id+access
$manifestPatch = @{
    groupMembershipClaims = 'SecurityGroup'
    optionalClaims = @{
        idToken     = @(@{ name = 'groups'; essential = $false; additionalProperties = @() })
        accessToken = @(@{ name = 'groups'; essential = $false; additionalProperties = @() })
        saml2Token  = @()
    }
} | ConvertTo-Json -Depth 6 -Compress

$tmpManifest = (New-TemporaryFile).FullName
try {
    # PS 5.1: Set-Content -Encoding utf8 skriver BOM. Graph-PATCH tål oftast
    # men säkrast utan BOM via .NET.
    [System.IO.File]::WriteAllText($tmpManifest, $manifestPatch, [System.Text.UTF8Encoding]::new($false))
    Invoke-Az rest --method PATCH `
        --uri "https://graph.microsoft.com/v1.0/applications/$authObjectId" `
        --headers 'Content-Type=application/json' `
        --body "@$tmpManifest" `
        --output none
    Write-Ok 'Groups-claim aktiverad (groupMembershipClaims + optionalClaims)'
} catch {
    Write-Warn2 "Kunde inte sätta groups-claim via Graph: $_"
    Write-Warn2 "Gör manuellt: portal.azure.com → App Registrations → $WebAuthApp → Token configuration → Add groups claim → SecurityGroup, kryssa ID + Access."
} finally {
    Remove-Item $tmpManifest -ErrorAction SilentlyContinue
}

# Skapa client secret för Easy Auth code-flow (krävs vid callback-exchange)
$easyAuthSecret = (Invoke-Az ad app credential reset --id $authAppId --display-name 'easy-auth' --years 2 --append --query password -o tsv).Trim()

# Slå på Easy Auth (V1-syntax — default på nuvarande az CLI).
# V2-issuer (login.microsoftonline.com/.../v2.0) eftersom moderna App Registrations
# utfärdar V2-tokens som Easy Auth annars rejecterar med 401 vid callback.
Write-Info 'Konfigurerar Easy Auth på webapp:en...'
Invoke-Az webapp auth update `
    --name $WebApp --resource-group $RG `
    --enabled true `
    --action LoginWithAzureActiveDirectory `
    --aad-client-id $authAppId `
    --aad-client-secret $easyAuthSecret `
    --aad-token-issuer-url "https://login.microsoftonline.com/$TenantId/v2.0" `
    --aad-allowed-token-audiences $authAppId `
    --output none
Remove-Variable easyAuthSecret
Write-Ok 'Easy Auth aktiverad (Microsoft-provider, omdirigerar oinloggade)'

# ---------------------------------------------------------------------------
# App Registration för GitHub Actions OIDC
# ---------------------------------------------------------------------------

Write-Step 'App Registration: GitHub Actions OIDC'

$existingDeployApp = Test-Az ad app list --display-name $WebDeployApp --query "[0]" --output json
if (-not $existingDeployApp -or $existingDeployApp -eq 'null') {
    Invoke-Az ad app create --display-name $WebDeployApp --sign-in-audience AzureADMyOrg --output none
    Write-Ok "Skapade App Registration $WebDeployApp"
}
$deployAppId = (Invoke-Az ad app list --display-name $WebDeployApp --query '[0].appId' --output tsv).Trim()
$deployObjectId = (Invoke-Az ad app list --display-name $WebDeployApp --query '[0].id' --output tsv).Trim()

# Service Principal
$existingSp = Test-Az ad sp show --id $deployAppId --output json
if (-not $existingSp) {
    Invoke-Az ad sp create --id $deployAppId --output none
    Write-Ok 'Skapade Service Principal'
}
$deploySpOid = (Invoke-Az ad sp show --id $deployAppId --query id --output tsv).Trim()

# Roller: AcrPush på ACR + Website Contributor på webapp
Ensure-Role $deploySpOid 'AcrPush'              $acrId     'ACR (deploy-SP)'
Ensure-Role $deploySpOid 'Website Contributor'  $webappId  'Webapp (deploy-SP)'

# Federated credential bunden till main-branch
$fedSubject = "repo:${RepoSlug}:ref:refs/heads/main"
$fedCredName = 'github-main'
$existingFed = Test-Az ad app federated-credential list --id $deployObjectId --query "[?name=='$fedCredName'] | [0]" --output json
if (-not $existingFed -or $existingFed -eq 'null') {
    $fedJson = @{
        name      = $fedCredName
        issuer    = 'https://token.actions.githubusercontent.com'
        subject   = $fedSubject
        audiences = @('api://AzureADTokenExchange')
    } | ConvertTo-Json -Compress
    $tmpFed = (New-TemporaryFile).FullName
    try {
        [System.IO.File]::WriteAllText($tmpFed, $fedJson, [System.Text.UTF8Encoding]::new($false))
        Invoke-Az ad app federated-credential create --id $deployObjectId --parameters "@$tmpFed" --output none
        Write-Ok "Federated credential: $fedSubject"
    } finally {
        Remove-Item $tmpFed -ErrorAction SilentlyContinue
    }
} else {
    Write-Info 'Federated credential finns redan'
}

# ---------------------------------------------------------------------------
# GitHub repo secrets/vars (om gh CLI är inloggad)
# ---------------------------------------------------------------------------

Write-Step 'GitHub repo secrets/vars'
if ($SkipGitHubSecrets) {
    Write-Info 'Hoppar över (-SkipGitHubSecrets)'
} else {
    $ghAvailable = (Get-Command gh -ErrorAction SilentlyContinue) -ne $null
    if ($ghAvailable) {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $errFile = [System.IO.Path]::GetTempFileName()
        try {
            & gh auth status 2>$errFile | Out-Null
            $ghAvailable = ($LASTEXITCODE -eq 0)
        } finally {
            Remove-Item $errFile -ErrorAction SilentlyContinue
            $global:LASTEXITCODE = 0
            $ErrorActionPreference = $prevEap
        }
    }
    if ($ghAvailable) {
        & gh secret set AZURE_CLIENT_ID       --repo $RepoSlug --body $deployAppId
        & gh secret set AZURE_TENANT_ID       --repo $RepoSlug --body $TenantId
        & gh secret set AZURE_SUBSCRIPTION_ID --repo $RepoSlug --body $SubscriptionId
        & gh variable set ACR_NAME       --repo $RepoSlug --body $Acr
        & gh variable set APP_NAME       --repo $RepoSlug --body $WebApp
        & gh variable set RESOURCE_GROUP --repo $RepoSlug --body $RG
        Write-Ok "Pushade secrets/vars till $RepoSlug"
    } else {
        Write-Warn2 'gh CLI saknas eller inte inloggad. Sätt manuellt:'
        Write-Host ''
        Write-Host "  Repo: https://github.com/$RepoSlug/settings/secrets/actions"
        Write-Host "    AZURE_CLIENT_ID       = $deployAppId"
        Write-Host "    AZURE_TENANT_ID       = $TenantId"
        Write-Host "    AZURE_SUBSCRIPTION_ID = $SubscriptionId"
        Write-Host ''
        Write-Host "  Repo: https://github.com/$RepoSlug/settings/variables/actions"
        Write-Host "    ACR_NAME       = $Acr"
        Write-Host "    APP_NAME       = $WebApp"
        Write-Host "    RESOURCE_GROUP = $RG"
    }
}

# ---------------------------------------------------------------------------
# Sammanfattning
# ---------------------------------------------------------------------------

Write-Step 'Klart — översikt'
Write-Host ""
Write-Host "  Resource Group : $RG"
Write-Host "  Postgres FQDN  : $pgFqdn  (db: $PgDb, user: $PgAdmin)"
Write-Host "  Key Vault      : $KV   (secret: database-url)"
Write-Host "  Storage        : $Storage  (container: $MasterContainer)"
Write-Host "  Master URL     : $MasterBlobUrl"
Write-Host "  ACR            : $Acr.azurecr.io"
Write-Host "  Webapp         : https://$webappHost"
Write-Host "  Auth App Reg   : $WebAuthApp  ($authAppId)"
Write-Host "  Deploy App Reg : $WebDeployApp  ($deployAppId)"
Write-Host ""
Write-Host "Nästa steg:" -ForegroundColor Cyan
Write-Host "  1. Hämta DATABASE_URL för lokal migrate-körning:"
Write-Host "       `$env:DATABASE_URL = (az keyvault secret show --vault-name $KV --name database-url --query value -o tsv)"
Write-Host "  2. Migrera DuckDB → Postgres + verifiera:"
Write-Host "       py scripts/migrate_duckdb_to_postgres.py --verify"
Write-Host "  3. Pusha master-filen:"
Write-Host "       `$env:MASTER_BLOB_URL = '$MasterBlobUrl'"
Write-Host "       py scripts/push_master.py"
Write-Host "  4. Trigga första deploy (push till main eller Run workflow):"
Write-Host "       gh workflow run deploy.yml --repo $RepoSlug"
Write-Host "  5. När deploy:n är grön: öppna https://$webappHost — du ska redirectas till Microsoft-login."
Write-Host ""
Write-Host "Verifiera i portalen:" -ForegroundColor Yellow
Write-Host "  * App Registration '$WebAuthApp' → Token configuration → groups-claim med ID + Access"
Write-Host "    (om Graph-PATCH:en gick fram syns claims redan; annars manuellt klick)"
Write-Host "  * Att din användare är medlem av gruppen $MaestroGroupId"
