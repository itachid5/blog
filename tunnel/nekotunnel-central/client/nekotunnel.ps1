$ErrorActionPreference = "Stop"

$FrpcVersion = "0.57.0"
$AppDir = Join-Path $env:USERPROFILE ".nekotunnel"
$ConfigPath = Join-Path $AppDir "config.json"
$StatePath = Join-Path $AppDir "state.json"
$LogPath = Join-Path $AppDir "nekotunnel.log"
$InstalledScriptPath = Join-Path $AppDir "nekotunnel.ps1"
$TaskName = "NekoTunnel"
$Headers = @{ "bypass-tunnel-reminder" = "true" }

function Show-Usage {
    Write-Host "Usage:"
    Write-Host "  nekotunnel token <USER_TOKEN> --api <API_URL>"
    Write-Host "  nekotunnel login <USER_TOKEN> --api <API_URL>"
    Write-Host "  nekotunnel tcp <local_port>"
    Write-Host "  nekotunnel tcp <local_port> <USER_TOKEN> <API_URL>"
    Write-Host "  nekotunnel start tcp <local_port>"
    Write-Host "  nekotunnel stop"
    Write-Host "  nekotunnel restart"
    Write-Host "  nekotunnel status"
    Write-Host "  nekotunnel logs"
    Write-Host "  nekotunnel logout"
    exit 2
}

function Ensure-AppDir {
    if (-not (Test-Path $AppDir)) {
        New-Item -ItemType Directory -Path $AppDir | Out-Null
    }
}

function Mask-Token([string]$Token) {
    if ([string]::IsNullOrEmpty($Token)) { return "not configured" }
    if ($Token.Length -le 10) { return $Token.Substring(0, [Math]::Min(3, $Token.Length)) + "..." }
    return $Token.Substring(0, 7) + "..." + $Token.Substring($Token.Length - 4)
}

function Load-Config {
    if (-not (Test-Path $ConfigPath)) { return $null }
    return Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
}

function Save-Config([string]$ApiUrl, [string]$Token) {
    Ensure-AppDir
    $Existing = Load-Config
    $BackgroundPort = $null
    if ($null -ne $Existing -and $Existing.background_port) { $BackgroundPort = $Existing.background_port }
    $Config = [ordered]@{ api_url = $ApiUrl.TrimEnd('/'); user_token = $Token }
    if ($BackgroundPort) { $Config.background_port = $BackgroundPort }
    [pscustomobject]$Config | ConvertTo-Json | Set-Content -Path $ConfigPath -Encoding UTF8
}

function Save-BackgroundPort([int]$Port) {
    Ensure-AppDir
    $Config = Load-Config
    if ($null -eq $Config) { $Config = [pscustomobject]@{} }
    $Config | Add-Member -NotePropertyName background_port -NotePropertyValue $Port -Force
    $Config | ConvertTo-Json | Set-Content -Path $ConfigPath -Encoding UTF8
}

function Clear-Config {
    if (Test-Path $ConfigPath) { Remove-Item $ConfigPath -Force }
}

function Save-State($State) {
    Ensure-AppDir
    $State | ConvertTo-Json | Set-Content -Path $StatePath -Encoding UTF8
}

function Load-State {
    if (-not (Test-Path $StatePath)) { return $null }
    try {
        return Get-Content -Raw -Path $StatePath | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{ error = "state file is not valid JSON" }
    }
}

function Clear-State {
    if (Test-Path $StatePath) { Remove-Item $StatePath -Force }
}

function Parse-Port([string]$Value) {
    $Port = 0
    if (-not [int]::TryParse($Value, [ref]$Port) -or $Port -lt 1 -or $Port -gt 65535) {
        throw "local_port must be an integer from 1 to 65535"
    }
    return $Port
}

