# Start the Smart Bill AI dev stack on Windows (backend + frontend) with one command.
# Usage:  .\run.ps1          normal start
#         .\run.ps1 -Seed    (re)load demo data before starting
# Stop:   Ctrl+C
#Requires -Version 5.1
[CmdletBinding()]
param([switch]$Seed)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$Python = Join-Path $BackendDir '.venv-win\Scripts\python.exe'

function Read-DotEnv([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith('#') -or $text.IndexOf('=') -lt 1) { continue }
        $i = $text.IndexOf('=')
        $key = $text.Substring(0, $i).Trim()
        $value = $text.Substring($i + 1).Trim()
        if ($value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $map[$key] = $value
    }
    return $map
}

$DotEnv = Read-DotEnv (Join-Path $Root '.env')

# The handoff keeps secrets outside the project tree. Mirror the backend's
# precedence: real environment > external secrets file > project .env > default.
$ConfiguredSecretsFile = $env:SMARTBILL_SECRETS_FILE
if ($ConfiguredSecretsFile) {
    $SecretsPath = if ([System.IO.Path]::IsPathRooted($ConfiguredSecretsFile)) {
        $ConfiguredSecretsFile
    } else {
        Join-Path $PWD $ConfiguredSecretsFile
    }
} else {
    $SecretsPath = Join-Path $HOME '.smartbill\handoff-secrets.env'
}
foreach ($entry in (Read-DotEnv $SecretsPath).GetEnumerator()) {
    $DotEnv[$entry.Key] = $entry.Value
}

function Get-Setting([string]$Name, [string]$Default) {
    $fromEnv = [Environment]::GetEnvironmentVariable($Name)
    if ($fromEnv) { return $fromEnv }
    if ($DotEnv.ContainsKey($Name) -and $DotEnv[$Name] -ne '') { return $DotEnv[$Name] }
    return $Default
}

foreach ($secretName in @('SECRET_KEY', 'RATE_LIMIT_SECRET')) {
    $secretValue = Get-Setting $secretName ''
    if (-not $secretValue -or
        $secretValue -match '^(change-me|changeme|dev-|replace-me|your-|example-)' -or
        $secretValue -match 'at-least-32|placeholder') {
        Write-Host "Missing or placeholder $secretName. Create $SecretsPath or set it in the environment/project .env." -ForegroundColor Red
        exit 1
    }
}

$BackendPort = [int](Get-Setting 'BACKEND_PORT' '8000')
$FrontendPort = [int](Get-Setting 'FRONTEND_PORT' '5173')
$OllamaUrl = (Get-Setting 'OLLAMA_URL' 'http://localhost:11434').TrimEnd('/')
# On some Windows setups "localhost" resolves to IPv6 first and every probe waits
# for the fallback; the loopback address answers immediately.
$OllamaProbeUrl = $OllamaUrl -replace '//localhost', '//127.0.0.1'
$OllamaModel = Get-Setting 'OLLAMA_MODEL' ''
$OllamaAnalysisModel = Get-Setting 'OLLAMA_ANALYSIS_MODEL' $OllamaModel
$OllamaLabel = 'not available'

# --- database (XAMPP MySQL) --------------------------------------------------
# When DATABASE_URL uses MySQL/MariaDB, make sure the XAMPP server is listening.
$DatabaseUrl = Get-Setting 'DATABASE_URL' ''
if ($DatabaseUrl -match '^(?:mysql|mariadb)(?:\+[A-Za-z0-9_-]+)?://') {
    $DbHost = '127.0.0.1'
    $DbPort = 3306
    if ($DatabaseUrl -match '://(?:[^@/]+@)?\[?([^:/\]@]+)\]?(?::(\d+))?/') {
        if ($Matches[1]) { $DbHost = $Matches[1] }
        if ($Matches[2]) { $DbPort = [int]$Matches[2] }
    }

    function Test-DbPort([string]$HostName, [int]$Port) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $task = $client.ConnectAsync($HostName, $Port)
            if (-not $task.Wait(700)) { return $false }
            return $client.Connected
        } catch { return $false } finally { $client.Dispose() }
    }

    if (-not (Test-DbPort $DbHost $DbPort)) {
        $Mysqld = Get-Setting 'XAMPP_MYSQLD' 'C:\xampp\mysql\bin\mysqld.exe'
        if (-not (Test-Path -LiteralPath $Mysqld)) {
            Write-Host "MySQL is not reachable on ${DbHost}:${DbPort} and XAMPP was not found at $Mysqld." -ForegroundColor Red
            Write-Host "Start MySQL from the XAMPP Control Panel, then run this again." -ForegroundColor Yellow
            exit 1
        }
        $MyIni = Join-Path (Split-Path -Parent $Mysqld) 'my.ini'
        Write-Host "Starting XAMPP MySQL on ${DbHost}:${DbPort} ..." -ForegroundColor Cyan
        Start-Process -FilePath $Mysqld -ArgumentList @("--defaults-file=$MyIni", '--standalone') -WindowStyle Hidden
        for ($i = 0; $i -lt 60; $i++) {
            if (Test-DbPort $DbHost $DbPort) { break }
            Start-Sleep -Milliseconds 500
        }
        if (-not (Test-DbPort $DbHost $DbPort)) {
            Write-Host "MySQL did not start within 30 seconds. Check the XAMPP log." -ForegroundColor Red
            exit 1
        }
        Write-Host "MySQL is ready." -ForegroundColor Green
    }
}

