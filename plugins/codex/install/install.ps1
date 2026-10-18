# Install Varyz, then hand the first run to `varyz setup`: the archive, its
# daemon, your agents and your local data sources, asking
# before each agent and source. Setup asks on a page in the browser, where
# agents and sources are ticked, or in the terminal where no browser can open.
# -Yes answers every one of those questions with yes; -Web keeps to the page
# even without a browser found. `Install Varyz.cmd` carries a copy of this
# file and runs it with -Web: the Windows double-click installer.
# Varyz is installed from the source folder this file sits in (a clone or an
# unpacked download of the repository), or else from GitHub, which needs git
# and, while the repository is private, a git that is signed in to GitHub.
# Usage:
#   .\scripts\install.ps1                  # from this folder
#   .\scripts\install.ps1 -PackageSource <path or git+https://...>
[CmdletBinding()]
param(
    [string]$PackageSource = $(if ($env:VARYZ_PACKAGE_SOURCE) { $env:VARYZ_PACKAGE_SOURCE } else { "" }),
    [string]$ConfigPath = $(if ($env:VARYZ_CONFIG) { $env:VARYZ_CONFIG } elseif ($env:VARYS_CONFIG) { $env:VARYS_CONFIG } else { Join-Path $env:USERPROFILE ".varyz\config.toml" }),
    [switch]$Yes,
    [switch]$Web,
    [switch]$DryRun,
    [string]$ReleaseUpgradeScript = "",
    [string]$ReleaseSha256 = "",
    [string]$ReleaseRecovery = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:VARYZ_BOOTSTRAP_APPROVE -eq "1") { $Yes = $true }
if ($env:VARYZ_BOOTSTRAP_DRY_RUN -eq "1") { $DryRun = $true }
if ($Yes -and $Web) { throw "-Web asks on the page; drop -Yes" }
# Before 2026-09-24 Varyz was called Varys and kept its default archive in
# ~/.varys. Re-running the installer there must not start a second, empty one.
$legacyConfig = Join-Path $env:USERPROFILE ".varys\config.toml"
if (-not $PSBoundParameters.ContainsKey("ConfigPath") -and -not $env:VARYZ_CONFIG -and -not $env:VARYS_CONFIG -and -not (Test-Path -LiteralPath $ConfigPath) -and (Test-Path -LiteralPath $legacyConfig)) {
    $ConfigPath = $legacyConfig
}
$ConfigPath = [System.IO.Path]::GetFullPath($ConfigPath)
$repository = "git+https://github.com/egrm07/varyz"
if (-not $PackageSource) {
    # Run as a file this is scripts\; run from `Install Varyz.cmd` the launcher
    # says where it is, since a script block has no folder of its own.
    $here = if ($PSScriptRoot) { $PSScriptRoot } elseif ($env:VARYZ_LAUNCHER) { Split-Path -Parent $env:VARYZ_LAUNCHER } else { "" }
    $sourceTree = if ($here) { Split-Path -Parent $here } else { "" }
    $pyproject = if ($sourceTree) { Join-Path $sourceTree "pyproject.toml" } else { "" }
    if ($pyproject -and (Test-Path -LiteralPath $pyproject) -and (Select-String -LiteralPath $pyproject -Pattern '^name = "varyzai"$' -Quiet)) {
        $PackageSource = $sourceTree
    } else {
        $PackageSource = $repository
    }
}

Write-Host "Varyz local-first installer"
Write-Host "  package: $PackageSource"
Write-Host "  config:  $ConfigPath"

# With an NVIDIA card the answer-checking model reads each agent question on
# it through DirectML: 121 ms a call against 1.5 s on the processor. The
# DirectML build of onnxruntime is the same module as the plain one, so the
# plain one is left out; uv keeps both choices for `uv tool upgrade`.
$gpu = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
$ortOverride = "onnxruntime ; sys_platform != 'win32'"