function Resolve-Credentials([string[]]$TcpArgs) {
    $CleanArgs = @($TcpArgs | Where-Object { $_ -ne "--foreground-service" })
    if ($CleanArgs.Count -eq 1) {
        $Config = Load-Config
        if ($null -eq $Config -or [string]::IsNullOrEmpty($Config.api_url) -or [string]::IsNullOrEmpty($Config.user_token)) {
            throw "No token/API configured. Run: nekotunnel token USER_TOKEN --api API_URL"
        }
        return [pscustomobject]@{ local_port = Parse-Port $CleanArgs[0]; token = $Config.user_token; api_url = $Config.api_url.TrimEnd('/') }
    }
    if ($CleanArgs.Count -eq 3) {
        return [pscustomobject]@{ local_port = Parse-Port $CleanArgs[0]; token = $CleanArgs[1]; api_url = $CleanArgs[2].TrimEnd('/') }
    }
    throw "Invalid tcp usage."
}

function Invoke-NekoPost([string]$ApiUrl, [string]$Path, $Payload) {
    $Uri = $ApiUrl.TrimEnd('/') + $Path
    return Invoke-RestMethod -Uri $Uri -Method Post -Headers $Headers -ContentType "application/json" -Body ($Payload | ConvertTo-Json)
}

function Ensure-Frpc([string]$ApiUrl) {
    Ensure-AppDir
    $BinDir = Join-Path $AppDir "frpc-$FrpcVersion"
    $FrpcPath = Join-Path $BinDir "frpc.exe"
    if (Test-Path $FrpcPath) { return $FrpcPath }

    $ArchiveName = "frp_${FrpcVersion}_windows_amd64.zip"
    $CacheDir = Join-Path $AppDir "cache"
    $ArchivePath = Join-Path $CacheDir $ArchiveName
    if (-not (Test-Path $CacheDir)) { New-Item -ItemType Directory -Path $CacheDir | Out-Null }
    if (-not (Test-Path $ArchivePath)) {
        $CentralUrl = $ApiUrl.TrimEnd('/') + "/client/frpc/$ArchiveName"
        Write-Host "Downloading frpc v$FrpcVersion from NekoTunnel Central..."
        try {
            Invoke-WebRequest -UseBasicParsing -Headers $Headers -Uri $CentralUrl -OutFile $ArchivePath
        } catch {
            $FallbackUrl = "https://github.com/fatedier/frp/releases/download/v$FrpcVersion/$ArchiveName"
            Write-Host "Central cache unavailable; downloading frpc from official GitHub release..."
            Invoke-WebRequest -UseBasicParsing -Uri $FallbackUrl -OutFile $ArchivePath
        }
    }

    $TempDir = Join-Path $env:TEMP ("nekotunnel-frpc-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $TempDir | Out-Null
    try {
        Expand-Archive -Path $ArchivePath -DestinationPath $TempDir -Force
        $Frpc = Get-ChildItem -Path $TempDir -Recurse -Filter "frpc.exe" | Select-Object -First 1
        if ($null -eq $Frpc) { throw "Downloaded frpc archive did not contain frpc.exe." }
        if (-not (Test-Path $BinDir)) { New-Item -ItemType Directory -Path $BinDir | Out-Null }
        Copy-Item -Path $Frpc.FullName -Destination $FrpcPath -Force
    } finally {
        Remove-Item -Path $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    return $FrpcPath
}

function Write-FrpcConfig($Allocation, [int]$LocalPort) {
    $ConfigPath = Join-Path $env:TEMP ("neko_config_" + $Allocation.session_id + ".toml")
    $ConfigText = @"
serverAddr = "$($Allocation.server_addr)"
serverPort = $($Allocation.server_port)

auth.method = "token"
auth.token = "$($Allocation.frp_token)"

loginFailExit = false

[transport]
protocol = "tcp"
heartbeatInterval = 10
heartbeatTimeout = 90
tcpMux = true
tcpMuxKeepaliveInterval = 30
tls.enable = true
tls.disableCustomTLSFirstByte = true

[[proxies]]
name = "$($Allocation.proxy_name)"
type = "tcp"
localIP = "127.0.0.1"
localPort = $LocalPort
remotePort = $($Allocation.remote_port)
"@
    Set-Content -Path $ConfigPath -Value $ConfigText -Encoding ASCII
    return $ConfigPath
}

function Connection-Command([int]$LocalPort, [string]$ServerAddr, $ServerPort) {
    if ($LocalPort -eq 3389) { return "RDP command: mstsc /v:${ServerAddr}:$ServerPort" }
    if ($LocalPort -eq 22) { return "SSH command: ssh username@$ServerAddr -p $ServerPort" }
    return "Connect to ${ServerAddr}:$ServerPort"
}

function Get-TaskState {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $Task) { return "not configured" }
    return $Task.State.ToString()
}

function Show-Status {
    $Config = Load-Config
    $FrpcPath = Join-Path (Join-Path $AppDir "frpc-$FrpcVersion") "frpc.exe"
    if ($null -eq $Config) {
        Write-Host "API URL: not configured"
        Write-Host "Token: not configured"
        Write-Host "Background port: not configured"
    } else {
        Write-Host "API URL: $($Config.api_url)"
        Write-Host "Token: $(Mask-Token $Config.user_token)"
        Write-Host "Background port: $(if ($Config.background_port) { $Config.background_port } else { 'not configured' })"
    }
    Write-Host "frpc v${FrpcVersion}: $(if (Test-Path $FrpcPath) { 'installed' } else { 'not installed' })"
    Write-Host "Scheduled task NekoTunnel: $(Get-TaskState)"
    $State = Load-State
    if ($null -eq $State) {
        Write-Host "Active session state: none"
    } elseif ($State.error) {
        Write-Host "State: $($State.error)"
    } else {
        Write-Host "Active session state:"
        Write-Host "  session_id: $($State.session_id)"
        Write-Host "  local_port: $($State.local_port)"
        Write-Host "  remote: $($State.server_addr):$($State.server_port)"
        Write-Host "  started_at: $($State.started_at)"
    }
}

function Disconnect-State {
    $State = Load-State
    if ($null -eq $State -or $State.error) { Clear-State; return }
    $Config = Load-Config
    if ($null -ne $Config -and $Config.api_url -and $Config.user_token -and $State.session_id) {
        try { Invoke-NekoPost $Config.api_url "/api/disconnect" @{ token = $Config.user_token; session_id = $State.session_id } | Out-Null } catch { Write-Host "Disconnect failed: $($_.Exception.Message)" }
    }
    Clear-State
}

function Run-Tcp-Once([string[]]$TcpArgs) {
    try {
        $Creds = Resolve-Credentials $TcpArgs
        $FrpcPath = Ensure-Frpc $Creds.api_url
        $Allocation = Invoke-NekoPost $Creds.api_url "/api/connect" @{ token = $Creds.token; local_port = $Creds.local_port; client_info = "nekotunnel-windows/$FrpcVersion" }
    } catch {
        Write-Error $_.Exception.Message
        return 1
    }

    if (-not $Allocation.ok) {
        $Allocation | ConvertTo-Json
        return 1
    }

    $FrpcConfig = Write-FrpcConfig $Allocation $Creds.local_port
    Save-State ([pscustomobject]@{
        session_id = $Allocation.session_id
        local_port = $Creds.local_port
        server_addr = $Allocation.server_addr
        server_port = $Allocation.server_port
        remote_port = $Allocation.remote_port
        proxy_name = $Allocation.proxy_name
        started_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    })

    Write-Host "Connected session: $($Allocation.session_id)"
    Write-Host (Connection-Command $Creds.local_port $Allocation.server_addr $Allocation.server_port)
    Write-Host "Using frpc config: $FrpcConfig"
    Write-Host "Press Ctrl+C to disconnect."

    $Process = Start-Process -FilePath $FrpcPath -ArgumentList @("-c", $FrpcConfig) -PassThru -NoNewWindow
    try {
        Start-Sleep -Milliseconds 500
        if ($Process.HasExited) {
            Write-Host "frpc exited immediately with code $($Process.ExitCode)."
        }
        while (-not $Process.HasExited) {
            $Interval = 15
            if ($Allocation.heartbeat_interval) { $Interval = [int]$Allocation.heartbeat_interval }
            Start-Sleep -Seconds $Interval
            try {
                $Heartbeat = Invoke-NekoPost $Creds.api_url "/api/heartbeat" @{ token = $Creds.token; session_id = $Allocation.session_id }
                if (-not $Heartbeat.ok) { break }
            } catch {
                Write-Host "Heartbeat failed: $($_.Exception.Message)"
                break
            }
        }
    } finally {
        if ($Process -and -not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            $Process.WaitForExit()
        }
        try { Invoke-NekoPost $Creds.api_url "/api/disconnect" @{ token = $Creds.token; session_id = $Allocation.session_id } | Out-Null } catch { Write-Host "Disconnect failed: $($_.Exception.Message)" }
        Clear-State
        Remove-Item -Path $FrpcConfig -Force -ErrorAction SilentlyContinue
    }
    return 0
}

function Run-Tcp([string[]]$TcpArgs) {
    if ($TcpArgs -notcontains "--foreground-service") {
        $Code = Run-Tcp-Once $TcpArgs
        if ($Code -ne 0) { exit $Code }
        return
    }
    while ($true) {
        $Code = Run-Tcp-Once $TcpArgs
        Write-Host "Tunnel exited with code $Code. Reconnecting in 5 seconds..."
        Start-Sleep -Seconds 5
    }
}

function Start-Background([string[]]$StartArgs) {
    if ($StartArgs.Count -ne 2 -or $StartArgs[0] -ne "tcp") { Show-Usage }
    $Port = Parse-Port $StartArgs[1]
    Ensure-AppDir
    Save-BackgroundPort $Port
    Copy-Item -Path $PSCommandPath -Destination $InstalledScriptPath -Force
    $CommandText = "& '$InstalledScriptPath' tcp $Port --foreground-service *> '$LogPath'"
    $Encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($CommandText))
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $Encoded"
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel LeastPrivilege
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "NekoTunnel background task started."
}

