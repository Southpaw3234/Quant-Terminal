# One-shot dispatcher for the Phase 1 GPU validation run on feat/maximize-model.
# Registered as a one-time Windows Scheduled Task (QT-GPU-Validation) to fire
# after the evening cycle, so the long GPU run never blocks live market-hours
# cycles via the workflow's shared concurrency group.

$ErrorActionPreference = 'Stop'
$repo = 'Southpaw3234/Quant-Terminal'
$gh = 'C:\Program Files\GitHub CLI\gh.exe'

# Scheduled tasks can't read gh's keyring auth (session-bound), so load the
# DPAPI-encrypted token (decryptable only by this Windows account) into GH_TOKEN.
$tokFile = Join-Path $PSScriptRoot '.gh_token.dpapi'
if (Test-Path $tokFile) {
    $sec = Get-Content $tokFile | ConvertTo-SecureString
    $env:GH_TOKEN = [System.Net.NetworkCredential]::new('', $sec).Password
}

$logDir = Join-Path $PSScriptRoot '..\run_logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir 'trigger.log'
$stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss') + ' UTC'

try {
    $out = & $gh workflow run quant_daily.yml --repo $repo --ref feat/maximize-model -f run_type=morning -f use_gpu=true 2>&1
    Add-Content -Path $log -Value "$stamp  dispatch GPU-VALIDATION (feat/maximize-model, use_gpu=true)  OK  $out"
    exit 0
}
catch {
    Add-Content -Path $log -Value "$stamp  dispatch GPU-VALIDATION FAILED  $($_.Exception.Message)"
    exit 1
}
