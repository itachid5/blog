$ErrorActionPreference = "Stop"

$FrpcVersion = "0.57.0"
$DefaultApiUrl = "https://ap.tunnel.theorbit.tech"
$AppDir = Join-Path $env:USERPROFILE ".nekotunnel"
$ConfigPath = Join-Path $AppDir "config.json"
$StatePath = Join-Path $AppDir "state.json"
$LogDir = Join-Path $AppDir "logs"
$LogPath = Join-Path $AppDir "nekotunnel.log"
$MachineIdPath = Join-Path $AppDir "machine_id"
$InstalledScriptPath = Join-Path $AppDir "nekotunnel.ps1"
$Headers = @{ "bypass-tunnel-reminder" = "true" }

function Show-Usage {
    Write-Host "Usage:"
    Write-Host "  nekotunnel token <USER_TOKEN> [--api <API_URL>]"
    Write-Host "  nekotunnel login <USER_TOKEN> [--api <API_URL>]"
    Write-Host "  nekotunnel api [<API_URL>]"
    Write-Host "  nekotunnel tcp <local_port> [--tcp-mux true|false]"
    Write-Host "  nekotunnel tcp <local_port> <USER_TOKEN> <API_URL> [--tcp-mux true|false]"
    Write-Host "  nekotunnel start tcp <local_port>"
    Write-Host "  nekotunnel start tcp <local_port> --persist"
    Write-Host "  nekotunnel install-service tcp <local_port>"
    Write-Host "  nekotunnel install-system-service tcp <local_port>"
    Write-Host "  nekotunnel stop tcp <local_port>"
    Write-Host "  nekotunnel stop all"
    Write-Host "  nekotunnel restart tcp <local_port>"
    Write-Host "  nekotunnel status"
    Write-Host "  nekotunnel logs tcp <local_port> [--frpc|--report]"
    Write-Host "  nekotunnel logs all"
    Write-Host "  nekotunnel logout"
    exit 2
}