# --- prerequisites -----------------------------------------------------------
if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "Missing backend virtualenv ($Python)." -ForegroundColor Red
    Write-Host "Create it with:" -ForegroundColor Yellow
    Write-Host "  cd backend"
    Write-Host "  python -m venv .venv-win"
    Write-Host "  .\.venv-win\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $FrontendDir 'node_modules'))) {
    Write-Host "Missing frontend dependencies. Run: cd frontend; npm install" -ForegroundColor Red
    exit 1
}

# Put the portable OCR tools and Node.js on PATH for this process (and the children we start).
$TesseractCmd = Get-Setting 'TESSERACT_CMD' ''
if ($TesseractCmd -and (Test-Path -LiteralPath $TesseractCmd)) {
    $env:Path = (Split-Path -Parent $TesseractCmd) + ';' + $env:Path
}
$TessdataPrefix = Get-Setting 'TESSDATA_PREFIX' ''
if ($TessdataPrefix) { $env:TESSDATA_PREFIX = $TessdataPrefix }
$PopplerPath = Get-Setting 'POPPLER_PATH' ''
if ($PopplerPath -and (Test-Path -LiteralPath $PopplerPath)) {
    $env:Path = $PopplerPath + ';' + $env:Path
}
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue) -and
    (Test-Path -LiteralPath 'C:\Program Files\nodejs\npm.cmd')) {
    $env:Path = 'C:\Program Files\nodejs;' + $env:Path
}
$Npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $Npm) {
    Write-Host "npm was not found. Install Node.js LTS, then reopen this window." -ForegroundColor Red
    exit 1
}

function Test-PortBusy([int]$Port) {
    # Vite listens on ::1 and uvicorn on 127.0.0.1. An IPv4-only TcpClient misses
    # the frontend entirely, so prefer the TCP table which covers both families.
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -First 1
        return [bool]$listener
    }
    foreach ($address in @([System.Net.IPAddress]::Loopback, [System.Net.IPAddress]::IPv6Loopback)) {
        $socket = [System.Net.Sockets.Socket]::new(
            $address.AddressFamily,
            [System.Net.Sockets.SocketType]::Stream,
            [System.Net.Sockets.ProtocolType]::Tcp)
        try {
            $socket.Connect($address, $Port)
            return $true
        } catch {
        } finally {
            $socket.Dispose()
        }
    }
    return $false
}

function Get-PortOwner([int]$Port) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return 0
}

function Test-BackendOurs([int]$Port) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
        if ($health.status -eq 'ok') { return $true }
    } catch { }
    # Also catch a hung instance, but only when the command line proves it was
    # started from this project's virtualenv (never kill an unrelated program).
    $venv = [regex]::Escape((Join-Path $BackendDir '.venv-win\Scripts\python.exe'))
    $proc = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match 'uvicorn\s+app\.main:app' -and
        $_.CommandLine -match "--port\s+$Port(\s|$)" -and
        $_.CommandLine -match $venv
    } | Select-Object -First 1
    return [bool]$proc
}

function Test-FrontendOurs([int]$Port) {
    $owner = Get-PortOwner $Port
    if (-not $owner) { return $false }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue
    if (-not $proc -or -not $proc.CommandLine) { return $false }
    $frontend = [regex]::Escape($FrontendDir)
    return ($proc.CommandLine -match 'vite' -and $proc.CommandLine -match $frontend)
}

