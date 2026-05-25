# Wrapper för natt-körning av SAF-T journal-dubblett-städning.
#
# Användning manuellt (kör ikväll innan du går och lägger dig):
#     pwsh -File scripts/run_saft_cleanup_night.ps1
#
# Schemaläggning via Windows Task Scheduler (engångskörning kl 23:00 ikväll):
#     $repo = "C:\Users\DidWac\dev\finance-automation"
#     schtasks /create /tn "SaftCleanup" `
#       /tr "powershell -ExecutionPolicy Bypass -File $repo\scripts\run_saft_cleanup_night.ps1" `
#       /sc once /st 23:00 /sd (Get-Date -Format yyyy/MM/dd)
#
# Skriptet är idempotent — säkert att köra om vid avbrott.
# Loggar progress till _logs/saft_cleanup_YYYYMMDD_HHMMSS.log.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# Logfil med tidsstämpel — undviker att skriva över tidigare körningar.
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = Join-Path $repoRoot "_logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logFile = Join-Path $logDir "saft_cleanup_$stamp.log"

Write-Host "==> SAF-T cleanup wrapper start  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "    repo:    $repoRoot"
Write-Host "    log:     $logFile"

# Hämta DATABASE_URL från Key Vault. Kräver giltig az-session.
Write-Host "    fetching DATABASE_URL from kv-finauto-6427..."
try {
    $env:DATABASE_URL = (az keyvault secret show `
        --vault-name kv-finauto-6427 --name database-url --query value -o tsv)
}
catch {
    Write-Host "ERROR: kunde inte hämta DATABASE_URL ur Key Vault." -ForegroundColor Red
    Write-Host "       Kör 'az login' först." -ForegroundColor Red
    exit 2
}
if (-not $env:DATABASE_URL) {
    Write-Host "ERROR: DATABASE_URL är tom." -ForegroundColor Red
    exit 2
}

Write-Host "==> Kör cleanup_saft_journal_dups.py --execute"
py -u scripts/cleanup_saft_journal_dups.py --execute --log $logFile
$exitCode = $LASTEXITCODE

Write-Host ""
Write-Host "==> Färdigt. Exit code: $exitCode"
Write-Host "    Logfil: $logFile"
Write-Host ""
Write-Host "    Verifiera resultatet i morgon med:"
Write-Host "      `$env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 --name database-url --query value -o tsv)"
Write-Host "      py scripts/check_saft_journal_dups.py"

exit $exitCode