function Stop-Background {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $Task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host "NekoTunnel background task stopped."
    } else {
        Write-Host "NekoTunnel background task was not configured."
    }
    Disconnect-State
}

function Restart-Background {
    $Config = Load-Config
    if ($null -eq $Config -or -not $Config.background_port) {
        Write-Host "No saved background port. Use: nekotunnel start tcp <port>"
        exit 2
    }
    Stop-Background
    Start-Background @("tcp", [string]$Config.background_port)
}

function Show-Logs {
    Ensure-AppDir
    if (-not (Test-Path $LogPath)) { New-Item -ItemType File -Path $LogPath | Out-Null }
    Get-Content $LogPath -Tail 100 -Wait
}

if ($args.Count -lt 1) { Show-Usage }
$Command = $args[0]
$Rest = @($args | Select-Object -Skip 1)

switch ($Command) {
    "token" {
        if ($Rest.Count -ne 3 -or $Rest[1] -ne "--api") { Show-Usage }
        Save-Config $Rest[2] $Rest[0]
        Write-Host "Saved NekoTunnel config for $($Rest[2].TrimEnd('/')) with token $(Mask-Token $Rest[0])"
    }
    "login" {
        if ($Rest.Count -ne 3 -or $Rest[1] -ne "--api") { Show-Usage }
        Save-Config $Rest[2] $Rest[0]
        Write-Host "Saved NekoTunnel config for $($Rest[2].TrimEnd('/')) with token $(Mask-Token $Rest[0])"
    }
    "tcp" { Run-Tcp $Rest }
    "start" { Start-Background $Rest }
    "stop" {
        if ($Rest.Count -ne 0) { Show-Usage }
        Stop-Background
    }
    "restart" {
        if ($Rest.Count -ne 0) { Show-Usage }
        Restart-Background
    }
    "status" {
        if ($Rest.Count -ne 0) { Show-Usage }
        Show-Status
    }
    "logs" {
        if ($Rest.Count -ne 0) { Show-Usage }
        Show-Logs
    }
    "logout" {
        if ($Rest.Count -ne 0) { Show-Usage }
        Clear-Config
        Write-Host "Removed NekoTunnel config."
    }
    default { Show-Usage }
}
