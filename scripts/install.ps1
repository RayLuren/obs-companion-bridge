# SPDX-License-Identifier: GPL-2.0-or-later
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$NginxExecutable,
    [switch]$ReplaceExisting,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$checkout = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$toolRoot = 'D:\OBS伴侣桥接'
$programTarget = Join-Path $toolRoot 'app\1.0.2'
$relayTarget = Join-Path $toolRoot 'outputs\OBS伴侣本机桥接\rtmp_server'
$pluginTarget = 'D:\obs-studio\obs-plugins\64bit\obs-mate-menu.dll'
$programSource = Join-Path $checkout 'dist\OBS伴侣桥接'
$pluginSource = Join-Path $checkout 'obs_plugin\build-full\obs-mate-menu.dll'
$NginxExecutable = (Resolve-Path -LiteralPath $NginxExecutable).ProviderPath

if ($env:OS -ne 'Windows_NT') { throw 'Windows is required.' }
foreach ($file in @(
    (Join-Path $programSource 'OBS伴侣桥接.exe'), $pluginSource,
    (Join-Path $checkout 'relay\server.ps1'), (Join-Path $checkout 'relay\bridge.conf'),
    'D:\obs-studio\bin\64bit\obs64.exe',
    'D:\webcast_mate\13.1.1.502547155\直播伴侣.exe', $NginxExecutable
)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required file is missing: $file" }
}
if (-not (Test-Path -LiteralPath (Join-Path $programSource '_internal') -PathType Container)) {
    throw 'Build output _internal is required; copying only the EXE is unsupported.'
}
$obsVersion = (Get-Item -LiteralPath 'D:\obs-studio\bin\64bit\obs64.exe').VersionInfo.FileVersion
if ($obsVersion -notmatch '^32\.2\.1(?:\D|$)') { throw "OBS 32.2.1 is required, found '$obsVersion'." }
$active = @(Get-Process -Name 'obs64','直播伴侣','OBS伴侣桥接','nginx' -ErrorAction SilentlyContinue)
if ($active.Count) { throw 'Close OBS, Companion and the bridge normally, and ensure nginx has stopped before installing.' }
$existing = @(($programTarget, $relayTarget, $pluginTarget) | Where-Object { Test-Path -LiteralPath $_ })
if ($existing.Count -and -not $ReplaceExisting) {
    throw 'Existing installation found. Review it first; use -ReplaceExisting only for an intentional update with backup.'
}
if ($CheckOnly) {
    Write-Output 'Preflight passed. No files were copied or software started.'
    return
}

# Back up only the exact bridge-owned targets. Runtime/history and other tools stay in place.
if ($existing.Count) {
    $backupRoot = Join-Path $toolRoot ('install-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-ffff'))
    [void][IO.Directory]::CreateDirectory($backupRoot)
    if (Test-Path -LiteralPath $programTarget) {
        Copy-Item -LiteralPath $programTarget -Destination (Join-Path $backupRoot 'application') -Recurse
    }
    if (Test-Path -LiteralPath $relayTarget) {
        Copy-Item -LiteralPath $relayTarget -Destination (Join-Path $backupRoot 'relay') -Recurse
    }
    if (Test-Path -LiteralPath $pluginTarget) {
        Copy-Item -LiteralPath $pluginTarget -Destination (Join-Path $backupRoot 'obs-mate-menu.dll')
    }
    Write-Output "Previous bridge files backed up to $backupRoot"
}
foreach ($folder in @($programTarget, $relayTarget, [IO.Path]::GetDirectoryName($pluginTarget))) {
    [void][IO.Directory]::CreateDirectory($folder)
}
Get-ChildItem -LiteralPath $programSource -Force | Copy-Item -Destination $programTarget -Recurse -Force
Copy-Item -LiteralPath (Join-Path $checkout 'LICENSE') -Destination (Join-Path $programTarget 'LICENSE') -Force
Copy-Item -LiteralPath $pluginSource -Destination $pluginTarget -Force
Copy-Item -LiteralPath (Join-Path $checkout 'relay\server.ps1'),(Join-Path $checkout 'relay\bridge.conf') -Destination $relayTarget -Force
$nginxTarget = Join-Path $relayTarget 'nginx.exe'
if ($NginxExecutable -ine [IO.Path]::GetFullPath($nginxTarget)) {
    Copy-Item -LiteralPath $NginxExecutable -Destination $nginxTarget -Force
}
Write-Output 'Installed the bridge files. No application, service or stream was started. Configure OBS local H264/AAC output before use.'
