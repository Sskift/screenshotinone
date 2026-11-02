#Requires -Version 5.1
<#
.SYNOPSIS
Stop and remove only the specified screenshotinone startup task.
.EXAMPLE
.\scripts\remove-startup.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidatePattern('^[A-Za-z0-9_.-]{1,128}$')]
    [string]$TaskName = 'ScreenshotInOne'
)

$ErrorActionPreference = 'Stop'
$ownerMarker = 'Managed by screenshotinone (https://github.com/Sskift/screenshotinone).'
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$taskFolder = $service.GetFolder('\')
try {
    $task = $taskFolder.GetTask($TaskName)
} catch {
    if ($_.Exception.GetBaseException().HResult -eq -2147024894) {
        Write-Output "Task '$TaskName' is not installed."
        return
    }
    throw
}

[xml]$definition = $task.Xml
$namespaces = New-Object System.Xml.XmlNamespaceManager($definition.NameTable)
$namespaces.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
$description = $definition.SelectSingleNode('/t:Task/t:RegistrationInfo/t:Description', $namespaces)
$actions = $definition.SelectNodes('/t:Task/t:Actions/*', $namespaces)
if ($null -eq $description -or $description.InnerText -ne $ownerMarker -or $actions.Count -ne 1) {
    throw "Task '$TaskName' is not managed by screenshotinone. Refusing to remove it."
}
$command = $actions[0].SelectSingleNode('t:Command', $namespaces)
$arguments = $actions[0].SelectSingleNode('t:Arguments', $namespaces)
if ($actions[0].LocalName -ne 'Exec' -or $null -eq $command -or $null -eq $arguments -or
    [IO.Path]::GetFileName($command.InnerText) -ine 'pythonw.exe' -or
    $arguments.InnerText -notmatch '^-m screenshotinone --config "[^"]+" watch$') {
    throw "Task '$TaskName' has an unexpected action. Refusing to stop or remove it."
}

if ($PSCmdlet.ShouldProcess($TaskName, 'Stop and remove screenshotinone startup task')) {
    # Stop only instances belonging to this verified task. Do not kill Python by name.
    if ($task.GetInstances(0).Count -gt 0) {
        try { $task.Stop(0) } catch {
            if ($_.Exception.GetBaseException().HResult -ne -2147216629) { throw }
        }
    }
    $taskFolder.DeleteTask($TaskName, 0)
    Write-Output "Removed task '$TaskName'. Configuration and sync history were preserved."
}