# Stop only processes that belong to this stack (the listener plus the uvicorn
# reloader above it or the npm/vite wrapper). A previous run.cmd window that was
# closed with X can leave these behind; the next start should replace them.
function Get-ProcessTreeIds([int[]]$RootIds) {
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    $ids = @{}
    foreach ($root in $RootIds) { $ids[$root] = $true }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($proc in $all) {
            if ($ids.ContainsKey([int]$proc.ParentProcessId) -and
                -not $ids.ContainsKey([int]$proc.ProcessId)) {
                $ids[[int]$proc.ProcessId] = $true
                $changed = $true
            }
        }
    }
    return @($ids.Keys)
}

function Stop-PortListeners([int]$Port, [string]$Pattern) {
    $roots = @()
    $listener = Get-PortOwner $Port
    if ($listener) { $roots += $listener }

    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.ProcessId -eq $PID) { return }
        $cmd = $_.CommandLine
        if ($cmd -and $cmd -match $Pattern -and $cmd -match "--port\s+$Port(\s|$)") {
            $roots += [int]$_.ProcessId
        }
    }
    if (-not $roots) { return }

    # uvicorn --reload serves from a multiprocessing child that does not mention
    # uvicorn in its command line, so include the whole tree below the roots.
    foreach ($procId in (Get-ProcessTreeIds $roots)) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}

function Wait-PortFree([int]$Port, [int]$Tries = 24) {
    for ($i = 0; $i -lt $Tries -and (Test-PortBusy $Port); $i++) {
        Start-Sleep -Milliseconds 250
    }
    return -not (Test-PortBusy $Port)
}