if ($DryRun) {
    Write-Host "Dry run; no commands will run and no files will change."
    Write-Host "Would ensure uv is installed."
    if ($ReleaseUpgradeScript) {
        Write-Host "Would back up and upgrade an existing uv installation from this release wheel; verify before committing; roll back on failure. Close Varyz agents and stop its daemon first."
    } else {
        Write-Host "Would run, when Varyz and its config are already installed: varyz update apply --config $ConfigPath"
    }
    if ($gpu) {
        Write-Host "Would run: uv tool install $PackageSource --with onnxruntime-directml --overrides <a file saying: $ortOverride>"
        Write-Host "  (an NVIDIA card was found; the answer-checking model runs on it)"
    } else {
        Write-Host "Would run: uv tool install $PackageSource"
    }
    if ($Yes) {
        Write-Host "Would run: varyz setup --config $ConfigPath --yes"
    } elseif ($Web) {
        Write-Host "Would run: varyz setup --config $ConfigPath --web"
    } else {
        Write-Host "Would run: varyz setup --config $ConfigPath"
    }
    Write-Host "Setup creates the archive, starts its daemon, connects the agents it finds (Claude Code, Codex, Cursor) and offers your local data sources, asking before each unless -Yes. It ends by listing every data source and what it holds."
    return
}

if ($PackageSource.StartsWith("git+") -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Installing Varyz from GitHub needs git. Install git (winget install Git.Git), or run this installer from a downloaded copy of Varyz."
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host "uv was not found; installing it from Astral's official installer..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $uvCandidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe")
    )
    $uvPath = $uvCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $uvPath) {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uv) { throw "uv installed but its executable was not found" }
        $uvPath = $uv.Source
    }
} else {
    $uvPath = $uv.Source
}

$toolBin = (& $uvPath tool dir --bin).Trim()
if ($LASTEXITCODE -ne 0 -or -not $toolBin) { throw "uv did not report its tool bin directory" }
if ($ReleaseUpgradeScript -and $ReleaseRecovery) {
    & $uvPath run --no-project --isolated --python 3.12 $ReleaseUpgradeScript --uv $uvPath --recover $ReleaseRecovery --config $ConfigPath
    if ($LASTEXITCODE -ne 0) { throw "Recovery could not complete; the backup was kept." }
    return
}
function Find-Launcher {
    @((Join-Path $toolBin "varyz.exe"), (Join-Path $toolBin "varyz")) |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

$varyz = Find-Launcher
if ($varyz -and (Test-Path -LiteralPath $ConfigPath)) {
    if ($ReleaseUpgradeScript) {
        & $uvPath run --no-project --isolated --python 3.12 $ReleaseUpgradeScript --uv $uvPath --wheel $PackageSource --sha256 $ReleaseSha256 --config $ConfigPath
        if ($LASTEXITCODE -ne 0) { throw "Release upgrade did not complete. Read the recovery status above." }
        Write-Host "Release upgrade complete. Restart your agents and daemon; existing setup was kept."
        return
    }
    # Installed already (2026-10-02): `uv tool install` only says so. An update
    # backs the database up before a migration, checks the new version starts
    # and refreshes the connected agents; it takes Varyz from where it was
    # installed from, which `varyz update status` names.
    Write-Host "Varyz is installed; updating it from where it was installed."
    & $varyz update apply --config $ConfigPath
    if ($LASTEXITCODE -ne 0) { throw "Could not update Varyz (see above). Close your agents, then run: varyz update apply" }
} else {
    if ($gpu) {
        $overrides = Join-Path ([System.IO.Path]::GetTempPath()) "varyz-install-overrides.txt"
        [System.IO.File]::WriteAllText($overrides, "$ortOverride`n")
        try {
            & $uvPath tool install $PackageSource --with onnxruntime-directml --overrides $overrides
        } finally {
            Remove-Item -LiteralPath $overrides -ErrorAction SilentlyContinue
        }
    } else {
        & $uvPath tool install $PackageSource
    }
    if ($LASTEXITCODE -ne 0) { throw "uv tool install failed" }
    $varyz = Find-Launcher
    if (-not $varyz) { throw "varyz installed but its launcher was not found" }
}

$configDir = Split-Path -Parent $ConfigPath
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

if ($Yes) {
    & $varyz setup --config $ConfigPath --yes
} elseif ($Web) {
    # The questions are on a page in the browser; this window only has to stay open.
    & $varyz setup --config $ConfigPath --web
} else {
    & $varyz setup --config $ConfigPath
}
# 130 is a setup paused in a download; it has said how to carry on.
if ($LASTEXITCODE -eq 130) { exit 130 }
if ($LASTEXITCODE -ne 0) { throw "Setup finished with problems (see above). Re-run it any time: varyz setup --config `"$ConfigPath`"" }
Write-Host "If a new terminal cannot find varyz, run: uv tool update-shell"
