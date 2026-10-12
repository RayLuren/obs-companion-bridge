param(
    [ValidateSet('Test','Start','Stop','Status')][string]$Mode = 'Status',
    [switch]$Popup
)
$ErrorActionPreference = 'Stop'
$serverRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$exe = Join-Path $serverRoot 'nginx.exe'
$config = Join-Path $serverRoot 'bridge.conf'
$prefix = $serverRoot.Replace('\','/') + '/'
$configArg = $config.Replace('\','/')
$pidFile = Join-Path $serverRoot 'logs\bridge-nginx.pid'

function Get-OwnedMaster {
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { return $null }
    $masterId = 0
    $pidText = ([IO.File]::ReadAllText($pidFile)).Trim()
    # nginx -t may create an empty PID file even though it never starts a master.
    if (-not $pidText) { return $null }
    if (-not [int]::TryParse($pidText, [ref]$masterId) -or $masterId -le 0) { throw 'The local PID file is invalid; no process was signalled.' }
    $process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $masterId)
    if ($null -eq $process) { return $null }
    if ($process.ExecutablePath -ne $exe -or -not $process.CommandLine -or
        -not $process.CommandLine.Contains($prefix) -or -not $process.CommandLine.Contains($configArg)) {
        throw 'PID ownership does not match this copied executable and prefix; no process was signalled.'
    }
    return $process
}

function Get-BridgeListeners {
    return @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {$_.LocalPort -in 19360,18081})
}

function Invoke-ConfigTest {
    [void][IO.Directory]::CreateDirectory((Join-Path $serverRoot 'logs'))
    [void][IO.Directory]::CreateDirectory((Join-Path $serverRoot 'temp'))
    & $exe -p $prefix -c $configArg -t
    if ($LASTEXITCODE -ne 0) { throw 'Local nginx configuration test failed; server was not started.' }
}

function Stop-OwnedServer {
    $master = Get-OwnedMaster
    if ($null -eq $master) { return [pscustomobject]@{stopped=$true;was_running=$false} }
    & $exe -p $prefix -c $configArg -s quit
    if ($LASTEXITCODE -ne 0) { throw 'The scoped quit signal failed; no global process termination was attempted.' }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt 7500 -and (Get-OwnedMaster)) { Start-Sleep -Milliseconds 100 }
    if (Get-OwnedMaster) {
        & $exe -p $prefix -c $configArg -s stop
        if ($LASTEXITCODE -ne 0) { throw 'The scoped stop signal failed.' }
        $watch.Restart()
        while ($watch.ElapsedMilliseconds -lt 2500 -and (Get-OwnedMaster)) { Start-Sleep -Milliseconds 100 }
    }
    if (Get-OwnedMaster) { throw 'The verified local master is still running; inspect its own logs before any further action.' }
    return [pscustomobject]@{stopped=$true;was_running=$true;master_pid=$master.ProcessId}
}

try {
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf) -or -not (Test-Path -LiteralPath $config -PathType Leaf)) { throw 'Copied nginx.exe or bridge.conf is missing.' }
    switch ($Mode) {
        'Test' { Invoke-ConfigTest; [pscustomobject]@{configuration_valid=$true;server_started=$false} | ConvertTo-Json -Compress }
        'Status' {
            $master = Get-OwnedMaster
            [pscustomobject]@{running=($null -ne $master);master_pid=$(if($master){$master.ProcessId}else{$null});listeners=@(Get-BridgeListeners|Select-Object LocalAddress,LocalPort,OwningProcess)} | ConvertTo-Json -Depth 4
        }
        'Stop' { Stop-OwnedServer | ConvertTo-Json -Compress }
        'Start' {
            $master = Get-OwnedMaster
            if ($master) { [pscustomobject]@{started=$true;already_running=$true;master_pid=$master.ProcessId}|ConvertTo-Json -Compress; break }
            $occupied = Get-BridgeListeners
            if ($occupied.Count -ne 0) { throw 'Port 19360 or 18081 is already listening; existing processes were left unchanged.' }
            Invoke-ConfigTest
            # A stale PID can be removed only after ownership checking found no running process.
            if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
            $arguments = '-p "' + $prefix + '" -c "' + $configArg + '"'
            [void](Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $serverRoot -WindowStyle Hidden -PassThru)
            $watch = [Diagnostics.Stopwatch]::StartNew(); $master = $null
            while ($watch.ElapsedMilliseconds -lt 4000) {
                Start-Sleep -Milliseconds 100
                $master = Get-OwnedMaster
                $listeners = Get-BridgeListeners
                if ($master -and $listeners.Count -eq 2 -and (@($listeners|Where-Object LocalAddress -ne '127.0.0.1').Count -eq 0)) { break }
            }
            if (-not $master -or $listeners.Count -ne 2 -or (@($listeners|Where-Object LocalAddress -ne '127.0.0.1').Count -ne 0)) {
                if ($master) { [void](Stop-OwnedServer) }
                throw 'The local loopback server did not become ready; inspect logs/error.log.'
            }
            [pscustomobject]@{started=$true;master_pid=$master.ProcessId;rtmp='rtmp://127.0.0.1:19360/obs';local_stream_name='main';http_flv='http://127.0.0.1:18081/live?port=19360&app=obs&stream=main'} | ConvertTo-Json -Compress
        }
    }
} catch {
    if ($Popup) { Add-Type -AssemblyName System.Windows.Forms; [void][Windows.Forms.MessageBox]::Show($_.Exception.Message,'Local OBS bridge') }
    else { Write-Error $_.Exception.Message }
    exit 1
}