function Test-Ollama {
    try {
        Invoke-RestMethod -Uri "$OllamaProbeUrl/api/tags" -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

# --- Ollama ------------------------------------------------------------------
$OllamaProc = $null
if (-not (Test-Ollama)) {
    $OllamaExe = (Get-Command ollama.exe -ErrorAction SilentlyContinue).Source
    if (-not $OllamaExe) {
        $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
        if (Test-Path -LiteralPath $candidate) { $OllamaExe = $candidate }
    }
    if ($OllamaExe) {
        Write-Host "Starting Ollama..."
        $OllamaProc = Start-Process -FilePath $OllamaExe -ArgumentList 'serve' -NoNewWindow -PassThru
        for ($i = 0; $i -lt 20 -and -not (Test-Ollama); $i++) { Start-Sleep -Milliseconds 500 }
    }
}
if (Test-Ollama) {
    try {
        $tags = Invoke-RestMethod -Uri "$OllamaProbeUrl/api/tags" -TimeoutSec 5
        $models = @($tags.models | ForEach-Object { $_.name })
        if ($OllamaModel) {
            $modelsReady = $true
            if ($models -notcontains $OllamaModel) {
                Write-Host "Warning: the configured chat model is not downloaded. Run: ollama pull $OllamaModel" -ForegroundColor Yellow
                $modelsReady = $false
            }
            if ($OllamaAnalysisModel -and ($models -notcontains $OllamaAnalysisModel)) {
                Write-Host "Warning: the configured analysis model is not downloaded. Run: ollama pull $OllamaAnalysisModel" -ForegroundColor Yellow
                $modelsReady = $false
            }
            if ($modelsReady) { $OllamaLabel = 'ready' }
        } elseif ($models.Count -gt 0) {
            $OllamaLabel = 'ready'
        } else {
            Write-Host "Warning: no Smart Bill AI model is installed. Run: ollama pull <model>" -ForegroundColor Yellow
        }
    } catch { }
} else {
    Write-Host "Note: Ollama is not reachable - uploads use the rule parser instead of the AI." -ForegroundColor Yellow
    Write-Host "      Install/start Ollama, then pull a small model with: ollama pull <model>" -ForegroundColor Yellow
}

# --- ports -------------------------------------------------------------------
# If an earlier run.cmd window was closed without stopping its children, the
# stack may still be answering. Restart it instead of refusing to start.
if ((Test-PortBusy $BackendPort) -and (Test-BackendOurs $BackendPort)) {
    Write-Host "Smart Bill AI backend is already running on :$BackendPort - restarting it..." -ForegroundColor Yellow
    Stop-PortListeners $BackendPort 'uvicorn\s+app\.main:app'
    if (-not (Wait-PortFree $BackendPort)) {
        Write-Host "Could not stop the old backend on :$BackendPort. Stop it manually and try again." -ForegroundColor Red
        exit 1
    }
}
if (Test-PortBusy $BackendPort) {
    $owner = Get-PortOwner $BackendPort
    $who = if ($owner) { " (PID $owner)" } else { "" }
    Write-Host "Port $BackendPort is in use by another program$who. Stop it or set BACKEND_PORT in .env." -ForegroundColor Red
    exit 1
}
if ((Test-PortBusy $FrontendPort) -and (Test-FrontendOurs $FrontendPort)) {
    Write-Host "Smart Bill AI frontend is already running on :$FrontendPort - restarting it..." -ForegroundColor Yellow
    Stop-PortListeners $FrontendPort 'vite'
    if (-not (Wait-PortFree $FrontendPort)) {
        Write-Host "Could not stop the old frontend on :$FrontendPort. Stop it manually and try again." -ForegroundColor Red
        exit 1
    }
}
if (Test-PortBusy $FrontendPort) {
    $owner = Get-PortOwner $FrontendPort
    $who = if ($owner) { " (PID $owner)" } else { "" }
    Write-Host "Port $FrontendPort is in use by another program$who. Stop it or set FRONTEND_PORT in .env." -ForegroundColor Red
    exit 1
}

$BackendProc = $null
$FrontendProc = $null
$Stopping = $false

function Stop-Tree($ProcessId) {
    if (-not $ProcessId) { return }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $proc) { return }
    # Never kill a recycled PID that now belongs to an unrelated program.
    if ($proc.CommandLine -and $proc.CommandLine -notmatch 'uvicorn\s+app\.main:app|vite|npm') { return }
    cmd /d /c "taskkill /PID $ProcessId /T /F >nul 2>&1" | Out-Null
}

function Stop-Stack {
    if ($script:Stopping) { return }
    $script:Stopping = $true
    Write-Host ""
    Write-Host "Shutting down..."
    Stop-Tree $script:FrontendProc.Id
    Stop-Tree $script:BackendProc.Id
    if ($script:OllamaProc) { Stop-Tree $script:OllamaProc.Id }
    Write-Host "Stopped."
}

try {
    if ($Seed) {
        Write-Host "Seeding demo data..."
        Push-Location $BackendDir
        try { & $Python -m scripts.seed } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw "Seeding failed (exit code $LASTEXITCODE)." }
    }

    Write-Host "Starting backend on :$BackendPort ..."
    $BackendProc = Start-Process -FilePath $Python `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--reload', '--port', "$BackendPort") `
        -WorkingDirectory $BackendDir -NoNewWindow -PassThru

    Write-Host -NoNewline "Waiting for API"
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ($BackendProc.HasExited) {
            Write-Host ""
            Write-Host "Backend failed to start. See the log above." -ForegroundColor Red
            exit 1
        }
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$BackendPort/api/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch {
            Write-Host -NoNewline "."
            Start-Sleep -Seconds 1
        }
    }
    if (-not $ready) {
        Write-Host ""
        Write-Host "Backend did not become ready in time." -ForegroundColor Red
        exit 1
    }
    Write-Host " ready."

    Write-Host "Starting frontend on :$FrontendPort ..."
    $env:VITE_PORT = "$FrontendPort"
    $env:VITE_API_PROXY = "http://localhost:$BackendPort"
    $FrontendProc = Start-Process -FilePath $env:ComSpec `
        -ArgumentList @('/d', '/c', "`"$Npm`" run dev -- --port $FrontendPort --strictPort") `
        -WorkingDirectory $FrontendDir -NoNewWindow -PassThru

    # Give Vite time to bind before the health monitor starts, otherwise the
    # monitor would mistake slow startup for a crash and shut the stack down.
    for ($i = 0; $i -lt 120 -and -not (Test-PortBusy $FrontendPort); $i++) {
        Start-Sleep -Milliseconds 500
    }

    Write-Host ""
    Write-Host "  App      : http://localhost:$FrontendPort"
    Write-Host "  API docs : http://localhost:$BackendPort/api/docs"
    Write-Host "  Login    : demo@smartbill.ai / Demo@12345 (only if you seeded demo data)"
    Write-Host "  Smart Bill AI : $OllamaLabel (answers stream as they are written)"
    Write-Host ""
    Write-Host "Press Ctrl+C to stop everything."

    # npm/vite wrappers can exit while the servers keep running, so watch the
    # actual ports instead of only the launcher PIDs. Require a long, continuous
    # outage before giving up, so a slow moment never stops a working stack.
    $backendDown = 0
    $frontendDown = 0
    while ($true) {
        Start-Sleep -Seconds 2
        if (Test-PortBusy $BackendPort) { $backendDown = 0 } else { $backendDown++ }
        if (Test-PortBusy $FrontendPort) { $frontendDown = 0 } else { $frontendDown++ }
        if ($backendDown -ge 15 -or $frontendDown -ge 15) { break }
    }
    Write-Host "A service stopped unexpectedly. See the log above." -ForegroundColor Yellow
} finally {
    Stop-Stack
}
