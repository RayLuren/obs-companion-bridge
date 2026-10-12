# SPDX-License-Identifier: GPL-2.0-or-later
# Download a pinned official source archive and derive import libraries locally.
# No installed OBS files are modified and no DLL is copied into the reference tree.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ObsBin,
    [string]$ObsReference = (Join-Path $PSScriptRoot '..\.deps\obs-32.2.1')
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$version = '32.2.1'
$archiveUri = 'https://github.com/obsproject/obs-studio/releases/download/32.2.1/OBS-Studio-32.2.1-Sources.tar.gz'
# Published by obsproject on the official 32.2.1 release page.
$archiveSha256 = '6a2532b1094bc51bc2fdeb1068d5c19cfe04216191a5b35c8707625401a80bf4'
if ($env:OS -ne 'Windows_NT') { throw 'Windows x64 is required.' }
if (-not [Environment]::Is64BitOperatingSystem) { throw 'A 64-bit Windows installation is required.' }
$ObsBin = (Resolve-Path -LiteralPath $ObsBin).ProviderPath
$ObsReference = [IO.Path]::GetFullPath($ObsReference)
$generated = Join-Path $ObsReference 'generated'
$source = Join-Path $ObsReference 'obs-src'

function Get-MsvcExportTools {
    $dumpbinCommand = Get-Command dumpbin.exe -ErrorAction SilentlyContinue
    $libCommand = Get-Command lib.exe -ErrorAction SilentlyContinue
    if ($dumpbinCommand -and $libCommand) {
        return @{ Dumpbin = $dumpbinCommand.Source; Lib = $libCommand.Source }
    }
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw 'MSVC tools were not found. Install Visual Studio Build Tools with Desktop development with C++, or run from an x64 Developer Command Prompt.'
    }
    $candidates = @(& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find 'VC\Tools\MSVC\**\bin\Hostx64\x64\dumpbin.exe')
    if ($LASTEXITCODE -ne 0 -or -not $candidates.Count) { throw 'vswhere found no MSVC x64 export tools.' }
    $dumpbinPath = ($candidates | Sort-Object -Descending | Select-Object -First 1)
    $libPath = Join-Path ([IO.Path]::GetDirectoryName($dumpbinPath)) 'lib.exe'
    if (-not (Test-Path -LiteralPath $libPath -PathType Leaf)) { throw 'MSVC lib.exe was not found alongside dumpbin.exe.' }
    return @{ Dumpbin = $dumpbinPath; Lib = $libPath }
}

$exportTools = Get-MsvcExportTools
foreach ($name in @('obs', 'obs-frontend-api')) {
    $dll = Join-Path $ObsBin ($name + '.dll')
    if (-not (Test-Path -LiteralPath $dll -PathType Leaf)) { throw "Required installed OBS DLL is missing: $dll" }
    $fileVersion = (Get-Item -LiteralPath $dll).VersionInfo.FileVersion
    if ($fileVersion -notmatch '^32\.2\.1(?:\D|$)') { throw "Expected OBS $version DLL, found '$fileVersion' in $dll" }
    $headers = @(& $exportTools.Dumpbin /nologo /headers $dll)
    if ($LASTEXITCODE -ne 0 -or -not ($headers -match '8664 machine')) { throw "Expected a readable x64 PE DLL: $dll" }
}

[void][IO.Directory]::CreateDirectory($ObsReference)
[void][IO.Directory]::CreateDirectory($generated)
$archive = Join-Path $ObsReference 'OBS-Studio-32.2.1-Sources.tar.gz'
if (-not (Test-Path -LiteralPath $archive -PathType Leaf) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $archiveSha256) {
    Write-Output "Downloading official OBS $version source archive..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $archiveUri -OutFile $archive
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $archiveSha256) { throw 'Official OBS source archive SHA-256 verification failed.' }
$tarCommand = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $tarCommand) { throw 'tar.exe is required to extract the verified archive.' }
[void][IO.Directory]::CreateDirectory($source)
# Extract headers only: unrelated formatting-tool symlinks in the full archive
# cannot be created on standard Windows installations without Developer Mode.
$archiveEntries = @(& $tarCommand.Source -tzf $archive)
if ($LASTEXITCODE -ne 0) { throw 'Verified OBS source archive listing failed.' }
$headerEntries = @($archiveEntries | Where-Object {
    $_ -match '^obs-studio-32\.2\.1-sources/(?:libobs/|frontend/api/).+\.h$' -or
    $_ -eq 'obs-studio-32.2.1-sources/COPYING'
})
if (-not $headerEntries.Count) { throw 'Expected headers were not found in the pinned source archive.' }
$headerList = Join-Path $ObsReference 'header-files.txt'
[IO.File]::WriteAllLines($headerList, $headerEntries, [Text.Encoding]::ASCII)
& $tarCommand.Source -xzf $archive --strip-components=1 -C $source -T $headerList
if ($LASTEXITCODE -ne 0) { throw 'Verified OBS header extraction failed.' }
foreach ($required in @('libobs\obs-module.h', 'frontend\api\obs-frontend-api.h')) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $required) -PathType Leaf)) { throw "Extracted source header is missing: $required" }
}

foreach ($name in @('obs', 'obs-frontend-api')) {
    $dll = Join-Path $ObsBin ($name + '.dll')
    $exports = @(& $exportTools.Dumpbin /nologo /exports $dll)
    if ($LASTEXITCODE -ne 0) { throw "Export inspection failed for $name.dll" }
    $names = @($exports | ForEach-Object {
        if ($_ -match '^\s+\d+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+(\S+)') { $Matches[1] }
    })
    if (-not $names.Count) { throw "No exports were found in $name.dll" }
    $def = Join-Path $generated ($name + '.def')
    [IO.File]::WriteAllLines($def, @(('LIBRARY ' + $name + '.dll'), 'EXPORTS') + $names, [Text.Encoding]::ASCII)
    & $exportTools.Lib /nologo /machine:x64 ("/def:$def") ("/out:" + (Join-Path $generated ($name + '.lib')))
    if ($LASTEXITCODE -ne 0) { throw "Import library generation failed for $name.dll" }
    Write-Output "$name`: generated import library from $($names.Count) installed DLL exports"
}
[IO.File]::WriteAllLines((Join-Path $generated 'obsconfig.h'), @(
    '#pragma once', '#define OBS_DATA_PATH "data"', '#define OBS_PLUGIN_PATH "obs-plugins/64bit"',
    '#define OBS_PLUGIN_DESTINATION "obs-plugins/64bit"', '#define OBS_RELEASE_CANDIDATE 0', '#define OBS_BETA 0'
), [Text.Encoding]::ASCII)
$manifest = [ordered]@{ version = $version; source_url = $archiveUri; source_sha256 = $archiveSha256; obs_bin = $ObsBin }
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ObsReference 'reference.json') -Encoding UTF8
Write-Output "Prepared local OBS reference: $ObsReference"
