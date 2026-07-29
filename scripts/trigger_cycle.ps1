param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('morning', 'intraday', 'evening', 'scoring')]
    [string]$RunType
)

# Reliable external trigger for the Quant-Terminal GitHub Actions workflow.
# Registered as Windows Scheduled Tasks to replace GitHub's unreliable cron.
# Fires `workflow_dispatch` with the correct run_type and logs the result.

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

function Write-TriggerLog {
    param([string]$Line)
    # Add-Content opens the file exclusively, so a sibling instance writing at
    # the same moment throws "file in use". Retry instead of losing the line:
    # on 7/27 a lost retry mislabeled a successful dispatch FAILED, and on 7/28
    # a successful dispatch left no line at all.
    for ($i = 0; $i -lt 10; $i++) {
        try { Add-Content -Path $log -Value $Line -ErrorAction Stop; return }
        catch { Start-Sleep -Milliseconds (100 * ($i + 1)) }
    }
}

# Missed-start catch-up (StartWhenAvailable) fires every missed QT task in the
# same second after a PC wake, producing same-second workflow_dispatch pairs on
# GitHub. Serialize instances so concurrent triggers dispatch and log cleanly.
$mutex = New-Object System.Threading.Mutex($false, 'Global\QuantTerminal-TriggerCycle')
$owned = $false
try { $owned = $mutex.WaitOne([TimeSpan]::FromMinutes(2)) }
catch [System.Threading.AbandonedMutexException] { $owned = $true }  # prior holder died; ownership transferred

try {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss') + ' UTC'
    try {
        $out = & $gh workflow run quant_daily.yml --repo $repo -f run_type=$RunType 2>&1
        Write-TriggerLog "$stamp  dispatch run_type=$RunType  OK  $out"
        exit 0
    }
    catch {
        Write-TriggerLog "$stamp  dispatch run_type=$RunType  FAILED  $($_.Exception.Message)"
        exit 1
    }
}
finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
