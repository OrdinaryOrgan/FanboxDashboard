$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $root 'start_dashboard.ps1'

& $startScript -Restart
