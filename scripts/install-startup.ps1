#Requires -Version 5.1
<#
.SYNOPSIS
Run screenshotinone silently at Windows user logon.
.EXAMPLE
.\scripts\install-startup.ps1 -ConfigPath .\config.json -StartNow
.NOTES
Builds a small native filesystem watcher before replacing the startup task.
The watcher starts pythonw.exe only when synchronization is needed. Uses the
current user's interactive token without a console, elevation, or a password.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [string]$PythonPath,

    [ValidatePattern('^[A-Za-z0-9_.-]{1,128}$')]
    [string]$TaskName = 'ScreenshotInOne',

    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$ownerMarker = 'Managed by screenshotinone (https://github.com/Sskift/screenshotinone).'

function ConvertTo-TaskPath {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value -match '[\x00-\x1f\x7f"]' -or $Value -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+\\)') {
        throw 'Task paths must be absolute Windows paths without quotes or control characters.'
    }
    $fullPath = [IO.Path]::GetFullPath($Value)
    if ($fullPath -ieq [IO.Path]::GetPathRoot($fullPath)) {
        throw 'A task path must identify a file or subdirectory, not a drive or share root.'
    }
    $fullPath.TrimEnd([char[]]'\/')
}

function Assert-OwnedTask {
    param([Parameter(Mandatory = $true)]$Task)

    [xml]$definition = $Task.Xml
    $namespaces = New-Object System.Xml.XmlNamespaceManager($definition.NameTable)
    $namespaces.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $description = $definition.SelectSingleNode('/t:Task/t:RegistrationInfo/t:Description', $namespaces)
    $actions = $definition.SelectNodes('/t:Task/t:Actions/*', $namespaces)
    if ($null -eq $description -or $description.InnerText -ne $ownerMarker -or $actions.Count -ne 1) {
        throw "Task '$TaskName' already exists and is not managed by screenshotinone. Choose another -TaskName."
    }
    $command = $actions[0].SelectSingleNode('t:Command', $namespaces)
    $arguments = $actions[0].SelectSingleNode('t:Arguments', $namespaces)
    $workingDirectory = $actions[0].SelectSingleNode('t:WorkingDirectory', $namespaces)
    if ($actions[0].LocalName -ne 'Exec' -or $null -eq $command -or $null -eq $arguments -or $null -eq $workingDirectory) {
        throw "Task '$TaskName' has an unexpected action. Refusing to replace it."
    }
    $executable = ConvertTo-TaskPath $command.InnerText
    $workingRoot = ConvertTo-TaskPath $workingDirectory.InnerText
    $legacy = [regex]::Match($arguments.InnerText, '^-m screenshotinone --config "(?<config>[^"]+)" watch$')
    if ([IO.Path]::GetFileName($executable) -ieq 'pythonw.exe' -and $legacy.Success) {
        $null = ConvertTo-TaskPath $legacy.Groups['config'].Value
        return
    }
    $native = [regex]::Match($arguments.InnerText, '^--python "(?<python>[^"]+)" --project "(?<project>[^"]+)" --config "(?<config>[^"]+)" --debounce-ms (?<delay>[0-9]+)(?<roots>(?: --root "[^"]+")+)$')
    if ([IO.Path]::GetFileName($executable) -notmatch '^ScreenshotWatcher-[0-9a-f]{16}\.exe$' -or -not $native.Success) {
        throw "Task '$TaskName' has an unexpected watcher action. Refusing to replace it."
    }
    $nativePython = ConvertTo-TaskPath $native.Groups['python'].Value
    $nativeProject = ConvertTo-TaskPath $native.Groups['project'].Value
    $nativeConfig = ConvertTo-TaskPath $native.Groups['config'].Value
    $expectedBin = Join-Path ([IO.Path]::GetDirectoryName($nativeConfig)) 'bin'
    $delay = 0
    $validDelay = [int]::TryParse($native.Groups['delay'].Value, [ref]$delay)
    $rootArguments = [regex]::Matches($native.Groups['roots'].Value, ' --root "(?<path>[^"]+)"')
    if ([IO.Path]::GetFileName($nativePython) -ine 'pythonw.exe' -or $nativeProject -ine $workingRoot -or
        [IO.Path]::GetDirectoryName($executable) -ine $expectedBin -or -not $validDelay -or
        $delay -lt 1 -or $rootArguments.Count -gt 32) {
        throw "Task '$TaskName' has inconsistent watcher paths or options. Refusing to replace it."
    }
    foreach ($rootArgument in $rootArguments) {
        $rootPath = ConvertTo-TaskPath $rootArgument.Groups['path'].Value
        if ([IO.Path]::GetFileName($rootPath) -ine 'userdata') {
            throw "Task '$TaskName' watches an unexpected directory. Refusing to replace it."
        }
    }
}

