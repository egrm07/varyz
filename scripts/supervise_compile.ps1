<#
.SYNOPSIS
    Run the personal-model compile unattended so that a failure costs a restart
    rather than the run.

.DESCRIPTION
    The compile is already resumable: every annotation is cached in SQLite as
    soon as it is validated, the reading plan is deterministic for a given
    archive and budget, and a generation is activated only when it is whole.
    What is missing around it is everything that decides whether the process
    survives eight hours on a laptop -- sleep, a dead model server, a full disk,
    a sync taking the writer lock underneath it, and someone to notice.

    This script supplies that. It pins the machine awake, freezes the archive
    for the duration, holds one absolute annotation deadline across every
    attempt, restarts the compile when it dies, restarts the model server when
    the compile stopped because the server had, stops on its own when the disk
    runs low rather than letting SQLite discover it, and writes a report.

    Nothing here can lose work: each attempt re-reads the cache it filled.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\supervise_compile.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\supervise_compile.ps1 -DryRun

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\supervise_compile.ps1 -Resume
#>
[CmdletBinding()]
param(
    # How long the reading is expected to take. The eight hours themselves are
    # the compiler's own policy: the budget trims how many windows each message
    # keeps until a whole pass fits. This figure only sets when the supervisor
    # gives up, and nothing here shortens the plan.
    [double]$Hours = 8,
    # How far past that a run may go before it is killed. Generous on purpose:
    # after the last annotation the compile is still working -- candidates,
    # synthesis, validation, activation -- and that work is what turns eight
    # hours of reading into a model that can be asked anything.
    [double]$GraceHours = 6,
    # A hard stop on starting new annotations, shared by every attempt so
    # restarts spend the same window rather than a fresh one. Zero, the default,
    # sets none: a run that stops at a deadline is a partial run, and a partial
    # run activates nothing, so a deadline is a cap to reach for deliberately
    # and not something to impose on a run meant to finish.
    [double]$DeadlineHours = 0,
    # Run the eight hours in parts. A part reads this many *new* episodes and
    # stops; cache hits are free and do not count, so every later part starts
    # where the last one stopped rather than paying for it again. A part always
    # ends `partial` and activates nothing -- run the last part with no cap and
    # it finishes the plan and activates. Zero, the default, sets no cap.
    [int]$PartCalls = 0,
    # No sign of movement for this long means the run is no longer moving.
    # Movement is any of four things: a new annotation, a new record, a bigger
    # database, or another line on the attempt's stderr. It used to be the
    # first of those alone, and armed only once a *new* annotation had been
    # bought -- so a resumed run whose annotations were all cached could never
    # arm it at all, and a hang while reading candidate windows, synthesising
    # or staging went unwatched for the whole run. Never armed before the first annotation, because
    # identity and episode reconstruction legitimately take a long time and
    # write nothing to watch.
    [int]$StallMinutes = 45,
    [int]$MaxAttempts = 20,
    [int]$PollSeconds = 60,
    # Pre-run backups to keep, newest first, including the one this run makes.
    # Each is the whole archive -- 3.28 GB on 2026-08-31 -- and nothing was
    # clearing them, so a day of restarts cost more disk than the compile did.
    # Two keeps this run's own backup and the one before it, which is as far
    # back as "undo the last compile" ever reaches.
    [int]$KeepBackups = 2,
    # SQLite discovering a full disk mid-write is the one failure that can cost
    # more than a restart. Stop well above it.
    [double]$MinFreeGB = 6,
    # Free physical memory below which the run says so. Not a kill: by the time
    # this fires the memory is already held by the driver and stopping the run
    # returns none of it. It is there so the log names the cause while the run
    # is still going, rather than the next morning going into why a model that
    # was fast on Tuesday crawls on Wednesday.
    [double]$MinFreeMemoryGB = 3,
    # How far the page file may grow under this run before the attempt is
    # stopped. On 2026-08-31 free space fell from 18.49 GB to 6.81 GB inside a
    # single sixty-second poll with the database unchanged: Windows had grown
    # C:\pagefile.sys by about twelve gigabytes to cover the run's commit
    # charge, and never wrote a page of it. The disk floor caught that after the
    # disk was already gone. The page file is the leading indicator, and it is
    # free to read.
    [double]$MaxPagefileGrowthGB = 8,
    # The one resource the compile actually competes for, and the only one this
    # script never looked at. On 2026-09-02 another project's model host loaded
    # an 8B adapter onto the card at 13:02; the compile's next window timed out
    # at 600 seconds and every window after it came back empty. Two runs were
    # lost that way, five hours between them, and the supervisor called both
    # healthy because the disk, the page file and the commit charge were all
    # fine. A 12B at q4 with six slots of q8_0 cache wants about nine gigabytes;
    # below that the card oversubscribes, Windows pages the weights back over
    # PCIe, and every request times out rather than failing.
    [double]$MinFreeVramGB = 9,
    [switch]$NoBackup,
    # A stop request is durable across supervisor restarts and machine reboots.
    # Clearing it must be an explicit resume action; an ordinary launch (for
    # example from Task Scheduler after a reboot) exits without starting work.
    [switch]$Resume,
    # Runs the whole harness against a --dry-run compile: preflight, sleep pin,
    # pause, loop, report. Costs a minute and proves the plumbing.
    [switch]$DryRun,
    # Passed through to the compile, e.g. -ExtraArgs '--frontier'
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$stopPath = Join-Path $root 'data\compile-run.stop'
$resumedFromStop = $false
if (Test-Path -LiteralPath $stopPath) {
    if (-not $Resume) {
        Write-Host ('{0}  stop sentinel is present at {1}; exiting without starting a compile. Use -Resume to clear it explicitly.' -f ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')), $stopPath)
        return
    }
    Remove-Item -LiteralPath $stopPath -Force -ErrorAction Stop
    $resumedFromStop = $true
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runDir = Join-Path $root "data\runs\compile-$stamp"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$logPath = Join-Path $runDir 'supervisor.log'
$beatPath = Join-Path $runDir 'heartbeat.jsonl'
$statePath = Join-Path $root 'data\compile-run.state.json'
$dbPath = Join-Path $root 'data\varyz.db'
$configPath = Join-Path $root 'config.toml'
$pausedPath = Join-Path $root 'data\autosync\PAUSED'
$leasePath = Join-Path $root 'data\runtime\llama.cpp\managed-server.json'

# Every file this script writes is also read while it runs -- by whoever is
# watching the run, by a monitor, by a tail. On Windows a reader holding the
# handle makes the write fail with a sharing violation, and an unhandled failure
# here terminates the supervisor. That is exactly what happened on 2026-08-30:
# an eight-hour compile died at attempt 1 because its log was being read. A
# supervisor must survive being watched, so writes are retried briefly and then
# abandoned. Losing a heartbeat line is a cosmetic gap in a chart; losing the
# run because of a heartbeat line is eight hours.
function Write-Watched([scriptblock]$Write, [string]$What) {
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ($true) {
        try {
            & $Write
            return $true
        } catch {
            if ([DateTime]::UtcNow -ge $deadline) {
                Write-Host ('({0} write gave up after 5s, run continuing) {1}' -f $What, $_.Exception.Message)
                return $false
            }
            Start-Sleep -Milliseconds 100
        }
    }
}

# Dropping a heartbeat is cosmetic; dropping a log line is not. On
# 2026-08-31 a `tail -f` on this log held its handle for fourteen minutes and
# the two lines that mattered most in the run -- "attempt 4 exited 1" and the
# start of attempt 5 -- were the ones abandoned, so the record shows an attempt
# that never ended and a successor that never began. The run was fine. The
# account of it was wrong, and the account is what gets read afterwards.
#
# So a line that cannot be written is kept and prepended to the next write
# rather than thrown away. Order is preserved, a watched log catches up as soon
# as the reader lets go, and the run still never blocks on being watched:
# everything is in memory and the supervisor moves on either way. Only a
# supervisor killed outright loses what is queued, and that is already a
# separate failure.
$script:pendingLog = New-Object System.Collections.Generic.List[string]

function Write-Log([string]$Message) {
    $line = '{0}  {1}' -f ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')), $Message
    $script:pendingLog.Add($line)
    $batch = $script:pendingLog.ToArray()
    if (Write-Watched { Add-Content -LiteralPath $logPath -Encoding utf8 -Value $batch -ErrorAction Stop } 'log') {
        $script:pendingLog.Clear()
    }
    Write-Host $line
}

function Get-FreeGB {
    $drive = (Get-Item -LiteralPath $root).PSDrive.Name
    return [Math]::Round((Get-PSDrive $drive).Free / 1GB, 2)
}

function Get-PagefileGB {
    # Get-Item on the path throws for a file the kernel keeps open; the
    # directory listing reads its length regardless, and does so unelevated.
    try {
        $file = Get-ChildItem -LiteralPath 'C:\' -Force -File -ErrorAction Stop |
            Where-Object { $_.Name -eq 'pagefile.sys' } | Select-Object -First 1
        if ($null -eq $file) { return $null }
        return [Math]::Round($file.Length / 1GB, 2)
    } catch { return $null }
}

function Get-Commit {
    # Committed bytes against the commit limit. The limit is not fixed: an
    # auto-managed page file raises it by growing on disk, which is precisely
    # the failure this run has to survive, so the ratio alone never looks
    # alarming and the page file size is recorded beside it.
    try {
        $m = Get-CimInstance Win32_PerfRawData_PerfOS_Memory -OperationTimeoutSec 15 -ErrorAction Stop
        return [ordered]@{
            gb = [Math]::Round($m.CommittedBytes / 1GB, 2)
            limit_gb = [Math]::Round($m.CommitLimit / 1GB, 2)
        }
    } catch { return $null }
}

function Get-Memory {
    # Physical memory, and how much of it nothing on the machine can account
    # for. That second figure is the one worth having: pages the display driver
    # pins for unified-memory allocations belong to no process afterwards, so a
    # sum of every working set on the machine cannot see them and neither can
    # Task Manager. On 2026-09-18 a night of phases took this machine from 19.5
    # GB free to 0.6 of 31.1 with 22.8 GB unattributable, and the supervisor --
    # which watched the disk, the page file, the commit charge and the card --
    # had no line that moved. This is that line.
    try {
        $os = Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 15 -ErrorAction Stop
        $totalGb = $os.TotalVisibleMemorySize / 1MB
        $availableGb = $os.FreePhysicalMemory / 1MB
        $counters = Get-Counter -ErrorAction Stop -Counter @(
            '\Process(_Total)\Working Set - Private',
            '\Memory\Pool Nonpaged Bytes',
            '\Memory\Pool Paged Resident Bytes',
            '\Memory\Modified Page List Bytes'
        )
        $claimed = 0
        foreach ($sample in $counters.CounterSamples) { $claimed += $sample.CookedValue / 1GB }
        return [ordered]@{
            total_gb = [Math]::Round($totalGb, 2)
            available_gb = [Math]::Round($availableGb, 2)
            unattributed_gb = [Math]::Round($totalGb - $availableGb - $claimed, 2)
        }
    } catch { return $null }
}

function Get-Gpu {
    # Free memory on the card, in gigabytes, with how busy it is. nvidia-smi is
    # a process spawn and belongs in the slow half of the poll, never among the
    # guards. Per-process memory reads as [Insufficient Permissions] unelevated
    # on this machine, so the totals are what there is to go on -- which is
    # enough: the question is how much room is left, not whose it was.
    try {
        $raw = & nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($raw)) { return $null }
        $parts = ($raw | Select-Object -First 1).Split(',')
        if ($parts.Count -lt 3) { return $null }
        $usedMb = [double]$parts[0].Trim()
        $totalMb = [double]$parts[1].Trim()
        return [ordered]@{
            used_gb = [Math]::Round($usedMb / 1024, 2)
            total_gb = [Math]::Round($totalMb / 1024, 2)
            free_gb = [Math]::Round(($totalMb - $usedMb) / 1024, 2)
            util = [int]$parts[2].Trim()
        }
    } catch { return $null }
}

function Get-TopProcess {
    # Name and private bytes only. A command line names the archive, the config
    # and sometimes a key; a process name names a program, and that is all this
    # needs to say which side of the machine is spending the memory.
    try {
        $top = Get-Process -ErrorAction Stop |
            Sort-Object PrivateMemorySize64 -Descending | Select-Object -First 1
        if ($null -eq $top) { return $null }
        return [ordered]@{
            name = $top.Name
            private_mb = [Math]::Round($top.PrivateMemorySize64 / 1MB)
        }
    } catch { return $null }
}

# --- the machine stays awake -------------------------------------------------
# Set on the calling thread and reasserted every poll, because the flag belongs
# to a thread and a script does not own which one it runs on.
Add-Type -Namespace Varyz -Name Power -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
$ES_CONTINUOUS = [uint32]'0x80000000'
$ES_SYSTEM_REQUIRED = [uint32]'0x00000001'
$ES_AWAYMODE_REQUIRED = [uint32]'0x00000040'
function Set-StayAwake {
    [void][Varyz.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED -bor $ES_AWAYMODE_REQUIRED)
}
function Clear-StayAwake {
    [void][Varyz.Power]::SetThreadExecutionState($ES_CONTINUOUS)
}

Write-Log "run directory: $runDir"
if ($resumedFromStop) {
    Write-Log 'explicit resume cleared the stop sentinel'
}

# --- tools -------------------------------------------------------------------
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($null -ne $uvCommand) {
    $uv = $uvCommand.Source
} else {
    # Task Scheduler and hidden PowerShell launches do not inherit the user's
    # interactive PATH on this machine. uv's standard per-user install remains
    # stable across those launches, so use it before declaring the tool absent.
    $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path -LiteralPath $uv)) {
        throw 'uv is neither on PATH nor at the standard per-user install path'
    }
    Write-Log ('uv was absent from PATH; using {0}' -f $uv)
}
# Every uv call here passes --no-sync, and the run depends on it. `uv run`
# otherwise re-syncs the project before each attempt, and a sync rewrites
# .venv\Scripts\varyz.exe. That file is held open for as long as an MCP
# server is running -- which is exactly whenever an agent has Varyz
# connected, i.e. most of the time this machine is in use. uv cannot replace
# a locked exe, so it fails the attempt with os error 32 before the compile
# starts, and the supervisor retries into the same wall until it exhausts
# its attempts. The project is installed editable, so --no-sync still runs
# the working tree's source; what it skips is the reinstall nobody asked
# for. Observed 2026-08-30: three attempts lost this way in five minutes.
# stdlib only, so the interpreter in the venv is enough and costs no resolve.
$probePython = Join-Path $root '.venv\Scripts\python.exe'
$probeScript = Join-Path $root 'scripts\compile_progress.py'

