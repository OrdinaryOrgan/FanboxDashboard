param(
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'
$frontendDist = Join-Path $frontendDir 'dist\index.html'
$appUrl = 'http://127.0.0.1:8000/'
$healthUrl = 'http://127.0.0.1:8000/health'
$preferredPython = 'C:\Users\Kotorin\AppData\Local\Programs\Python\Python312\python.exe'

function Ensure-FrontendBuild {
    if (Test-Path $frontendDist) {
        return
    }

    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        throw 'npm was not found in PATH, so the frontend build could not be created.'
    }

    Push-Location $frontendDir
    try {
        if (-not (Test-Path 'node_modules')) {
            & $npm.Source install
            if ($LASTEXITCODE -ne 0) {
                throw 'npm install failed while preparing the frontend.'
            }
        }

        & $npm.Source run build
        if ($LASTEXITCODE -ne 0) {
            throw 'npm run build failed while preparing the frontend.'
        }
    }
    finally {
        Pop-Location
    }
}

function Test-BackendHealth {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Wait-BackendHealth {
    param(
        [bool]$DesiredState,
        [int]$Attempts = 40,
        [int]$DelayMs = 500
    )

    foreach ($attempt in 1..$Attempts) {
        $healthy = Test-BackendHealth
        if ($healthy -eq $DesiredState) {
            return $true
        }
        Start-Sleep -Milliseconds $DelayMs
    }

    return $false
}

function Get-BackendPortPids {
    return @(
        Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    ) | Where-Object { $_ }
}

function Resolve-PythonLauncher {
    if (Test-Path $preferredPython) {
        return @{
            FilePath = $preferredPython
            Arguments = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @{
            FilePath = $python.Source
            Arguments = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
        }
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @{
            FilePath = $py.Source
            Arguments = @('-3', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
        }
    }

    throw 'Python not found in PATH.'
}

function Start-Backend {
    $launcher = Resolve-PythonLauncher
    Start-Process -FilePath $launcher.FilePath -WorkingDirectory $backendDir -ArgumentList $launcher.Arguments -WindowStyle Hidden

    if (-not (Wait-BackendHealth -DesiredState $true -Attempts 40 -DelayMs 500)) {
        throw 'Backend failed to start within the expected time.'
    }
}

function Stop-Backend {
    $pids = Get-BackendPortPids
    if (-not $pids -or $pids.Count -eq 0) {
        return
    }

    foreach ($processId in $pids) {
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
        }
        catch {
        }
    }

    Start-Sleep -Milliseconds 500
    [void](Wait-BackendHealth -DesiredState $false -Attempts 20 -DelayMs 250)
}

Ensure-FrontendBuild

if ($Restart) {
    Stop-Backend
    Start-Backend
}
elseif (-not (Test-BackendHealth)) {
    Start-Backend
}

Start-Process $appUrl
