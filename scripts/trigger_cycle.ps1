param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('morning', 'intraday', 'evening', 'scoring')]
    [string]$RunType
)

# Reliable external trigger for the Quant-Terminal GitHub Actions workflow.
# Registered as Windows Scheduled Tasks (see scripts\register_triggers.ps1) to
# replace GitHub's unreliable cron. Fires `workflow_dispatch` with the correct
# run_type and logs the result.

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
    $out = & $gh workflow run quant_daily.yml --repo $repo -f run_type=$RunType 2>&1
    Add-Content -Path $log -Value "$stamp  dispatch run_type=$RunType  OK  $out"
    exit 0
}
catch {
    Add-Content -Path $log -Value "$stamp  dispatch run_type=$RunType  FAILED  $($_.Exception.Message)"
    exit 1
}
