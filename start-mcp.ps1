param(
    [switch]$Restart  # Stoppa ev. befintlig MCP-server och starta om från noll
)

$ErrorActionPreference = "Stop"
$root      = $PSScriptRoot
$logDir    = Join-Path $root "_logs"
$mcpOut    = Join-Path $logDir "mcp_http.log"
$mcpErr    = Join-Path $logDir "mcp_http.err"
$cfOut     = Join-Path $logDir "cloudflared.log"
$cfErr     = Join-Path $logDir "cloudflared.err"
$tokenPath = Join-Path $root ".mcp_token"
$py        = Join-Path $root ".venv\Scripts\python.exe"
$dbPath    = Join-Path $root "data\finance.duckdb"

if (-not (Test-Path $py))        { throw "venv saknas: $py" }
if (-not (Test-Path $tokenPath)) { throw "Token saknas: $tokenPath  (generera: py -c `"import secrets; print(secrets.token_urlsafe(32))`" > .mcp_token)" }
if (-not (Test-Path $dbPath))    { throw "Warehouse saknas: $dbPath" }

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 1) Cloudflared: en quick tunnel-URL går inte att återanvända, så vi stoppar alltid ev. befintlig.
$cfOld = Get-Process cloudflared -ErrorAction SilentlyContinue
if ($cfOld) {
    Write-Host "[stop] Avslutar befintlig cloudflared (PID $($cfOld.Id -join ', '))"
    $cfOld | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

# 2) MCP HTTP-server: återanvänd om den redan lyssnar (om inte -Restart).
$listening = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($listening -and $Restart) {
    $owner = $listening.OwningProcess | Select-Object -First 1
    Write-Host "[stop] Avslutar befintlig MCP-server (PID $owner)"
    Stop-Process -Id $owner -Force
    Start-Sleep -Milliseconds 500
    $listening = $null
}

if ($listening) {
    $mcpPid = ($listening.OwningProcess | Select-Object -First 1)
    Write-Host "[skip] MCP-server kör redan (PID $mcpPid)"
} else {
    "" | Out-File $mcpOut -Encoding utf8
    "" | Out-File $mcpErr -Encoding utf8
    $proc = Start-Process -FilePath $py -ArgumentList "mcp_server.py","--http" `
        -WorkingDirectory $root `
        -RedirectStandardOutput $mcpOut -RedirectStandardError $mcpErr `
        -WindowStyle Hidden -PassThru
    $mcpPid = $proc.Id
    Write-Host "[ok]   MCP-server startad (PID $mcpPid) - vantar pa port 8765..."

    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 400
        $up = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    } while (-not $up -and (Get-Date) -lt $deadline)
    if (-not $up) { throw "MCP-server startade aldrig pa 8765 - kolla $mcpErr" }
}

# 3) Starta cloudflared quick tunnel.
"" | Out-File $cfOut -Encoding utf8
"" | Out-File $cfErr -Encoding utf8
$cf = Start-Process -FilePath "cloudflared" `
    -ArgumentList "tunnel","--url","http://127.0.0.1:8765" `
    -WorkingDirectory $root `
    -RedirectStandardOutput $cfOut -RedirectStandardError $cfErr `
    -WindowStyle Hidden -PassThru
Write-Host "[ok]   cloudflared startad (PID $($cf.Id)) - vantar pa publik URL..."

# 4) Plocka URL:en ur loggen (cloudflared skriver INF-rader till stderr).
$url = $null
$deadline = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $deadline -and -not $url) {
    Start-Sleep -Milliseconds 400
    $blob = ""
    if (Test-Path $cfErr) { $blob += (Get-Content $cfErr -Raw) }
    if (Test-Path $cfOut) { $blob += (Get-Content $cfOut -Raw) }
    if ($blob -match "(https://[a-z0-9-]+\.trycloudflare\.com)") {
        $url = $matches[1]
    }
}
if (-not $url) { throw "Hittade ingen trycloudflare-URL i loggen - kolla $cfErr" }

$token = (Get-Content $tokenPath -Raw).Trim()
$fullUrl = "$url/mcp?token=$token"
Set-Clipboard -Value $fullUrl

Write-Host ""
Write-Host "=========================================="
Write-Host " MCP via Cloudflare quick tunnel"
Write-Host "=========================================="
Write-Host " $fullUrl"
Write-Host "------------------------------------------"
Write-Host " (URL:en innehaller token - kopierad till urklipp)"
Write-Host " MCP-PID: $mcpPid    cloudflared-PID: $($cf.Id)"
Write-Host " Stopp:   Stop-Process -Id $($cf.Id), $mcpPid"
Write-Host "=========================================="
