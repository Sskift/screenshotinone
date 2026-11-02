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
        throw "Task '$TaskName' is not managed by screenshotinone. Refusing to remove it."
    }
    $command = $actions[0].SelectSingleNode('t:Command', $namespaces)
    $arguments = $actions[0].SelectSingleNode('t:Arguments', $namespaces)
    $workingDirectory = $actions[0].SelectSingleNode('t:WorkingDirectory', $namespaces)
    if ($actions[0].LocalName -ne 'Exec' -or $null -eq $command -or $null -eq $arguments -or $null -eq $workingDirectory) {
        throw "Task '$TaskName' has an unexpected action. Refusing to remove it."
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
        throw "Task '$TaskName' has an unexpected watcher action. Refusing to remove it."
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
        throw "Task '$TaskName' has inconsistent watcher paths or options. Refusing to remove it."
    }
    foreach ($rootArgument in $rootArguments) {
        $rootPath = ConvertTo-TaskPath $rootArgument.Groups['path'].Value
        if ([IO.Path]::GetFileName($rootPath) -ine 'userdata') {
            throw "Task '$TaskName' watches an unexpected directory. Refusing to remove it."
        }
    }
}

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

Assert-OwnedTask -Task $task

if ($PSCmdlet.ShouldProcess($TaskName, 'Stop and remove screenshotinone startup task')) {
    # Stop only instances belonging to this verified task. Do not kill Python by name.
    # TASK_ENUM_HIDDEN=1 is essential because our task has Hidden=true.
    $running = @($service.GetRunningTasks(1) | Where-Object { $_.Path -eq "\$TaskName" })
    $processes = @($running | ForEach-Object {
        $engine = Get-Process -Id $_.EnginePID -ErrorAction SilentlyContinue
        if ($null -ne $engine) { $null = $engine.Handle; $engine }
    })
    try { $task.Stop(0) } catch {
        if ($_.Exception.GetBaseException().HResult -ne -2147216629) { throw }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $active = @($service.GetRunningTasks(1) | Where-Object { $_.Path -eq "\$TaskName" })
        $alive = @($processes | Where-Object { -not $_.HasExited })
        $state = $taskFolder.GetTask($TaskName).State
        if ($active.Count -eq 0 -and $alive.Count -eq 0 -and $state -ne 4 -and $state -ne 2) {
            break
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "Task '$TaskName' did not stop within 15 seconds. Its definition was not removed."
        }
        Start-Sleep -Milliseconds 200
    } while ($true)
    $taskFolder.DeleteTask($TaskName, 0)
    Write-Output "Removed task '$TaskName'. Configuration and sync history were preserved."
}
