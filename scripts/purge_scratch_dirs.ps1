# Removes the abandoned test scratch directories left in the repo root by the
# fail-closed test suites (egress / authoring), which made scratch archives
# unreadable to prove Varyz refuses rather than degrades, and never restored the
# ACL in a finally. No current test creates these, so they will not come back.
#
# Requires an ELEVATED shell: the owner's own account cannot read, list, or
# delete them, and takeown/icacls are refused without elevation.

$ErrorActionPreference = 'Continue'

# Refuse to run unelevated: takeown/icacls fail instantly and silently, which
# looks like a completed purge in the log but removes nothing.
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "NOT ELEVATED - this shell has no admin token, so nothing would be deleted. Right-click PowerShell > 'Run as administrator', then run this script again."
    exit 2
}

$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo
$log = Join-Path $repo 'scripts\purge_scratch_dirs.log'
"=== purge started $(Get-Date -Format o) in $repo ===" | Set-Content -Encoding utf8 $log

$patterns = @('.test-tmp-*', '.pytest-tmp-*', '.tmp_public_eval_tests_*',
              'UsersegorAppDataLocalTempvaryz-nightly-prune-*')

function Get-Targets {
    Get-ChildItem -Force -Directory | Where-Object {
        $n = $_.Name
        ($patterns | Where-Object { $n -like $_ }).Count -gt 0
    }
}

$targets = @(Get-Targets)
"found $($targets.Count) directories" | Add-Content -Encoding utf8 $log

foreach ($t in $targets) {
    takeown /F $t.FullName /R /D Y *>$null
    icacls  $t.FullName /reset /T /C /Q *>$null
    Remove-Item -LiteralPath $t.FullName -Recurse -Force -ErrorAction SilentlyContinue
    $state = if (Test-Path -LiteralPath $t.FullName) { 'FAILED ' } else { 'removed' }
    "$state $($t.Name)" | Add-Content -Encoding utf8 $log
}

$left = @(Get-Targets).Count
$msg = "=== done $(Get-Date -Format o): $($targets.Count - $left) removed, $left remaining ==="
$msg | Add-Content -Encoding utf8 $log
Write-Output $msg
