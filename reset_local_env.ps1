$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$healthUrl = 'http://127.0.0.1:8000/health'
$backendPort = 8000

function Test-BackendHealth {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Get-ListeningPidsOnPort {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    return @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    ) | Where-Object { $_ }
}

function Get-ProcessCommandLine {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        return $process.CommandLine
    }
    catch {
        return $null
    }
}

function Test-IsProjectBackendProcess {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    $commandLine = Get-ProcessCommandLine -ProcessId $ProcessId
    if (-not $commandLine) {
        return $false
    }

    $normalized = $commandLine.ToLowerInvariant()
    return $normalized.Contains('app.main:app') -and $normalized.Contains("--port $backendPort")
}

function Get-ProjectBackendPids {
    return @(Get-ListeningPidsOnPort -Port $backendPort | Where-Object { Test-IsProjectBackendProcess -ProcessId $_ })
}

function Wait-BackendStopped {
    param(
        [int]$Attempts = 20,
        [int]$DelayMs = 300
    )

    foreach ($attempt in 1..$Attempts) {
        if ((Get-ProjectBackendPids).Count -eq 0 -and -not (Test-BackendHealth)) {
            return $true
        }

        Start-Sleep -Milliseconds $DelayMs
    }

    return $false
}

function Stop-BackendIfRunning {
    $listeningPids = @(Get-ListeningPidsOnPort -Port $backendPort)
    if (-not $listeningPids -or $listeningPids.Count -eq 0) {
        return
    }

    $projectPids = @(Get-ProjectBackendPids)
    if (-not $projectPids -or $projectPids.Count -eq 0) {
        if (Test-BackendHealth) {
            Write-Warning "Port $backendPort is serving /health, but the owning process does not match the expected project backend command line. Skipping stop."
        }
        return
    }

    foreach ($processId in $projectPids) {
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Write-Host "Stopped backend process $processId"
        }
        catch {
            Write-Warning "Failed to stop backend process ${processId}: $($_.Exception.Message)"
        }
    }

    [void](Wait-BackendStopped)
}

function Remove-TargetPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    if (Test-Path -LiteralPath $LiteralPath) {
        Remove-Item -LiteralPath $LiteralPath -Recurse -Force -ErrorAction Stop
        Write-Host "Removed $LiteralPath"
    }
}

function Remove-TargetsByPattern {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BasePath,
        [Parameter(Mandatory = $true)]
        [string]$Filter
    )

    if (-not (Test-Path -LiteralPath $BasePath)) {
        return
    }

    Get-ChildItem -LiteralPath $BasePath -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { -not $_.PSIsContainer -and $_.Name -like $Filter } |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            Write-Host "Removed $($_.FullName)"
        }
}

function Remove-DirectoriesByName {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BasePath,
        [Parameter(Mandatory = $true)]
        [string]$DirectoryName
    )

    if (-not (Test-Path -LiteralPath $BasePath)) {
        return
    }

    Get-ChildItem -LiteralPath $BasePath -Recurse -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq $DirectoryName } |
        Sort-Object FullName -Descending |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host "Removed $($_.FullName)"
        }
}

Stop-BackendIfRunning

$pathsToRemove = @(
    (Join-Path $root 'backend\data'),
    (Join-Path $root 'backend\tests\data'),
    (Join-Path $root 'backend\.pytest_cache'),
    (Join-Path $root 'frontend\dist'),
    (Join-Path $root '.pytest_cache'),
    (Join-Path $root '.mypy_cache'),
    (Join-Path $root '.skill-install-tmp'),
    (Join-Path $root 'coverage.xml'),
    (Join-Path $root '.coverage')
)

foreach ($target in $pathsToRemove) {
    Remove-TargetPath -LiteralPath $target
}

Remove-DirectoriesByName -BasePath $root -DirectoryName '__pycache__'
Remove-TargetsByPattern -BasePath $root -Filter '*.pyc'
Remove-TargetsByPattern -BasePath $root -Filter '*.pyo'
Remove-TargetsByPattern -BasePath $root -Filter '*.pyd'
Remove-TargetsByPattern -BasePath $root -Filter '*.log'

Write-Host ''
Write-Host 'Repository cleanup completed.'
Write-Host 'Local runtime state and caches were removed.'