function Get-CompileProgress {
    # -Full adds the two fields the report needs and the poll loop must not
    # pay for once a minute: which stages reached complete, and what kinds of
    # record the newest generation holds.
    param([switch]$Full)
    $extra = @()
    if ($Full) { $extra = @('--full') }
    try {
        if (Test-Path -LiteralPath $probePython) {
            $raw = & $probePython $probeScript $dbPath @extra
        } else {
            $raw = & $uv 'run' '--no-sync' 'python' $probeScript $dbPath @extra
        }
        return (($raw | Out-String).Trim() | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Reset-LocalModel {
    # A server that died leaves a lease naming a pid; a server that hung leaves
    # one naming a live process that no longer answers, and the runtime refuses
    # to start a second server beside it. Both are cleared the same way.
    Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Log ('killing llama-server pid {0}' -f $_.Id)
        try { $_.Kill() } catch { Write-Log ('could not kill pid {0}: {1}' -f $_.Id, $_.Exception.Message) }
    }
    Start-Sleep -Seconds 3
    Remove-Item -LiteralPath $leasePath -Force -ErrorAction SilentlyContinue
}

function Clear-DeadLease {
    # A run that was killed leaves its lease behind naming a server pid that no
    # longer exists, and the runtime refuses to start a second server beside a
    # lease it can see. The next run then inherits the corpse of the last one.
    #
    # Narrower than Reset-LocalModel on purpose: that kills every llama-server
    # on the machine, which is the right response to *our* server having hung
    # mid-run and the wrong one to run unprompted at start-up, when the server
    # on the card may belong to somebody else's work.
    if (-not (Test-Path -LiteralPath $leasePath)) { return }
    try {
        $lease = Get-Content -LiteralPath $leasePath -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        Write-Log 'the model server lease is unreadable; removing it'
        Remove-Item -LiteralPath $leasePath -Force -ErrorAction SilentlyContinue
        return
    }
    $serverPid = $lease.server_pid
    if ($null -eq $serverPid) { return }
    if (Get-Process -Id $serverPid -ErrorAction SilentlyContinue) { return }
    Write-Log ('the model server lease names pid {0}, which is gone; removing it' -f $serverPid)
    Remove-Item -LiteralPath $leasePath -Force -ErrorAction SilentlyContinue
}

function Stop-Attempt($Process, [string]$Reason) {
    if ($null -eq $Process) { return }
    if ($Process.HasExited) { return }
    Write-Log ('stopping attempt (pid {0}): {1}' -f $Process.Id, $Reason)
    & taskkill.exe '/PID' $Process.Id '/T' '/F' | Out-Null
    try { $Process.WaitForExit(30000) | Out-Null } catch { }
}

# --- preflight ---------------------------------------------------------------
if (-not (Test-Path -LiteralPath $dbPath)) { throw "no archive at $dbPath" }

$freeGB = Get-FreeGB
Write-Log ('free disk: {0} GB; floor {1} GB' -f $freeGB, $MinFreeGB)
$basePagefileGB = Get-PagefileGB
$baseCommit = Get-Commit
if ($null -ne $baseCommit) {
    Write-Log ('commit at start: {0} of {1} GB' -f $baseCommit.gb, $baseCommit.limit_gb)
}
if ($null -eq $basePagefileGB) {
    Write-Log 'page file size could not be read; its growth will not be watched'
} else {
    Write-Log ('page file at start: {0} GB; stop if it grows past {1} GB' -f $basePagefileGB, [Math]::Round($basePagefileGB + $MaxPagefileGrowthGB, 2))
}
$pagefileWarned = $false
if ($freeGB -lt ($MinFreeGB + 1)) {
    throw "only $freeGB GB free; free space before starting an eight-hour write"
}

# Read before the compile starts, so what the card reports is what is left for
# it rather than what it is itself using.
$baseGpu = Get-Gpu
if ($null -eq $baseGpu) {
    Write-Log 'GPU state could not be read; contention for the card will not be watched'
} else {
    Write-Log ('GPU: {0} of {1} GB used, {2} GB free, {3}% busy; floor {4} GB' -f $baseGpu.used_gb, $baseGpu.total_gb, $baseGpu.free_gb, $baseGpu.util, $MinFreeVramGB)
    if ($baseGpu.free_gb -lt $MinFreeVramGB) {
        throw ("only $($baseGpu.free_gb) GB free on the card and the model wants $MinFreeVramGB GB; " +
            'something else is holding it, and a compile that cannot get the GPU spends its hours timing out')
    }
}
$gpuWarned = $false
$memoryWarned = $false
$memoryBaseGb = $null
$memoryHeldBaseGb = $null
# The preflight above asks whether the card has room for our model. Once the
# compile owns the card that same question answers itself wrong: a 12B at six
# slots leaves about 1.7 GB free, so a fixed 9 GB floor fires within three
# minutes of every healthy run -- it did, on 2026-09-02, at 18:13:42 against a
# run that was working -- and a line that fires every time says nothing.
#
# What the line is for is whether something *else* arrived afterwards, and that
# is a drop below where the card settled with our own model resident, not below
# where it sat when the card was empty. So the floor is learned rather than
# configured: the lowest reading of the first ten minutes, less a gigabyte of
# slack for the model's own working set.
$gpuSettleMinutes = 10
$gpuFloorGb = $null
$gpuMinFreeGb = $null
$attemptStart = [DateTime]::UtcNow

Write-Log 'preflight: varyz model compile --dry-run'
$readiness = & $uv 'run' '--no-sync' 'varyz' 'model' 'compile' '--dry-run' '--config' $configPath 2>&1 | Out-String
Write-Log $readiness.Trim()
if ($LASTEXITCODE -ne 0) { throw "compile preflight failed with exit $LASTEXITCODE" }

$claimJson = Join-Path $root 'data\personal-model-compile.lock.json'
if (Test-Path -LiteralPath $claimJson) {
    $claim = $null
    try { $claim = Get-Content -LiteralPath $claimJson -Raw | ConvertFrom-Json } catch { $claim = $null }
    # An unreadable or stale claim is not a holder: the lock itself is an OS
    # byte lock, and the kernel released it when that process died.
    if ($null -ne $claim -and $null -ne $claim.pid) {
        $holder = Get-Process -Id $claim.pid -ErrorAction SilentlyContinue
        # The pid alone is not evidence: Windows reuses them, and refusing to
        # start because an unrelated process inherited one would cost the night.
        if ($null -ne $holder -and ($holder.ProcessName -match 'python|uv')) {
            throw "a compile claimed by pid $($claim.pid) is already running since $($claim.started_at)"
        }
    }
}

$startProgress = Get-CompileProgress
$startAnnotations = 0
if ($null -ne $startProgress -and $null -ne $startProgress.annotations) {
    $startAnnotations = [int]$startProgress.annotations
}
Write-Log ('cached annotations at start: {0}' -f $startAnnotations)

$pauseCreated = $false
$backupPath = $null
$attempt = 0
$outcome = 'unknown'
$detail = ''
$proc = $null
$startedAt = [DateTime]::UtcNow
$hardStop = $startedAt.AddHours($Hours + $GraceHours)
$deadlineIso = ''
if ($DeadlineHours -gt 0) {
    $deadlineIso = $startedAt.AddHours($DeadlineHours).ToString('yyyy-MM-ddTHH:mm:ssZ')
}

try {
    Set-StayAwake
    Write-Log 'sleep and hibernate suppressed for this process'

    # The archive is frozen for the run. Ingestion takes the writer lock the
    # compiler needs and rebuilds the graph the reading plan is derived from:
    # either one arriving mid-run costs the attempt, and a rebuild costs the
    # cache its keys.
    if (Test-Path -LiteralPath $pausedPath) {
        Write-Log 'autosync was already paused; leaving it as found'
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pausedPath) | Out-Null
        Set-Content -LiteralPath $pausedPath -Encoding utf8 -Value ('paused_at = {0}' -f ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')))
        $pauseCreated = $true
        Write-Log 'autosync paused for the duration of this run'
    }

    if (-not $NoBackup -and -not $DryRun) {
        $needGB = [Math]::Round(((Get-Item -LiteralPath $dbPath).Length / 1GB) * 1.1, 2)
        if (((Get-FreeGB) - $needGB) -lt $MinFreeGB) {
            Write-Log ('skipping the pre-run backup: it needs about {0} GB and would breach the {1} GB floor' -f $needGB, $MinFreeGB)
        } else {
            # `varyz backup` refuses a target inside the archive it is copying.
            # Keep safety snapshots at the repository level so the source tree
            # cannot recursively contain its own backup.
            $backupRoot = Join-Path $root '.compile-backups'
            $backupPath = Join-Path $backupRoot "pre-compile-$stamp"
            Write-Log ('backing up the archive to {0} ({1} GB)' -f $backupPath, $needGB)
            & $uv 'run' '--no-sync' 'varyz' 'backup' $backupPath '--config' $configPath | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "pre-run backup failed with exit $LASTEXITCODE" }
            Write-Log 'backup complete'
            # Only after the new one is whole, and only ever inside this
            # directory, matched by the name this script writes.
            if ($KeepBackups -gt 0) {
                $stale = @(Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -like 'pre-compile-*' } |
                    Sort-Object Name -Descending |
                    Select-Object -Skip $KeepBackups)
                foreach ($old in $stale) {
                    $gb = [Math]::Round(((Get-ChildItem -LiteralPath $old.FullName -Recurse -File | Measure-Object Length -Sum).Sum / 1GB), 2)
                    try {
                        Remove-Item -LiteralPath $old.FullName -Recurse -Force -Confirm:$false -ErrorAction Stop
                        Write-Log ('removed superseded backup {0} ({1} GB)' -f $old.Name, $gb)
                    } catch {
                        Write-Log ('could not remove superseded backup {0}: {1}' -f $old.Name, $_.Exception.Message)
                    }
                }
            }
        }
    }

    $deadlineNote = 'none (the compile runs to completion)'
    if ($deadlineIso -ne '') { $deadlineNote = $deadlineIso }
    $partNote = 'no (this run finishes the plan and activates)'
    if ($PartCalls -gt 0) {
        $partNote = ('{0} new annotations, then stop resumably' -f $PartCalls)
    }
    Write-Log ('part cap: {0}' -f $partNote)
    Write-Log ('annotation deadline {0}; supervisor stops at {1}' -f $deadlineNote, $hardStop.ToString('yyyy-MM-ddTHH:mm:ssZ'))
    Clear-DeadLease

    while ($true) {
        if (Test-Path -LiteralPath $stopPath) { $outcome = 'stopped'; $detail = 'stop file present'; break }
        if ([DateTime]::UtcNow -ge $hardStop) { $outcome = 'hard-stop'; $detail = 'grace period reached'; break }
        if ($attempt -ge $MaxAttempts) { $outcome = 'attempts-exhausted'; $detail = "$MaxAttempts attempts"; break }
        $attempt++

        $argList = New-Object System.Collections.Generic.List[string]
        $argList.Add('run'); $argList.Add('--no-sync')
        $argList.Add('varyz'); $argList.Add('model'); $argList.Add('compile')
        if ($DryRun) { $argList.Add('--dry-run') } else { $argList.Add('--run') }
        $argList.Add('--config'); $argList.Add($configPath)
        if (-not $DryRun -and $deadlineIso -ne '') {
            $argList.Add('--observation-deadline')
            $argList.Add($deadlineIso)
        }
        if (-not $DryRun -and $PartCalls -gt 0) {
            $argList.Add('--max-annotation-calls')
            $argList.Add([string]$PartCalls)
        }
        foreach ($extra in $ExtraArgs) { $argList.Add($extra) }

        # The candidate stage writes nothing to the database for tens of
        # minutes, so without this the heartbeat reports the same annotation
        # count for the whole of it and a working run is indistinguishable
        # from a hung one. Set here and nowhere else: an ordinary varyz
        # command stays as quiet as it was.
        $env:VARYZ_LOG_LEVEL = 'INFO'
        $outPath = Join-Path $runDir ('attempt-{0:d2}.out.log' -f $attempt)
        $errPath = Join-Path $runDir ('attempt-{0:d2}.err.log' -f $attempt)
        Write-Log ('attempt {0}: uv {1}' -f $attempt, ($argList -join ' '))
        $proc = Start-Process -FilePath $uv -ArgumentList $argList -WorkingDirectory $root -NoNewWindow -PassThru -RedirectStandardOutput $outPath -RedirectStandardError $errPath
        # Touching the handle keeps it open in this process, which is the only
        # way ExitCode survives the child. Without it every attempt reports an
        # empty exit code and a finished compile reads as a crash.
        $null = $proc.Handle

        $lastAnnotations = $startAnnotations
        $lastChange = [DateTime]::UtcNow
        # Per attempt, because each attempt loads the model again.
        $attemptStart = $lastChange
        $gpuFloorGb = $null
        $gpuMinFreeGb = $null
        $lastSignature = ''
        $killReason = ''
        # Said once per attempt: the activation switch is quiet for a long time,
        # and a line a minute saying so would bury the run's own log.
        $activatingNoted = $false

        while (-not $proc.HasExited) {
            Start-Sleep -Seconds $PollSeconds
            Set-StayAwake
            $now = [DateTime]::UtcNow

            # The guards go first, and out of cheap facts only.
            #
            # They used to sit at the end of this body, behind a Python
            # subprocess and two memory-manager queries. On 2026-08-31 the loop
            # went 19.6 minutes between heartbeats while the machine committed
            # 16 GB and llama-server grew from 6.9 to 15.1 GB. Spawning a process
            # and reading process memory are precisely what slow under that
            # pressure -- so the guards stopped running exactly when the
            # condition they exist to catch was arriving. Nothing below costs
            # more than a directory listing.
            $free = Get-FreeGB
            $pagefileGB = Get-PagefileGB
            if (Test-Path -LiteralPath $stopPath) { $killReason = 'stop-requested'; break }
            if ($free -lt $MinFreeGB) { $killReason = 'low-disk'; break }
            if ($now -ge $hardStop) { $killReason = 'hard-stop'; break }
            if ($null -ne $basePagefileGB -and $null -ne $pagefileGB -and
                    ([Math]::Round($pagefileGB - $basePagefileGB, 2) -ge $MaxPagefileGrowthGB)) {
                $killReason = 'pagefile-growth'; break
            }

            # Everything from here is detail for the heartbeat, and is allowed
            # to be slow or to come back empty without costing a guard.
            $progress = Get-CompileProgress
            $annotations = $lastAnnotations
            if ($null -ne $progress -and $null -ne $progress.annotations) { $annotations = [int]$progress.annotations }
            $lastAnnotations = $annotations
            $records = $null
            $dbGB = $null
            if ($null -ne $progress) {
                $records = $progress.records
                if ($null -ne $progress.db_bytes) { $dbGB = [Math]::Round($progress.db_bytes / 1GB, 2) }
            }
            # Four independent signs of life, because the compile spends most
            # of its hours somewhere no single one of them reaches: reading
            # candidate windows writes only to stderr, staging writes only
            # records, and a fully cached resume never buys an annotation at
            # all. Any one of them changing means the attempt is still working.
            $logBytes = 0
            try { $logBytes = (Get-Item -LiteralPath $errPath -ErrorAction Stop).Length } catch { }
            $dbBytes = ''
            if ($null -ne $progress -and $null -ne $progress.db_bytes) { $dbBytes = $progress.db_bytes }
            $signature = '{0}|{1}|{2}|{3}' -f $annotations, $records, $dbBytes, $logBytes
            if ($signature -ne $lastSignature) {
                $lastSignature = $signature
                $lastChange = $now
            }
            $commit = Get-Commit
            $gpu = Get-Gpu
            if ($null -ne $gpu) {
                if ($null -eq $gpuMinFreeGb -or $gpu.free_gb -lt $gpuMinFreeGb) {
                    $gpuMinFreeGb = $gpu.free_gb
                }
                if ($null -eq $gpuFloorGb -and ($now - $attemptStart).TotalMinutes -ge $gpuSettleMinutes) {
                    $gpuFloorGb = [Math]::Round($gpuMinFreeGb - 1, 2)
                    Write-Log ('GPU settled with our model resident: {0} GB free of {1}; warn below {2} GB' -f $gpuMinFreeGb, $gpu.total_gb, $gpuFloorGb)
                }
            }
            # Warned rather than killed: the compile now stops itself when twelve
            # candidate windows in a row come back empty, which is the layer that
            # can tell a busy card from a broken one. This is the line that says
            # why, in the log the run is read from afterwards.
            if ($null -ne $gpu -and -not $gpuWarned -and $null -ne $gpuFloorGb -and $gpu.free_gb -lt $gpuFloorGb) {
                $gpuWarned = $true
                Write-Log ('GPU room gone: {0} GB free of {1}, {2}% busy, against {3} GB with only our model on the card. Something else has taken it; expect windows to time out.' -f $gpu.free_gb, $gpu.total_gb, $gpu.util, $gpuMinFreeGb)
            }
            $memory = Get-Memory
            if ($null -ne $memory) {
                if ($null -eq $memoryBaseGb) {
                    $memoryBaseGb = $memory.available_gb
                    $memoryHeldBaseGb = $memory.unattributed_gb
                }
                # Warned rather than killed, like the card's floor above: a
                # phase that is paging is still a phase that finishes, and this
                # run's whole value is in finishing. What the line buys is the
                # morning after -- it names the driver before four hours go
                # into blaming the model for being slow.
                if (-not $memoryWarned -and $memory.available_gb -lt $MinFreeMemoryGB) {
                    $memoryWarned = $true
                    $grown = [Math]::Round($memory.unattributed_gb - $memoryHeldBaseGb, 2)
                    Write-Log ('Memory nearly gone: {0} GB free of {1}, from {2} GB at the start. {3} GB is unattributable to any process and has grown by {4} GB under this run -- if that is most of it, the display driver is holding unified-memory pages from servers that were killed rather than stopped, and only a reboot returns them.' -f $memory.available_gb, $memory.total_gb, $memoryBaseGb, $memory.unattributed_gb, $grown)
                }
            }
            $top = Get-TopProcess
            $beat = [ordered]@{
                at = $now.ToString('yyyy-MM-ddTHH:mm:ssZ')
                attempt = $attempt
                annotations = $annotations
                new_annotations = $annotations - $startAnnotations
                records = $records
                free_gb = $free
                db_gb = $dbGB
                pagefile_gb = $pagefileGB
                commit_gb = $(if ($null -ne $commit) { $commit.gb } else { $null })
                commit_limit_gb = $(if ($null -ne $commit) { $commit.limit_gb } else { $null })
                gpu_free_gb = $(if ($null -ne $gpu) { $gpu.free_gb } else { $null })
                gpu_util = $(if ($null -ne $gpu) { $gpu.util } else { $null })
                gpu_floor_gb = $gpuFloorGb
                mem_free_gb = $(if ($null -ne $memory) { $memory.available_gb } else { $null })
                mem_unattributed_gb = $(if ($null -ne $memory) { $memory.unattributed_gb } else { $null })
                top_process = $(if ($null -ne $top) { $top.name } else { $null })
                top_process_mb = $(if ($null -ne $top) { $top.private_mb } else { $null })
                minutes_since_new_annotation = [Math]::Round(($now - $lastChange).TotalMinutes, 1)
            }
            Write-Watched { Add-Content -LiteralPath $beatPath -Encoding utf8 -Value ($beat | ConvertTo-Json -Compress) -ErrorAction Stop } 'heartbeat' | Out-Null
            $state = [ordered]@{
                run = $stamp
                run_dir = $runDir
                attempt = $attempt
                pid = $proc.Id
                started_at = $startedAt.ToString('yyyy-MM-ddTHH:mm:ssZ')
                deadline = $deadlineNote
                part_cap = $PartCalls
                hard_stop = $hardStop.ToString('yyyy-MM-ddTHH:mm:ssZ')
                last = $beat
            }
            Write-Watched { Set-Content -LiteralPath $statePath -Encoding utf8 -Value ($state | ConvertTo-Json -Depth 4) -ErrorAction Stop } 'state' | Out-Null

            # The kill on page-file growth is made above, out of the size alone.
            # This is the warning that goes with it, and it stays here because it
            # names the process spending the memory, which only the detail
            # section has read.
            if ($null -ne $basePagefileGB -and $null -ne $pagefileGB -and -not $pagefileWarned) {
                $grown = [Math]::Round($pagefileGB - $basePagefileGB, 2)
                if ($grown -ge ($MaxPagefileGrowthGB / 4)) {
                    $pagefileWarned = $true
                    Write-Log ('the page file has grown {0} GB (now {1} GB) under {2} at {3} MB private; disk falls with it' -f $grown, $pagefileGB, $(if ($null -ne $top) { $top.name } else { 'an unread process' }), $(if ($null -ne $top) { $top.private_mb } else { 0 }))
                }
            }
            # Stall detection stays behind the probe it reads.
            #
            # Except while the compile is inside the activation switch, which
            # is the one stretch of a run that legitimately writes nothing any
            # of this can see: no annotation, no record, no database growth, no
            # log line. It validates every record in the generation and then
            # rewrites the generation row in a single transaction, and on
            # 2026-09-07 that ran past forty-five minutes twice. Both times this
            # line killed the compile four and a half hours in, at the last
            # step, and threw the whole run away -- the second time from an
            # identical position, because nothing had changed to stop it. A
            # supervisor that kills a run for finishing is worse than no
            # supervisor.
            #
            # Not a longer $StallMinutes: the window has no known upper bound,
            # it grows with the size of the generation, and lengthening the
            # timeout everywhere would also stop catching real hangs in the
            # stages that do write. This waits only where silence is expected,
            # and the hard stop above still bounds it.
            if (($now - $lastChange).TotalMinutes -ge $StallMinutes) {
                if ($null -ne $progress -and $progress.activating) {
                    if (-not $activatingNoted) {
                        $activatingNoted = $true
                        Write-Log ('no movement for {0} minutes, but the generation is inside the activation switch; waiting rather than killing a run that is finishing' -f $StallMinutes)
                    }
                } else {
                    $killReason = 'stalled'
                    break
                }
            }
        }

        if ($killReason -ne '') {
            Stop-Attempt $proc $killReason
            if ($killReason -eq 'stop-requested') { $outcome = 'stopped'; $detail = 'stop file present'; break }
            if ($killReason -eq 'low-disk') { $outcome = 'low-disk'; $detail = "free space fell below $MinFreeGB GB"; break }
            if ($killReason -eq 'pagefile-growth') {
                $outcome = 'pagefile-growth'
                $detail = "Windows grew the page file by $MaxPagefileGrowthGB GB to cover this run's commit charge, and the disk goes with it"
                break
            }
            if ($killReason -eq 'hard-stop') { $outcome = 'hard-stop'; $detail = 'grace period reached'; break }
            Write-Log ('attempt {0} showed no new annotation, record, database growth or log line for {1} minutes; restarting it and the model server' -f $attempt, $StallMinutes)
            Reset-LocalModel
            continue
        }

        try { $proc.WaitForExit() } catch { }
        $code = $proc.ExitCode
        if ($null -eq $code) {
            Write-Log 'the exit code of that attempt could not be read; treating it as a failure'
            $code = -1
        }
        Write-Log ('attempt {0} exited {1}' -f $attempt, $code)
        $tail = Get-Content -LiteralPath $errPath -Tail 20 -ErrorAction SilentlyContinue
        if ($null -ne $tail) { Write-Log ('stderr tail: {0}' -f (($tail | Out-String).Trim())) }

        if ($code -eq 0) {
            # A dry run exits 0 having written nothing, and this line used to
            # report it as 'complete -- the compile activated a generation'.
            # The report it produced was indistinguishable from a real one, in
            # a repository whose whole discipline is that a measurement has to
            # have happened.
            if ($DryRun) { $outcome = 'dry-run'; $detail = 'the preflight ran; no generation was written' }
            else { $outcome = 'complete'; $detail = 'the compile activated a generation' }
            break
        }

        if ($code -eq 3) {
            # Partial is the compiler's own clean stop: progress is stored, no
            # generation is activated, and every annotation paid for is cached.
            # Why it stopped decides whether restarting it would do anything.
            $progress = Get-CompileProgress
            $why = ''
            if ($null -ne $progress -and $null -ne $progress.latest) { $why = [string]$progress.latest.budget_exhausted_by }
            Write-Log ("partial compile; budget exhausted by '{0}'" -f $why)
            if ($why -eq 'deadline' -or $why -eq 'call_budget') {
                $outcome = 'budget-reached'
                $detail = "the annotator stopped at its $why; every annotation is cached, and no generation was activated -- run again to carry on"
                break
            }
            if ($why -eq 'provider_unavailable') {
                Write-Log 'the model server stopped answering; restarting it'
            }
            Reset-LocalModel
            continue
        }

        Reset-LocalModel
        $backoff = [Math]::Min(300, 30 * $attempt)
        Write-Log ('retrying in {0}s' -f $backoff)
        Start-Sleep -Seconds $backoff
    }
} finally {
    Stop-Attempt $proc 'supervisor exiting'
    Clear-StayAwake
    if ($pauseCreated) {
        Remove-Item -LiteralPath $pausedPath -Force -ErrorAction SilentlyContinue
        Write-Log 'autosync resumed'
    }

    $final = Get-CompileProgress -Full
    $endedAt = [DateTime]::UtcNow
    $newAnnotations = 0
    if ($null -ne $final -and $null -ne $final.annotations) { $newAnnotations = [int]$final.annotations - $startAnnotations }
    $reportPath = Join-Path $root ('reports\COMPILE_RUN_{0}.md' -f $stamp)
    $lines = New-Object System.Collections.Generic.List[string]
    $title = "# Personal-model compile run $stamp"
    if ($DryRun) { $title = "# Personal-model compile DRY RUN $stamp" }
    $lines.Add($title)
    $lines.Add('')
    $lines.Add(('Started {0}, ended {1} ({2} h over {3} attempt(s)).' -f $startedAt.ToString('yyyy-MM-ddTHH:mm:ssZ'), $endedAt.ToString('yyyy-MM-ddTHH:mm:ssZ'), [Math]::Round(($endedAt - $startedAt).TotalHours, 2), $attempt))
    $lines.Add('')
    $lines.Add('| | |')
    $lines.Add('|---|---|')
    $lines.Add("| outcome | $outcome |")
    $lines.Add("| detail | $detail |")
    $deadlineRow = 'none'
    if ($deadlineIso -ne '') { $deadlineRow = $deadlineIso }
    $lines.Add("| annotation deadline | $deadlineRow |")
    $partRow = 'none (this run was meant to finish the plan)'
    if ($PartCalls -gt 0) { $partRow = "$PartCalls new annotations" }
    $lines.Add("| part cap | $partRow |")
    $lines.Add("| supervisor hard stop | $($hardStop.ToString('yyyy-MM-ddTHH:mm:ssZ')) |")
    $lines.Add("| cached annotations before | $startAnnotations |")
    if ($null -ne $final) {
        $lines.Add("| cached annotations after | $($final.annotations) |")
        $lines.Add("| new annotations | $newAnnotations |")
        $lines.Add("| personal-model records (all generations) | $($final.records) |")
        $lines.Add("| active generation | $($final.active_generation) |")
        # A compile activates even when its semantic stages went stale, so
        # "completed" and "activated" are both true of a run whose Mirror is
        # empty. These two rows are what tells that apart, and without them
        # the answer needed a query nobody ran.
        if ($null -ne $final.checkpoints) {
            $unfinished = @()
            foreach ($stage in $final.checkpoints.PSObject.Properties) {
                if ($stage.Value -ne 'complete') { $unfinished += "$($stage.Name) ($($stage.Value))" }
            }
            $stageRow = 'none'
            # No checkpoints at all is not the same answer as every stage
            # complete, and the row must not read like the good one.
            if ($final.checkpoints.PSObject.Properties.Name.Count -eq 0) { $stageRow = 'no checkpoints recorded' }
            if ($unfinished.Count -gt 0) { $stageRow = ($unfinished -join ', ') }
            $lines.Add("| stages not complete | $stageRow |")
        }
        # The third way a run reads "completed" and means less than it says: a
        # stage can reach complete having run with no semantic model behind it,
        # which no stage state shows.
        if ($null -ne $final.stale_stages) {
            # Wrapped, because a one-element JSON array does not always
            # survive ConvertFrom-Json as an array, and a single stale stage
            # rendering as "none" is the one reading this row exists to catch.
            $stale = @($final.stale_stages)
            $staleRow = 'none'
            if ($stale.Count -gt 0) { $staleRow = ($stale -join ', ') }
            $lines.Add("| stages that ran without the semantic model | $staleRow |")
        }
        if ($null -ne $final.record_kinds) {
            $kinds = @()
            foreach ($kind in $final.record_kinds.PSObject.Properties) {
                $kinds += "$($kind.Name) $($kind.Value)"
            }
            $kindRow = 'none'
            if ($kinds.Count -gt 0) { $kindRow = ($kinds -join ', ') }
            $lines.Add("| newest generation $($final.newest_generation) holds | $kindRow |")
        }
        if ($null -ne $final.latest) {
            $lines.Add("| episodes annotated | $($final.latest.episodes_annotated) of $($final.latest.episodes_total) |")
            $lines.Add("| last state | $($final.latest.state) (budget_exhausted_by=$($final.latest.budget_exhausted_by)) |")
            # `provider_unavailable` next to a guard outcome reads as a flaky
            # local model, and on a killed attempt it is nothing of the kind:
            # Stop-Attempt is `taskkill /T`, which takes the whole tree down
            # including the llama.cpp server the compile was mid-request to.
            # Three reports on 2026-09-05 carry that pair, and the stderr they
            # point at says only `server pid ... exited or was restarted while
            # inference was in flight` -- true, and about this supervisor.
            $killed = @('low-disk', 'pagefile-growth', 'hard-stop', 'stopped')
            if ($final.latest.budget_exhausted_by -eq 'provider_unavailable' -and $killed -contains $outcome) {
                $lines.Add("| why the provider went away | this supervisor stopped the attempt ($outcome); the kill takes the model server with it |")
            }
        }
    }
    $lines.Add("| free disk | $(Get-FreeGB) GB |")
    $endGpu = Get-Gpu
    if ($null -ne $endGpu) { $lines.Add("| free GPU | $($endGpu.free_gb) of $($endGpu.total_gb) GB, $($endGpu.util)% busy |") }
    $endPagefileGB = Get-PagefileGB
    if ($null -ne $basePagefileGB -and $null -ne $endPagefileGB) {
        $lines.Add("| page file | $basePagefileGB GB at start, $endPagefileGB GB at end |")
    }
    # Relative to the repository, because these reports are committed and an
    # absolute path here publishes whoever's home directory ran the compile.
    # Written by hand rather than with [IO.Path]::GetRelativePath, which is
    # .NET Core only: on Windows PowerShell 5.1 it throws MethodNotFound, and
    # it threw here -- in the report the supervisor writes when it stops an
    # attempt -- so a low-disk stop killed the supervisor instead of pausing
    # it, and the run stayed dead for hours with nothing left watching it.
    $rel = {
        param($p)
        if (-not $p) { return $p }
        $full = [IO.Path]::GetFullPath($p)
        $base = [IO.Path]::GetFullPath($root)
        if (-not $base.EndsWith('')) { $base = $base + '' }
        if ($full.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) {
            return $full.Substring($base.Length)
        }
        return $full
    }
    if ($null -ne $backupPath) { $lines.Add("| pre-run backup | $(& $rel $backupPath) |") }
    $lines.Add("| logs | $(& $rel $runDir) |")
    $lines.Add('')
    $lines.Add('Every annotation above is cached and keyed by its own inputs, so a')
    $lines.Add('further run resumes from it rather than paying for it again.')
    New-Item -ItemType Directory -Force -Path (Join-Path $root 'reports') | Out-Null
    Set-Content -LiteralPath $reportPath -Encoding utf8 -Value ($lines -join [Environment]::NewLine)
    Write-Log ('outcome: {0} ({1}); {2} new annotations; report {3}' -f $outcome, $detail, $newAnnotations, $reportPath)
}

if ($outcome -eq 'complete' -or $outcome -eq 'budget-reached' -or $outcome -eq 'dry-run') { exit 0 }
exit 1