function ConvertTo-XmlText {
    param([string]$Value)
    [System.Security.SecurityElement]::Escape($Value)
}

function Stop-TaskAndWait {
    param([Parameter(Mandatory = $true)]$Task)

    # GetInstances(0) can omit hidden tasks. Include them explicitly, and retain
    # process handles so a reused process ID cannot confuse the exit check.
    $running = @($service.GetRunningTasks(1) | Where-Object { $_.Path -eq "\$TaskName" })
    $processes = @($running | ForEach-Object {
        $engine = Get-Process -Id $_.EnginePID -ErrorAction SilentlyContinue
        if ($null -ne $engine) { $null = $engine.Handle; $engine }
    })
    try { $Task.Stop(0) } catch {
        if ($_.Exception.GetBaseException().HResult -ne -2147216629) { throw }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $active = @($service.GetRunningTasks(1) | Where-Object { $_.Path -eq "\$TaskName" })
        $alive = @($processes | Where-Object { -not $_.HasExited })
        $state = $taskFolder.GetTask($TaskName).State
        if ($active.Count -eq 0 -and $alive.Count -eq 0 -and $state -ne 4 -and $state -ne 2) {
            return
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Task '$TaskName' did not stop within 15 seconds. Its definition was not replaced."
}

$configItem = Get-Item -LiteralPath $ConfigPath
if ($configItem.PSIsContainer) {
    throw '-ConfigPath must point to a configuration file.'
}
$resolvedConfig = $configItem.FullName
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'screenshotinone\__main__.py') -PathType Leaf)) {
    throw 'Keep this script in the scripts directory of the screenshotinone checkout.'
}
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = (Get-Command python.exe -ErrorAction Stop).Source
}
$pythonItem = Get-Item -LiteralPath $PythonPath
if ($pythonItem.PSIsContainer -or $pythonItem.Name -notin @('python.exe', 'pythonw.exe')) {
    throw '-PythonPath must point to python.exe or pythonw.exe.'
}
$pythonw = Join-Path $pythonItem.DirectoryName 'pythonw.exe'
$pythonConsole = Join-Path $pythonItem.DirectoryName 'python.exe'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf) -or -not (Test-Path -LiteralPath $pythonConsole -PathType Leaf)) {
    throw "Both python.exe and pythonw.exe must exist beside '$PythonPath'. Pass a Windows Python installation."
}
$resolvedConfig = ConvertTo-TaskPath $resolvedConfig
$projectRoot = ConvertTo-TaskPath $projectRoot
$pythonw = ConvertTo-TaskPath $pythonw

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$userSid = $identity.User.Value
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$taskFolder = $service.GetFolder('\')
$existingTask = $null
try {
    $existingTask = $taskFolder.GetTask($TaskName)
} catch {
    # HRESULT 0x80070002 is the scheduler's "task does not exist" response.
    if ($_.Exception.GetBaseException().HResult -ne -2147024894) { throw }
}
if ($null -ne $existingTask) {
    Assert-OwnedTask -Task $existingTask
}

# Resolve and validate the read-only plan before compiling or stopping anything.
Push-Location -LiteralPath $projectRoot
try {
    $planLines = @(& $pythonConsole -m screenshotinone --config $resolvedConfig watch-plan)
    if ($LASTEXITCODE -ne 0) { throw "Could not read the screenshot watcher plan (Python exit $LASTEXITCODE)." }
    $plan = ($planLines -join "`n") | ConvertFrom-Json
} finally {
    Pop-Location
}
if ($plan.enabled -isnot [bool] -or -not $plan.enabled) {
    throw 'No synchronization destination is enabled. Enable a destination before installing startup.'
}
if (($plan.debounce_ms -isnot [int] -and $plan.debounce_ms -isnot [long]) -or
    $plan.debounce_ms -lt 1 -or $plan.debounce_ms -gt [int]::MaxValue -or
    $plan.roots -isnot [System.Array] -or $plan.roots.Count -lt 1 -or $plan.roots.Count -gt 32) {
    throw 'The watcher plan must contain 1-32 userdata roots and a positive Int32 debounce_ms.'
}
$watchRoots = @($plan.roots | ForEach-Object {
    if ($_ -isnot [string]) { throw 'Watcher roots must be absolute paths.' }
    $rootItem = Get-Item -LiteralPath $_
    if (-not $rootItem.PSIsContainer -or $rootItem.Name -ine 'userdata') {
        throw 'Watcher roots must identify existing Steam userdata directories.'
    }
    ConvertTo-TaskPath $rootItem.FullName
} | Select-Object -Unique)
$binDirectory = Join-Path ([IO.Path]::GetDirectoryName($resolvedConfig)) 'bin'
$buildScript = Join-Path $PSScriptRoot 'build-watcher.ps1'
if ($WhatIfPreference) {
    $null = $PSCmdlet.ShouldProcess($TaskName, 'Build and register native screenshotinone watcher at user logon')
    return
}
$built = @(& $buildScript -OutputDirectory $binDirectory)
if ($built.Count -ne 1 -or $built[0] -isnot [string]) { throw 'The watcher build did not return exactly one executable path.' }
$watcher = ConvertTo-TaskPath $built[0]
if ([IO.Path]::GetDirectoryName($watcher) -ine $binDirectory -or
    [IO.Path]::GetFileName($watcher) -notmatch '^ScreenshotWatcher-[0-9a-f]{16}\.exe$' -or
    -not (Test-Path -LiteralPath $watcher -PathType Leaf)) {
    throw 'The watcher build returned an unexpected executable.'
}
# These validated paths contain neither quotes nor trailing separators, so
# Windows command-line quoting is unambiguous and no shell is involved.
$arguments = '--python "' + $pythonw + '" --project "' + $projectRoot + '" --config "' + $resolvedConfig + '" --debounce-ms ' + $plan.debounce_ms
foreach ($watchRoot in $watchRoots) { $arguments += ' --root "' + $watchRoot + '"' }
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>$(ConvertTo-XmlText $identity.Name)</Author>
    <Description>$(ConvertTo-XmlText $ownerMarker)</Description>
    <URI>\$(ConvertTo-XmlText $TaskName)</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$(ConvertTo-XmlText $userSid)</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="CurrentUser">
      <UserId>$(ConvertTo-XmlText $userSid)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="CurrentUser">
    <Exec>
      <Command>$(ConvertTo-XmlText $watcher)</Command>
      <Arguments>$(ConvertTo-XmlText $arguments)</Arguments>
      <WorkingDirectory>$(ConvertTo-XmlText $projectRoot)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

if ($PSCmdlet.ShouldProcess($TaskName, 'Register silent screenshotinone task at user logon')) {
    if ($StartNow -and $null -ne $existingTask) {
        # Stop the old definition before replacement; otherwise IgnoreNew can
        # refuse the new run while the previous process continues unchanged.
        Stop-TaskAndWait -Task $existingTask
    }
    # TASK_CREATE_OR_UPDATE=6; TASK_LOGON_INTERACTIVE_TOKEN=3.
    $registeredTask = $taskFolder.RegisterTask($TaskName, $taskXml, 6, $userSid, $null, 3, $null)
    Assert-OwnedTask -Task $registeredTask
    if ($StartNow) {
        $null = $registeredTask.Run($null)
    }
    [pscustomobject]@{
        TaskName = $TaskName
        ConfigPath = $resolvedConfig
        PythonPath = $pythonw
        WatcherPath = $watcher
        WorkingDirectory = $projectRoot
        WatchRoots = $watchRoots
        DebounceMilliseconds = $plan.debounce_ms
        StartRequested = [bool]$StartNow
    }
}
