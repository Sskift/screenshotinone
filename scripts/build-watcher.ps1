#Requires -Version 5.1
<#
.SYNOPSIS
Build the native Windows screenshotinone watcher without opening a window.
.EXAMPLE
$watcher = .\scripts\build-watcher.ps1
.NOTES
Writes only the absolute executable path to the success pipeline. The filename
hashes the source and build profile so a running executable is never overwritten.
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [string]$CompilerPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$sourcePath = Join-Path $projectRoot 'windows\ScreenshotWatcher.cs'
if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "Watcher source was not found at '$sourcePath'. Keep this script in the project scripts directory."
}

if ([string]::IsNullOrWhiteSpace($CompilerPath)) {
    foreach ($candidate in @(
        'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe',
        'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe'
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $CompilerPath = $candidate
            break
        }
    }
}
if ([string]::IsNullOrWhiteSpace($CompilerPath)) {
    throw 'The .NET Framework C# compiler was not found. Pass -CompilerPath with the full path to csc.exe.'
}
$compiler = Get-Item -LiteralPath $CompilerPath
if ($compiler.PSIsContainer -or $compiler.Name -ine 'csc.exe') {
    throw '-CompilerPath must identify the C# compiler csc.exe.'
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $projectRoot 'build\watcher'
}
$outputRoot = [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory))
$null = [IO.Directory]::CreateDirectory($outputRoot)

# Hash and compile the same snapshot even if the source is edited during a build.
$sourceBytes = [IO.File]::ReadAllBytes($sourcePath)
# The 32-bit CLR uses less private memory for this small resident relay. Python
# workers retain their own architecture. Include the profile to avoid reusing
# an earlier AnyCPU executable built from identical source.
$profileBytes = [Text.Encoding]::ASCII.GetBytes("screenshotinone-watcher-v1|netfx4|winexe|optimize|x86`n")
[byte[]]$buildIdentity = $profileBytes + $sourceBytes
$hasher = [Security.Cryptography.SHA256]::Create()
try {
    $digest = [BitConverter]::ToString($hasher.ComputeHash($buildIdentity)).Replace('-', '').ToLowerInvariant()
} finally {
    $hasher.Dispose()
}
$executablePath = Join-Path $outputRoot ('ScreenshotWatcher-' + $digest.Substring(0, 16) + '.exe')
if (Test-Path -LiteralPath $executablePath) {
    $existing = Get-Item -LiteralPath $executablePath
    if ($existing.PSIsContainer -or $existing.Length -eq 0) {
        throw "The cached watcher output is invalid: '$executablePath'. Remove that file and rebuild."
    }
    Write-Output $executablePath
    return
}

$buildId = [Guid]::NewGuid().ToString('N')
$snapshotPath = Join-Path $outputRoot ('.ScreenshotWatcher-' + $buildId + '.cs')
$temporaryOutput = Join-Path $outputRoot ('.ScreenshotWatcher-' + $buildId + '.pending.exe')
$process = $null
try {
    [IO.File]::WriteAllBytes($snapshotPath, $sourceBytes)
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $compiler.FullName
    $startInfo.Arguments = '/nologo /target:winexe /optimize+ /platform:x86 /out:"' + $temporaryOutput + '" "' + $snapshotPath + '"'
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($startInfo)
    $standardOutput = $process.StandardOutput.ReadToEndAsync()
    $standardError = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $compilerOutput = ($standardOutput.GetAwaiter().GetResult() + "`n" + $standardError.GetAwaiter().GetResult()).Trim()
    if ($process.ExitCode -ne 0) {
        throw "C# watcher compilation failed (exit $($process.ExitCode)).`n$compilerOutput"
    }
    if (-not (Test-Path -LiteralPath $temporaryOutput -PathType Leaf)) {
        throw 'The C# compiler succeeded but did not produce the watcher executable.'
    }
    try {
        # This overload refuses to overwrite an executable from another build.
        [IO.File]::Move($temporaryOutput, $executablePath)
    } catch [IO.IOException] {
        if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf) -or (Get-Item -LiteralPath $executablePath).Length -eq 0) {
            throw
        }
    }
    Write-Output $executablePath
} finally {
    if ($null -ne $process) { $process.Dispose() }
    foreach ($temporaryPath in @($snapshotPath, $temporaryOutput)) {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}
