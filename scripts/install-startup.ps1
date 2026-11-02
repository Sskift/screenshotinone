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
    # TASK_CREATE_OR_UPDATE=6; TASK_LOGON_INTERACTIVE_TOKEN=3.
    $registeredTask = $taskFolder.RegisterTask($TaskName, $taskXml, 6, $userSid, $null, 3, $null)
    Assert-OwnedTask -Task $registeredTask
    if ($StartNow) {
        # Apply updated settings immediately, including a changed config path.
        if ($registeredTask.GetInstances(0).Count -gt 0) {
            try { $registeredTask.Stop(0) } catch {
                # An instance can exit between GetInstances and Stop.
                if ($_.Exception.GetBaseException().HResult -ne -2147216629) { throw }
            }
        }
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
