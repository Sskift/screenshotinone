#Requires -Version 5.1
<#
.SYNOPSIS
Run screenshotinone silently at Windows user logon.
.EXAMPLE
.\scripts\install-startup.ps1 -ConfigPath .\config.json -StartNow
.NOTES
Uses the current user's interactive token and pythonw.exe. It does not request
administrator privileges, create a console, or store a Windows password.
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
    if ($actions[0].LocalName -ne 'Exec' -or $null -eq $command -or $null -eq $arguments -or
        [IO.Path]::GetFileName($command.InnerText) -ine 'pythonw.exe' -or
        $arguments.InnerText -notmatch '^-m screenshotinone --config "[^"]+" watch$') {
        throw "Task '$TaskName' has an unexpected action. Refusing to replace it."
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
if ($pythonItem.PSIsContainer) {
    throw '-PythonPath must point to a Python executable.'
}
$pythonw = Join-Path $pythonItem.DirectoryName 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "pythonw.exe was not found beside '$PythonPath'. Pass -PythonPath for a Windows Python installation."
}

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

$arguments = '-m screenshotinone --config "' + $resolvedConfig + '" watch'
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
      <Command>$(ConvertTo-XmlText $pythonw)</Command>
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
        WorkingDirectory = $projectRoot
        StartRequested = [bool]$StartNow
    }
}
