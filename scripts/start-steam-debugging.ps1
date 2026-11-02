#Requires -Version 5.1
<#
.SYNOPSIS
Start Steam with its local Chromium debugging interface enabled.
.EXAMPLE
.\scripts\start-steam-debugging.ps1
.EXAMPLE
.\scripts\start-steam-debugging.ps1 -SteamPath 'D:\Steam' -Restart
.NOTES
Uses hidden process launches and Steam's -silent option. Steam controls its own
windows and may still show UI. Run only after the user authorizes starting Steam
or a brief restart. -Restart is required to shut down an existing Steam client.
No registry values or shortcuts are changed, and no process is forcibly killed.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SteamPath,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

function Resolve-SteamExecutable {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.PSIsContainer) {
        $item = Get-Item -LiteralPath (Join-Path $item.FullName 'steam.exe') -ErrorAction Stop
    }
    if ($item.PSIsContainer -or $item.Name -ine 'steam.exe') {
        throw '-SteamPath must identify steam.exe or its installation directory.'
    }
    return $item.FullName
}

function Wait-DebugEndpoint {
    # HttpClient enforces cancellation across the complete buffered response.
    # Disabling both proxies and redirects keeps every probe on numeric loopback.
    Add-Type -AssemblyName System.Net.Http
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [Threading.Timeout]::InfiniteTimeSpan
    $client.MaxResponseContentBufferSize = 65536
    $timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        while ($timer.Elapsed.TotalSeconds -lt 15) {
            $remaining = [Math]::Max(1, [int](15000 - $timer.Elapsed.TotalMilliseconds))
            $cancellation = [Threading.CancellationTokenSource]::new([Math]::Min(1000, $remaining))
            $response = $null
            try {
                $pending = $client.GetAsync('http://127.0.0.1:8080/json/version', $cancellation.Token)
                $response = $pending.GetAwaiter().GetResult()
                if ([int]$response.StatusCode -eq 200) {
                    $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                    $version = $body | ConvertFrom-Json -ErrorAction Stop
                    if ($version.Browser -and $version.'Protocol-Version') {
                        return $true
                    }
                }
            } catch {
                # Do not echo server bodies, URLs, or exception details.
            } finally {
                if ($null -ne $response) { $response.Dispose() }
                $cancellation.Dispose()
            }
            $remaining = [int](15000 - $timer.Elapsed.TotalMilliseconds)
            if ($remaining -gt 0) {
                Start-Sleep -Milliseconds ([Math]::Min(200, $remaining))
            }
        }
        return $false
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

# Inspect ownership before deciding which Steam installation may be restarted.
$ownedProcesses = @()
foreach ($candidate in @(Get-CimInstance Win32_Process -Filter "Name = 'steam.exe'")) {
    $owner = Invoke-CimMethod -InputObject $candidate -MethodName GetOwnerSid
    if ($owner.ReturnValue -ne 0) {
        throw 'Cannot verify the owner of a running Steam process. No changes were made.'
    }
    if ($owner.Sid -eq $currentSid) {
        if ([string]::IsNullOrWhiteSpace($candidate.ExecutablePath)) {
            throw 'Cannot verify the executable path of your running Steam client. No changes were made.'
        }
        $ownedProcesses += $candidate
    }
}

if (-not [string]::IsNullOrWhiteSpace($SteamPath)) {
    $steamExe = Resolve-SteamExecutable -Path $SteamPath
} elseif ($ownedProcesses.Count -gt 0) {
    $runningPaths = @($ownedProcesses | ForEach-Object {
        Resolve-SteamExecutable -Path $_.ExecutablePath
    } | Select-Object -Unique)
    if ($runningPaths.Count -ne 1) {
        throw 'More than one Steam installation is running for your account. Close the extra client before continuing.'
    }
    $steamExe = $runningPaths[0]
} else {
    $candidates = @()
    foreach ($location in @(
        @{Key = 'HKCU:\Software\Valve\Steam'; Value = 'SteamPath'},
        @{Key = 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam'; Value = 'InstallPath'},
        @{Key = 'HKLM:\SOFTWARE\Valve\Steam'; Value = 'InstallPath'}
    )) {
        $settings = Get-ItemProperty -LiteralPath $location.Key -ErrorAction SilentlyContinue
        if ($null -ne $settings -and $settings.($location.Value)) {
            $candidates += $settings.($location.Value)
        }
    }
    foreach ($programRoot in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
        if (-not [string]::IsNullOrWhiteSpace($programRoot)) {
            $candidates += Join-Path $programRoot 'Steam'
        }
    }
    $steamExe = $null
    foreach ($candidate in $candidates) {
        try {
            $steamExe = Resolve-SteamExecutable -Path $candidate
            break
        } catch {
            # Missing historical registry paths may be followed by a valid install.
        }
    }
    if ($null -eq $steamExe) {
        throw 'Steam was not found. Pass -SteamPath with steam.exe or its installation directory.'
    }
}

foreach ($process in $ownedProcesses) {
    if ((Resolve-SteamExecutable -Path $process.ExecutablePath) -ine $steamExe) {
        throw 'The selected Steam installation differs from your running client. No changes were made.'
    }
}

$debugAlreadyEnabled = $ownedProcesses.Count -gt 0 -and @($ownedProcesses | Where-Object {
    $_.CommandLine -notmatch '(?:^|\s)"?-cef-enable-debugging"?(?=\s|$)'
}).Count -eq 0

if ($debugAlreadyEnabled) {
    $ready = Wait-DebugEndpoint
    [pscustomobject]@{
        SteamPath = $steamExe
        AlreadyRunning = $true
        DebugFlagPresent = $true
        DebugEndpointResponding = $ready
        Endpoint = 'http://127.0.0.1:8080'
    }
    if (-not $ready) {
        Write-Warning 'Steam has the debugging flag, but its local CDP endpoint did not respond within 15 seconds.'
    }
    return
}

if ($ownedProcesses.Count -gt 0 -and -not $Restart) {
    throw 'Steam is running without -cef-enable-debugging. Re-run with -Restart after authorizing a brief Steam restart, or exit Steam yourself first. No changes were made.'
}

$action = if ($ownedProcesses.Count -gt 0) { 'Gracefully restart Steam with local debugging enabled' } else { 'Start Steam with local debugging enabled' }
if (-not $PSCmdlet.ShouldProcess($steamExe, $action)) { return }

Write-Warning 'Steam may display its own window despite the hidden launch and -silent option.'
if ($ownedProcesses.Count -gt 0) {
    $originalProcesses = @($ownedProcesses | ForEach-Object {
        $process = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            # Open a handle before shutdown so a reused PID cannot change identity.
            $null = $process.Handle
            $process
        }
    })
    $shutdown = Start-Process -FilePath $steamExe -ArgumentList '-shutdown' -WindowStyle Hidden -PassThru
    $stopTimer = [Diagnostics.Stopwatch]::StartNew()
    while (@($originalProcesses | Where-Object { -not $_.HasExited }).Count -gt 0 -or -not $shutdown.HasExited) {
        if ($stopTimer.Elapsed.TotalSeconds -ge 45) {
            throw 'Steam did not finish its graceful shutdown within 45 seconds. No process was forcibly stopped; finish exiting Steam before trying again.'
        }
        Start-Sleep -Milliseconds 200
    }
}

$null = Start-Process -FilePath $steamExe -ArgumentList '-silent', '-cef-enable-debugging' -WindowStyle Hidden -PassThru
$ready = Wait-DebugEndpoint
[pscustomobject]@{
    SteamPath = $steamExe
    AlreadyRunning = $false
    DebugFlagPresent = $null
    DebugEndpointResponding = $ready
    Endpoint = 'http://127.0.0.1:8080'
}
if (-not $ready) {
    Write-Warning 'Steam was launched with debugging requested, but its local CDP endpoint did not respond within 15 seconds.'
}
