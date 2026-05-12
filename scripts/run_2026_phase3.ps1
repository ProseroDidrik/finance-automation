# Fas 3: process + load + verify för 4 perioder.
# Loggar varje steg till _logs\2026_phase3.log med tydliga STEP-markörer
# så Monitor kan fånga progress.

$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'
$env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 --name database-url --query value -o tsv)

$log = "_logs\2026_phase3.log"
"" | Out-File -FilePath $log -Encoding utf8
$periods = @("202601","202602","202603","202604")

function Log-Step($tag) {
    $ts = Get-Date -Format "HH:mm:ss"
    "STEP $ts $tag" | Tee-Object -FilePath $log -Append
}

function Run-Step($tag, $exe, $arglist) {
    Log-Step "BEGIN $tag"
    $out = & $exe @arglist 2>&1
    $out | Out-File -FilePath $log -Append -Encoding utf8
    # Ta sista DONE-rad om det finns, annars sista raden
    $done = $out | Where-Object { $_ -match '^\[DONE\]' } | Select-Object -Last 1
    if (-not $done) { $done = $out | Select-Object -Last 1 }
    Log-Step "END   $tag :: $done"
}

# --- Process per period ---
foreach ($p in $periods) {
    Run-Step "run_all $p" "py" @("run_all.py","--period",$p)
}

# --- Load per period ---
foreach ($p in $periods) {
    Run-Step "load_inl  $p" "py" @("load_inl.py","--period",$p)
    Run-Step "load_sie  $p" "py" @("load_sie.py","--period",$p)
    Run-Step "load_saft $p" "py" @("load_saft.py","--period",$p)
}

# --- Verify facit (bara 202602/03/04) ---
foreach ($p in @("202602","202603","202604")) {
    Run-Step "verify_facit $p" "py" @("verify_facit.py","--period",$p)
}

Log-Step "PHASE3_DONE"
