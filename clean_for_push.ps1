$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$healthUrl = 'http://127.0.0.1:8000/health'

function Test-BackendHealth {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Get-BackendPortPids {
    return @(
        Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    ) | Where-Object { $_ }
}

function Stop-BackendIfRunning {
    if (-not (Test-BackendHealth)) {
        return
    }

    $pids = Get-BackendPortPids
    foreach ($processId in $pids) {
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
        }
        catch {
        }
    }

    Start-Sleep -Milliseconds 800
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
    (Join-Path $root 'backend\.pytest_cache'),
    (Join-Path $root 'frontend\dist'),
    (Join-Path $root 'frontend\node_modules'),
    (Join-Path $root '.pytest_cache'),
    (Join-Path $root '.mypy_cache'),
    (Join-Path $root '.skill-install-tmp'),
    (Join-Path $root 'findings.md'),
    (Join-Path $root 'progress.md'),
    (Join-Path $root 'task_plan.md'),
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
Remove-TargetsByPattern -BasePath $root -Filter '*.sqlite3'
Remove-TargetsByPattern -BasePath $root -Filter '*.db'

Write-Host ''
Write-Host 'Repository cleanup completed.'
Write-Host 'You can now review git status and prepare the push.'
