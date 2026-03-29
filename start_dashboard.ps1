$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'
$frontendDist = Join-Path $frontendDir 'dist\index.html'
$appUrl = 'http://127.0.0.1:8000/'
$healthUrl = 'http://127.0.0.1:8000/health'

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

Ensure-FrontendBuild

$existing = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $existing) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    $launcher = $null
    $arguments = @()

    if ($python) {
        $launcher = $python.Source
        $arguments = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
    }
    else {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            $launcher = $py.Source
            $arguments = @('-3', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
        }
    }

    if (-not $launcher) {
        throw 'Python not found in PATH.'
    }

    Start-Process -FilePath $launcher -WorkingDirectory $backendDir -ArgumentList $arguments -WindowStyle Hidden

    $ok = $false
    foreach ($attempt in 1..40) {
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ok = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }

    if (-not $ok) {
        throw 'Backend failed to start within the expected time.'
    }
}

Start-Process $appUrl