function Ensure-AppDir {
    if (-not (Test-Path $AppDir)) {
        New-Item -ItemType Directory -Path $AppDir | Out-Null
    }
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir | Out-Null
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

function Get-MachineId {
    Ensure-AppDir
    if (Test-Path $MachineIdPath) {
        $Existing = (Get-Content -Raw -Path $MachineIdPath).Trim()
        if (-not [string]::IsNullOrEmpty($Existing)) { return $Existing }
    }
    $MachineId = [guid]::NewGuid().ToString()
    Set-Content -Path $MachineIdPath -Value $MachineId -Encoding ASCII
    return $MachineId
}

function Stable-EndpointId([string]$ApiUrl, [string]$Token, [string]$ClientId, [string]$Protocol, [int]$Port) {
    $Material = ($ApiUrl.TrimEnd('/') + "|" + $ClientId + "|" + $Protocol + "|" + $Port)
    $Bytes = [Text.Encoding]::UTF8.GetBytes($Material)
    $Hash = [Security.Cryptography.SHA256]::Create().ComputeHash($Bytes)
    return -join ($Hash | ForEach-Object { $_.ToString("x2") })
}

function Endpoint-Key([string]$Protocol, [int]$Port) {
    return "$Protocol-$Port"
}

function State-Path([string]$Protocol, [int]$Port) {
    return (Join-Path $AppDir ("state-" + (Endpoint-Key $Protocol $Port) + ".json"))
}

function Log-Path([string]$Protocol, [int]$Port) {
    return (Join-Path $LogDir ((Endpoint-Key $Protocol $Port) + ".log"))
}

function Frpc-Log-Path([string]$Protocol, [int]$Port, [string]$Stream) {
    return (Join-Path $LogDir ((Endpoint-Key $Protocol $Port) + ".frpc." + $Stream + ".log"))
}

function Task-Name([string]$Protocol, [int]$Port) {
    return "NekoTunnel-$(Endpoint-Key $Protocol $Port)"
}

function Strip-ValuedOption([string[]]$ArgsList, [string]$OptionName) {
    $Result = @()
    $Index = 0
    while ($Index -lt $ArgsList.Count) {
        if ($ArgsList[$Index] -eq $OptionName) {
            $Index += 2
        } else {
            $Result += $ArgsList[$Index]
            $Index += 1
        }
    }
    return @($Result)
}

function Parse-Endpoint([string[]]$EndpointArgs) {
    $FilteredArgs = @(Strip-ValuedOption (@($EndpointArgs | Where-Object { $_ -ne "--persist" })) "--tcp-mux")
    if ($FilteredArgs.Count -eq 1 -and $FilteredArgs[0] -eq "rdp") { throw (Rdp-UnsupportedMessage) }
    if ($FilteredArgs.Count -ne 2 -or $FilteredArgs[0] -ne "tcp") { return $null }
    return [pscustomobject]@{ protocol = "tcp"; port = Parse-Port $FilteredArgs[1] }
}

function TcpMux-Text([bool]$TcpMux) {
    if ($TcpMux) { return "true" }
    return "false"
}

function Parse-TcpMuxOption([string[]]$ArgsList, [int]$LocalPort) {
    $Value = $null
    $Index = 0
    while ($Index -lt $ArgsList.Count) {
        if ($ArgsList[$Index] -eq "--tcp-mux") {
            if ($Index + 1 -ge $ArgsList.Count) { throw "--tcp-mux requires true or false" }
            $Value = ([string]$ArgsList[$Index + 1]).ToLowerInvariant()
            $Index += 2
        } else {
            $Index += 1
        }
    }
    if ($null -eq $Value) { return $true }
    if (@("true", "1", "yes", "on") -contains $Value) { return $true }
    if (@("false", "0", "no", "off") -contains $Value) { return $false }
    throw "--tcp-mux must be true or false"
}

function Persisted-TcpMux([string]$Protocol, [int]$Port) {
    foreach ($Endpoint in @(Get-BackgroundEndpoints)) {
        if ($Endpoint.protocol -eq $Protocol -and [int]$Endpoint.port -eq $Port -and ($Endpoint.PSObject.Properties.Name -contains "tcp_mux")) {
            return [bool]$Endpoint.tcp_mux
        }
    }
    return $null
}

function Get-BackgroundEndpoints($Config = $null) {
    if ($null -eq $Config) { $Config = Load-Config }
    $Endpoints = @()
    if ($null -ne $Config -and $Config.background_endpoints) {
        foreach ($Endpoint in @($Config.background_endpoints)) {
            if ($Endpoint.protocol -eq "tcp") {
                $Item = [ordered]@{ protocol = "tcp"; port = Parse-Port ([string]$Endpoint.port) }
                if ($Endpoint.PSObject.Properties.Name -contains "tcp_mux") { $Item.tcp_mux = [bool]$Endpoint.tcp_mux }
                $Endpoints += [pscustomobject]$Item
            }
        }
    }
    if ($null -ne $Config -and $Config.background_port) {
        $Port = Parse-Port ([string]$Config.background_port)
        if (-not @($Endpoints | Where-Object { $_.protocol -eq "tcp" -and $_.port -eq $Port }).Count) {
            $Endpoints += [pscustomobject]@{ protocol = "tcp"; port = $Port }
        }
    }
    return @($Endpoints)
}

function Write-ConfigObject($Config) {
    Ensure-AppDir
    $Config | ConvertTo-Json -Depth 5 | Set-Content -Path $ConfigPath -Encoding UTF8
}

function Rdp-UnsupportedMessage {
    return "RDP is not officially supported. Use generic TCP:`nnekotunnel tcp 3389`nor`nnekotunnel start tcp 3389"
}

function Save-Config([string]$ApiUrl, [string]$Token) {
    Ensure-AppDir
    $Existing = Load-Config
    $Endpoints = Get-BackgroundEndpoints $Existing
    $Config = [ordered]@{ api_url = $ApiUrl.TrimEnd('/'); user_token = $Token }
    if ($null -ne $Existing -and $Existing.machine_id) { $Config.machine_id = [string]$Existing.machine_id }
    if ($Endpoints.Count -gt 0) { $Config.background_endpoints = @($Endpoints) }
    Write-ConfigObject ([pscustomobject]$Config)
}

function Save-BackgroundEndpoint([string]$Protocol, [int]$Port, [Nullable[bool]]$TcpMux = $null) {
    $Config = Load-Config
    if ($null -eq $Config) { $Config = [pscustomobject]@{} }
    $Endpoints = @(Get-BackgroundEndpoints $Config)
    $Updated = @()
    $Found = $false
    foreach ($Endpoint in $Endpoints) {
        if ($Endpoint.protocol -eq $Protocol -and [int]$Endpoint.port -eq $Port) {
            $Found = $true
            $Item = [ordered]@{ protocol = $Protocol; port = $Port }
            if ($null -ne $TcpMux) { $Item.tcp_mux = [bool]$TcpMux } elseif ($Endpoint.PSObject.Properties.Name -contains "tcp_mux") { $Item.tcp_mux = [bool]$Endpoint.tcp_mux }
            $Updated += [pscustomobject]$Item
        } else {
            $Updated += $Endpoint
        }
    }
    if (-not $Found) {
        $Item = [ordered]@{ protocol = $Protocol; port = $Port }
        if ($null -ne $TcpMux) { $Item.tcp_mux = [bool]$TcpMux }
        $Updated += [pscustomobject]$Item
    }
    $Config | Add-Member -NotePropertyName background_endpoints -NotePropertyValue @($Updated) -Force
    if ($Config.PSObject.Properties.Name -contains "background_port") { $Config.PSObject.Properties.Remove("background_port") }
    Write-ConfigObject $Config
}

function Remove-BackgroundEndpoint([string]$Protocol, [int]$Port) {
    $Config = Load-Config
    if ($null -eq $Config) { return }
    $Endpoints = @(Get-BackgroundEndpoints $Config | Where-Object { -not ($_.protocol -eq $Protocol -and $_.port -eq $Port) })
    $Config | Add-Member -NotePropertyName background_endpoints -NotePropertyValue @($Endpoints) -Force
    if ($Config.PSObject.Properties.Name -contains "background_port") { $Config.PSObject.Properties.Remove("background_port") }
    Write-ConfigObject $Config
}

function Clear-Config {
    if (Test-Path $ConfigPath) { Remove-Item $ConfigPath -Force }
}

function Save-State($State, [string]$Path = $StatePath) {
    Ensure-AppDir
    $State | ConvertTo-Json | Set-Content -Path $Path -Encoding UTF8
}

function Load-State([string]$Path = $StatePath) {
    if (-not (Test-Path $Path)) { return $null }
    try {
        return Get-Content -Raw -Path $Path | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{ error = "state file is not valid JSON" }
    }
}

function Clear-State([string]$Path = $StatePath) {
    if (Test-Path $Path) { Remove-Item $Path -Force }
}

function Parse-Port([string]$Value) {
    $Port = 0
    if (-not [int]::TryParse($Value, [ref]$Port) -or $Port -lt 1 -or $Port -gt 65535) {
        throw "local_port must be an integer from 1 to 65535"
    }
    return $Port
}

function Resolve-Credentials([string[]]$TcpArgs) {
    $CleanArgs = @(Strip-ValuedOption (@($TcpArgs | Where-Object { $_ -ne "--foreground-service" })) "--tcp-mux")
    if ($CleanArgs.Count -eq 1) {
        $Config = Load-Config
        if ($null -eq $Config -or [string]::IsNullOrEmpty($Config.user_token)) {
            throw "No token configured. Run: nekotunnel token USER_TOKEN"
        }
        $Port = Parse-Port $CleanArgs[0]
        $ApiUrl = if ([string]::IsNullOrEmpty($Config.api_url)) { $DefaultApiUrl } else { $Config.api_url.TrimEnd('/') }
        return [pscustomobject]@{ local_port = $Port; token = $Config.user_token; api_url = $ApiUrl; tcp_mux = (Parse-TcpMuxOption $TcpArgs $Port) }
    }
    if ($CleanArgs.Count -eq 3) {
        $Port = Parse-Port $CleanArgs[0]
        return [pscustomobject]@{ local_port = $Port; token = $CleanArgs[1]; api_url = $CleanArgs[2].TrimEnd('/'); tcp_mux = (Parse-TcpMuxOption $TcpArgs $Port) }
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
    $TcpMux = if ($Allocation.PSObject.Properties.Name -contains "tcp_mux") { [bool]$Allocation.tcp_mux } else { $LocalPort -ne 3389 }
    $TcpMuxValue = TcpMux-Text $TcpMux
    $ConfigText = @"
serverAddr = "$($Allocation.server_addr)"
serverPort = $($Allocation.server_port)

[auth]
method = "token"
token = "$($Allocation.frp_token)"

[transport]
protocol = "tcp"
heartbeatInterval = 20
heartbeatTimeout = 120
tcpMux = $TcpMuxValue
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

function Test-FrpcConfig([string]$FrpcPath, [string]$ConfigPath) {
    try {
        $Output = & $FrpcPath verify -c $ConfigPath 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) { return [pscustomobject]@{ ok = $true; message = $Output.Trim() } }
        if ($Output.ToLowerInvariant().Contains("unknown command") -and $Output.ToLowerInvariant().Contains("verify")) {
            return [pscustomobject]@{ ok = $true; message = "frpc config validation skipped: verify command is not supported" }
        }
        if ([string]::IsNullOrWhiteSpace($Output)) { $Output = "frpc verify exited with code $LASTEXITCODE" }
        return [pscustomobject]@{ ok = $false; message = $Output.Trim() }
    } catch {
        return [pscustomobject]@{ ok = $true; message = "frpc config validation skipped: $($_.Exception.Message)" }
    }
}

function Write-FrpcValidationError([string]$Protocol, [int]$Port, [string]$Message) {
    try {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
        Add-Content -Path (Frpc-Log-Path $Protocol $Port "err") -Value "frpc config validation failed: $Message"
    } catch {}
}

function Connection-Command([int]$LocalPort, [string]$ServerAddr, $ServerPort) {
    if ($LocalPort -eq 22) { return "SSH command: ssh username@$ServerAddr -p $ServerPort" }
    return "TCP connect command: connect to ${ServerAddr}:$ServerPort"
}

function Get-TaskState([string]$Name) {
    $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $Task) { return "not configured" }
    return $Task.State.ToString()
}

function Show-Status {
    $Config = Load-Config
    $Endpoints = Get-BackgroundEndpoints $Config
    $FrpcPath = Join-Path (Join-Path $AppDir "frpc-$FrpcVersion") "frpc.exe"
    if ($null -eq $Config) {
        Write-Host "API URL: $DefaultApiUrl"
        Write-Host "Token: not configured"
    } else {
        $ApiUrl = if ([string]::IsNullOrEmpty($Config.api_url)) { $DefaultApiUrl } else { $Config.api_url }
        Write-Host "API URL: $ApiUrl"
        Write-Host "Token: $(Mask-Token $Config.user_token)"
    }
    Write-Host "frpc v${FrpcVersion}: $(if (Test-Path $FrpcPath) { 'installed' } else { 'not installed' })"
    Write-Host "Background endpoints:"
    if ($Endpoints.Count -eq 0) { Write-Host "  none configured" }
    foreach ($Endpoint in $Endpoints) {
        $Protocol = [string]$Endpoint.protocol
        $Port = [int]$Endpoint.port
        $Key = Endpoint-Key $Protocol $Port
        $TaskName = Task-Name $Protocol $Port
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $TaskExists = if ($null -ne $Task) { "yes" } else { "no" }
        $TaskRunning = if ($null -ne $Task -and $Task.State.ToString() -eq "Running") { "yes" } else { "no" }
        Write-Host "  ${Key}: task=$TaskName exists=$TaskExists running=$TaskRunning"
        $State = Load-State (State-Path $Protocol $Port)
        if ($null -ne $State -and -not $State.error) {
            Write-Host "    session_id: $($State.session_id)"
            Write-Host "    remote: $($State.server_addr):$($State.server_port)"
            Write-Host "    endpoint_id: $(([string]$State.endpoint_id).Substring(0, [Math]::Min(12, ([string]$State.endpoint_id).Length)))"
            Write-Host "    reconnect_count: $($State.reconnect_count)"
            Write-Host "    started_at: $($State.started_at)"
            if ($State.last_error) { Write-Host "    last_error: $($State.last_error)" }
        } elseif ($null -ne $State -and $State.error) {
            Write-Host "    last_error: $($State.error)"
        }
    }
    $State = Load-State
    if ($null -ne $State) {
        Write-Host "Foreground session state:"
        if ($State.error) {
            Write-Host "  $($State.error)"
        } else {
            Write-Host "  session_id: $($State.session_id)"
            Write-Host "  local_port: $($State.local_port)"
            Write-Host "  remote: $($State.server_addr):$($State.server_port)"
        }
    }
}

function Disconnect-State([string]$Path = $StatePath) {
    $State = Load-State $Path
    if ($null -eq $State -or $State.error) { Clear-State $Path; return }
    $Config = Load-Config
    if ($null -ne $Config -and $Config.api_url -and $Config.user_token -and $State.session_id) {
        try { Invoke-NekoPost $Config.api_url "/api/disconnect" @{ token = $Config.user_token; session_id = $State.session_id; release = $true } | Out-Null } catch { Write-Host "Disconnect failed: $($_.Exception.Message)" }
    }
    Clear-State $Path
}

function Run-Tcp-Once([string[]]$TcpArgs) {
    try {
        $Creds = Resolve-Credentials $TcpArgs
        $FrpcPath = Ensure-Frpc $Creds.api_url
        $Protocol = "tcp"
        $ClientId = Get-MachineId
        $EndpointId = Stable-EndpointId $Creds.api_url $Creds.token $ClientId $Protocol $Creds.local_port
        $ServiceMode = $TcpArgs -contains "--foreground-service"
        $StateFile = if ($ServiceMode) { State-Path $Protocol $Creds.local_port } else { $StatePath }
        $Allocation = Invoke-NekoPost $Creds.api_url "/api/connect" @{ token = $Creds.token; protocol = $Protocol; local_port = $Creds.local_port; client_info = "nekotunnel-windows/$FrpcVersion"; client_id = $ClientId; endpoint_id = $EndpointId; tcp_mux = $Creds.tcp_mux }
    } catch {
        Write-Error $_.Exception.Message
        return 1
    }

    if (-not $Allocation.ok) {
        $Allocation | ConvertTo-Json
        return 1
    }

    $FrpcConfig = Write-FrpcConfig $Allocation $Creds.local_port
    $Verify = Test-FrpcConfig $FrpcPath $FrpcConfig
    if (-not $Verify.ok) {
        Write-Host "frpc config validation failed: $($Verify.message)"
        Write-FrpcValidationError $Protocol $Creds.local_port $Verify.message
        try { Invoke-NekoPost $Creds.api_url "/api/disconnect" @{ token = $Creds.token; session_id = $Allocation.session_id; release = $true } | Out-Null } catch { Write-Host "Disconnect failed: $($_.Exception.Message)" }
        Remove-Item -Path $FrpcConfig -Force -ErrorAction SilentlyContinue
        return 1
    }

    $PreviousState = Load-State $StateFile
    $PreviousPublic = $null
    if ($null -ne $PreviousState -and $PreviousState.server_addr -and $PreviousState.server_port) { $PreviousPublic = "$($PreviousState.server_addr):$($PreviousState.server_port)" }
    $CurrentPublic = "$($Allocation.server_addr):$($Allocation.server_port)"
    $StateData = [pscustomobject]@{
        session_id = $Allocation.session_id
        slot_id = $Allocation.slot_id
        protocol = $Protocol
        local_port = $Creds.local_port
        server_addr = $Allocation.server_addr
        server_port = $Allocation.server_port
        remote_port = $Allocation.remote_port
        proxy_name = $Allocation.proxy_name
        client_id = $Allocation.client_id
        endpoint_id = $Allocation.endpoint_id
        reconnect_count = $Allocation.reconnect_count
        tcp_mux = if ($Allocation.PSObject.Properties.Name -contains "tcp_mux") { [bool]$Allocation.tcp_mux } else { [bool]$Creds.tcp_mux }
        route_mode = if ($Allocation.route_mode) { $Allocation.route_mode } else { "mux" }
        connection_profile = if ($Allocation.connection_profile) { $Allocation.connection_profile } else { "generic" }
        same_endpoint_reused = if ($null -ne $PreviousState -and $PreviousState.endpoint_id) { $PreviousState.endpoint_id -eq $Allocation.endpoint_id } else { $false }
        same_slot_reused = if ($null -ne $PreviousState -and $PreviousState.slot_id) { $PreviousState.slot_id -eq $Allocation.slot_id } else { $false }
        public_address_changed = if ($PreviousPublic) { $PreviousPublic -ne $CurrentPublic } else { $false }
        public_address_reused = if ($PreviousPublic) { $PreviousPublic -eq $CurrentPublic } else { $false }
        started_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    }
    Save-State $StateData $StateFile

    Write-Host "Connected session: $($Allocation.session_id)"
    Write-Host "public_address=$($Allocation.server_addr):$($Allocation.server_port) reconnect_count=$($Allocation.reconnect_count)"
    Write-Host (Connection-Command $Creds.local_port $Allocation.server_addr $Allocation.server_port)
    Write-Host "Using frpc config: $FrpcConfig"
    if ($ServiceMode) {
        Write-Host "NekoTunnel service mode running. Use `nekotunnel stop $Protocol $($Creds.local_port)` to stop."
    } else {
        Write-Host "Press Ctrl+C to disconnect."
    }

    $FrpcOut = Frpc-Log-Path $Protocol $Creds.local_port "out"
    $FrpcErr = Frpc-Log-Path $Protocol $Creds.local_port "err"
    if ($ServiceMode) {
        if (-not (Test-Path $FrpcOut)) { New-Item -ItemType File -Path $FrpcOut -Force | Out-Null }
        if (-not (Test-Path $FrpcErr)) { New-Item -ItemType File -Path $FrpcErr -Force | Out-Null }
        $Process = Start-Process -FilePath $FrpcPath -ArgumentList @("-c", $FrpcConfig) -PassThru -RedirectStandardOutput $FrpcOut -RedirectStandardError $FrpcErr -WindowStyle Hidden
    } else {
        $Process = Start-Process -FilePath $FrpcPath -ArgumentList @("-c", $FrpcConfig) -PassThru -NoNewWindow
    }
    try {
        Start-Sleep -Milliseconds 500
        if ($Process.HasExited) {
            Write-Host "frpc exited immediately with code $($Process.ExitCode)."
        }
        $HeartbeatMisses = 0
        while (-not $Process.HasExited) {
            $Interval = 20
            if ($Allocation.heartbeat_interval) { $Interval = [int]$Allocation.heartbeat_interval }
            Start-Sleep -Seconds $Interval
            try {
                $Heartbeat = Invoke-NekoPost $Creds.api_url "/api/heartbeat" @{ token = $Creds.token; session_id = $Allocation.session_id }
                if ($Heartbeat.ok) {
                    if ($HeartbeatMisses -gt 0) { Write-Host "API heartbeat recovered" }
                    if ($ServiceMode -and ($StateData.PSObject.Properties.Name -contains "last_heartbeat_error")) { $StateData.PSObject.Properties.Remove("last_heartbeat_error"); Save-State $StateData $StateFile }
                    $HeartbeatMisses = 0
                } else {
                    $HeartbeatMisses += 1
                    $HeartbeatMessage = "API heartbeat degraded $HeartbeatMisses"
                    Write-Host $HeartbeatMessage
                    if ($ServiceMode) { $StateData | Add-Member -NotePropertyName last_heartbeat_error -NotePropertyValue $HeartbeatMessage -Force; Save-State $StateData $StateFile }
                }
            } catch {
                $HeartbeatMisses += 1
                $HeartbeatMessage = "API heartbeat failure ${HeartbeatMisses}: $($_.Exception.Message)"
                Write-Host $HeartbeatMessage
                if ($ServiceMode) { $StateData | Add-Member -NotePropertyName last_heartbeat_error -NotePropertyValue $HeartbeatMessage -Force; Save-State $StateData $StateFile }
            }
        }
    } finally {
        if ($Process -and -not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            $Process.WaitForExit()
        }
        $ExitCode = if ($Process) { $Process.ExitCode } else { 1 }
        $Release = -not $ServiceMode
        try { Invoke-NekoPost $Creds.api_url "/api/disconnect" @{ token = $Creds.token; session_id = $Allocation.session_id; release = $Release } | Out-Null } catch { Write-Host "Disconnect failed: $($_.Exception.Message)" }
        if ($ServiceMode) {
            $StateData | Add-Member -NotePropertyName frpc_exit_code -NotePropertyValue $ExitCode -Force
            $StateData | Add-Member -NotePropertyName last_error -NotePropertyValue "frpc exited with code $ExitCode" -Force
            $StateData | Add-Member -NotePropertyName ended_at -NotePropertyValue (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") -Force
            Save-State $StateData $StateFile
            $ExitAt = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
            $TcpMuxLabel = TcpMux-Text ([bool]$StateData.tcp_mux)
            Write-Host "frpc_exit_at=$ExitAt exit_code=$ExitCode reconnect_count=$($Allocation.reconnect_count) same_endpoint_reused=$($StateData.same_endpoint_reused) public_address_reused=$($StateData.public_address_reused) same_slot_reused=$($StateData.same_slot_reused) tcpMux=$TcpMuxLabel tcp_profile=$($StateData.connection_profile) route_mode=$($StateData.route_mode)"
            Show-RecentLog $FrpcOut "last_100_frpc_out" 100
            Show-RecentLog $FrpcErr "last_100_frpc_err" 100
        } else {
            Clear-State $StateFile
        }
        Remove-Item -Path $FrpcConfig -Force -ErrorAction SilentlyContinue
    }
    return $ExitCode
}

function Run-Tcp([string[]]$TcpArgs) {
    if ($TcpArgs -notcontains "--foreground-service") {
        $Code = Run-Tcp-Once $TcpArgs
        if ($Code -ne 0) { exit $Code }
        return
    }
    $Backoffs = @(2, 5, 10, 20, 30, 60)
    $BackoffIndex = 0
    while ($true) {
        $Started = Get-Date
        $Code = Run-Tcp-Once $TcpArgs
        if (((Get-Date) - $Started).TotalSeconds -ge 120) { $BackoffIndex = 0 }
        $Delay = $Backoffs[[Math]::Min($BackoffIndex, $Backoffs.Count - 1)]
        $Jitter = Get-Random -Minimum 0 -Maximum ([Math]::Max(1, [int]($Delay / 5 + 1)))
        $WaitFor = [Math]::Min(60, $Delay + $Jitter)
        try {
            $PortArgs = @(Strip-ValuedOption (@($TcpArgs | Where-Object { $_ -ne "--foreground-service" })) "--tcp-mux")
            $StateFile = State-Path "tcp" (Parse-Port $PortArgs[0])
            $State = Load-State $StateFile
            if ($null -ne $State) {
                $State | Add-Member -NotePropertyName last_reconnect_delay_seconds -NotePropertyValue $WaitFor -Force
                $State | Add-Member -NotePropertyName last_error -NotePropertyValue "frpc exited with code $Code" -Force
                Save-State $State $StateFile
            }
        } catch {}
        Write-Host "Tunnel exited with code $Code. Reconnecting in $WaitFor seconds..."
        $BackoffIndex = [Math]::Min($BackoffIndex + 1, $Backoffs.Count - 1)
        Start-Sleep -Seconds $WaitFor
    }
}

function Run-Service([string[]]$ServiceArgs) {
    $Endpoint = Parse-Endpoint $ServiceArgs
    if ($null -eq $Endpoint) { Show-Usage }
    Ensure-AppDir
    $Mux = if ($ServiceArgs -contains "--tcp-mux") { Parse-TcpMuxOption $ServiceArgs $Endpoint.port } else { Persisted-TcpMux $Endpoint.protocol $Endpoint.port }
    if ($null -eq $Mux) { $Mux = ($Endpoint.port -ne 3389) }
    $LogFile = Log-Path $Endpoint.protocol $Endpoint.port
    try { Start-Transcript -Path $LogFile -Append | Out-Null } catch {}
    try {
        Write-Host "started_at=$((Get-Date).ToString("yyyy-MM-dd HH:mm:ss")) endpoint=$(Endpoint-Key $Endpoint.protocol $Endpoint.port) tcpMux=$(TcpMux-Text $Mux)"
        Run-Tcp @([string]$Endpoint.port, "--foreground-service", "--tcp-mux", (TcpMux-Text $Mux))
    } finally {
        try { Stop-Transcript | Out-Null } catch {}
    }
}

function Install-Self {
    Ensure-AppDir
    $SourcePath = [System.IO.Path]::GetFullPath($PSCommandPath)
    $DestPath = [System.IO.Path]::GetFullPath($InstalledScriptPath)
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($SourcePath, $DestPath)) {
        Copy-Item -Path $SourcePath -Destination $DestPath -Force
    }
}

function Start-Background([string[]]$StartArgs) {
    $Endpoint = Parse-Endpoint $StartArgs
    if ($null -eq $Endpoint) { Show-Usage }
    $Protocol = [string]$Endpoint.protocol
    $Port = [int]$Endpoint.port
    Ensure-AppDir
    $TcpMux = Parse-TcpMuxOption $StartArgs $Port
    Save-BackgroundEndpoint $Protocol $Port $TcpMux
    Install-Self
    $TaskName = Task-Name $Protocol $Port
    $LogFile = Log-Path $Protocol $Port
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstalledScriptPath`" run-service $Protocol $Port --tcp-mux $(TcpMux-Text $TcpMux)"
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -Hidden -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "NekoTunnel background task $TaskName started."
}

function Install-System-Service([string[]]$ServiceArgs) {
    Start-Background $ServiceArgs
    Write-Host "AtLogOn task installed. It will start after user login. For before-login service mode, run as Administrator and install service mode."
}

function Stop-Endpoint([string]$Protocol, [int]$Port, [bool]$RemoveConfig = $true) {
    $TaskName = Task-Name $Protocol $Port
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $Stopped = $false
    if ($null -ne $Task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        $Stopped = $true
    }
    Disconnect-State (State-Path $Protocol $Port)
    if ($RemoveConfig) { Remove-BackgroundEndpoint $Protocol $Port }
    return $Stopped
}

function Stop-Background([string[]]$StopArgs) {
    if ($StopArgs.Count -eq 0 -or ($StopArgs.Count -eq 1 -and $StopArgs[0] -eq "all")) {
        $Endpoints = Get-BackgroundEndpoints
        if ($Endpoints.Count -eq 0) { Write-Host "No background endpoints configured."; return }
        foreach ($Endpoint in $Endpoints) {
            $Protocol = [string]$Endpoint.protocol
            $Port = [int]$Endpoint.port
            if (Stop-Endpoint $Protocol $Port) {
                Write-Host "Stopped $(Endpoint-Key $Protocol $Port)."
            } else {
                Write-Host "$(Endpoint-Key $Protocol $Port) was not configured."
            }
        }
        return
    }
    $Endpoint = Parse-Endpoint $StopArgs
    if ($null -eq $Endpoint) { Show-Usage }
    if (Stop-Endpoint $Endpoint.protocol $Endpoint.port) {
        Write-Host "NekoTunnel background endpoint $(Endpoint-Key $Endpoint.protocol $Endpoint.port) stopped."
    } else {
        Write-Host "NekoTunnel background endpoint $(Endpoint-Key $Endpoint.protocol $Endpoint.port) was not configured."
    }
}

function Restart-Background([string[]]$RestartArgs) {
    $Endpoint = Parse-Endpoint $RestartArgs
    if ($null -eq $Endpoint) { Show-Usage }
    Stop-Endpoint $Endpoint.protocol $Endpoint.port $false | Out-Null
    $Args = @($Endpoint.protocol, [string]$Endpoint.port)
    if ($RestartArgs -contains "--tcp-mux") { $Args += @("--tcp-mux", (TcpMux-Text (Parse-TcpMuxOption $RestartArgs $Endpoint.port))) }
    Start-Background $Args
}

function Show-RecentLog([string]$Path, [string]$Label, [int]$Tail = 100) {
    Write-Host "==> $Label ($Path) <=="
    if (Test-Path $Path) { Get-Content $Path -Tail $Tail } else { Write-Host "No log file yet." }
}

function Last-LogLine([string]$Path) {
    if (-not (Test-Path $Path)) { return "-" }
    $Line = Get-Content $Path -Tail 1 -ErrorAction SilentlyContinue
    if ($null -eq $Line) { return "-" }
    return [string]$Line
}

function Show-LogReport([string]$Protocol, [int]$Port) {
    $State = Load-State (State-Path $Protocol $Port)
    if ($null -eq $State) { $State = [pscustomobject]@{} }
    Write-Host "endpoint=$(Endpoint-Key $Protocol $Port)"
    Write-Host "last_public_address=$($State.server_addr):$($State.server_port)"
    $EndpointId = [string]$State.endpoint_id
    Write-Host "endpoint_id_short=$($EndpointId.Substring(0, [Math]::Min(12, $EndpointId.Length)))"
    Write-Host "same_slot_reused=$($State.same_slot_reused)"
    Write-Host "same_endpoint_reused=$($State.same_endpoint_reused)"
    Write-Host "public_address_changed=$($State.public_address_changed)"
    Write-Host "public_address_reused=$($State.public_address_reused)"
    Write-Host "reconnect_count=$($State.reconnect_count)"
    Write-Host "last_frpc_exit_code=$($State.frpc_exit_code)"
    $LastFrpcErr = Last-LogLine (Frpc-Log-Path $Protocol $Port "err")
    Write-Host "last_frpc_stderr_reason=$LastFrpcErr"
    Write-Host "last_api_heartbeat_error=$($State.last_heartbeat_error)"
    Write-Host "current_mode=$(if (Test-Path (State-Path $Protocol $Port)) { 'background' } else { 'foreground' })"
    Write-Host "current_tcp_profile=$($State.connection_profile)"
    Write-Host "tcpMux=$($State.tcp_mux)"
    Write-Host "route_mode=$(if ($State.route_mode) { $State.route_mode } else { 'mux' })"
}

function Show-Logs([string[]]$LogArgs) {
    Ensure-AppDir
    if ($LogArgs.Count -eq 0) {
        Write-Host "Usage: nekotunnel logs <all|tcp <local_port>> [--frpc|--report]"
        Write-Host "Try: nekotunnel logs all"
        Write-Host "Try: nekotunnel logs tcp 3389"
        return
    }
    if ($LogArgs.Count -eq 1 -and $LogArgs[0] -eq "all") {
        $Endpoints = Get-BackgroundEndpoints
        if ($Endpoints.Count -eq 0) { Write-Host "No background endpoints configured."; return }
        foreach ($Endpoint in $Endpoints) {
            Show-RecentLog (Log-Path $Endpoint.protocol $Endpoint.port) "$(Endpoint-Key $Endpoint.protocol $Endpoint.port)" 100
        }
        return
    }
    $Frpc = $LogArgs -contains "--frpc"
    $Report = $LogArgs -contains "--report"
    $EndpointArgs = @($LogArgs | Where-Object { $_ -ne "--frpc" -and $_ -ne "--report" })
    $Endpoint = Parse-Endpoint $EndpointArgs
    if ($null -eq $Endpoint) { Show-Usage }
    if ($Report) { Show-LogReport $Endpoint.protocol $Endpoint.port; return }
    if ($Frpc) {
        Show-RecentLog (Log-Path $Endpoint.protocol $Endpoint.port) "main" 100
        Show-RecentLog (Frpc-Log-Path $Endpoint.protocol $Endpoint.port "out") "frpc stdout" 100
        Show-RecentLog (Frpc-Log-Path $Endpoint.protocol $Endpoint.port "err") "frpc stderr" 100
        return
    }
    $Path = Log-Path $Endpoint.protocol $Endpoint.port
    if (-not (Test-Path $Path)) { New-Item -ItemType File -Path $Path | Out-Null }
    Get-Content $Path -Tail 100 -Wait
}

if ($args.Count -lt 1) { Show-Usage }
$Command = $args[0]
$Rest = @($args | Select-Object -Skip 1)

switch ($Command) {
    "token" {
        if ($Rest.Count -eq 1) { $ApiUrl = $DefaultApiUrl } elseif ($Rest.Count -eq 3 -and $Rest[1] -eq "--api") { $ApiUrl = $Rest[2] } else { Show-Usage }
        Save-Config $ApiUrl $Rest[0]
        Write-Host "Saved NekoTunnel config for $($ApiUrl.TrimEnd('/')) with token $(Mask-Token $Rest[0])"
    }
    "login" {
        if ($Rest.Count -eq 1) { $ApiUrl = $DefaultApiUrl } elseif ($Rest.Count -eq 3 -and $Rest[1] -eq "--api") { $ApiUrl = $Rest[2] } else { Show-Usage }
        Save-Config $ApiUrl $Rest[0]
        Write-Host "Saved NekoTunnel config for $($ApiUrl.TrimEnd('/')) with token $(Mask-Token $Rest[0])"
    }
    "tcp" { Run-Tcp $Rest }
    "rdp" { Write-Host (Rdp-UnsupportedMessage); exit 2 }
    "api" {
        $Config = Load-Config
        if ($Rest.Count -eq 0) {
            $ApiUrl = if ($null -eq $Config -or [string]::IsNullOrEmpty($Config.api_url)) { $DefaultApiUrl } else { $Config.api_url.TrimEnd('/') }
            Write-Host "API URL: $ApiUrl"
        } elseif ($Rest.Count -eq 1) {
            if ($null -eq $Config) { $Config = [pscustomobject]@{} }
            $Config | Add-Member -NotePropertyName api_url -NotePropertyValue $Rest[0].TrimEnd('/') -Force
            Write-ConfigObject $Config
            Write-Host "API URL: $($Rest[0].TrimEnd('/'))"
        } else { Show-Usage }
    }
    "run-service" { Run-Service $Rest }
    "start" { Start-Background $Rest }
    "install-service" { Start-Background $Rest }
    "install-system-service" { Install-System-Service $Rest }
    "stop" { Stop-Background $Rest }
    "restart" { Restart-Background $Rest }
    "status" {
        if ($Rest.Count -ne 0) { Show-Usage }
        Show-Status
    }
    "logs" { Show-Logs $Rest }
    "logout" {
        if ($Rest.Count -ne 0) { Show-Usage }
        Clear-Config
        Write-Host "Removed NekoTunnel config."
    }
    default { Show-Usage }
}
