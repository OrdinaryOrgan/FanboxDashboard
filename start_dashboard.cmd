@echo off
setlocal

cd /d "%~dp0"

set "APP_URL=http://127.0.0.1:8000/"
set "HEALTH_URL=http://127.0.0.1:8000/health"
set "BACKEND_DIR=%~dp0backend"
set "PYTHON_CMD="

for /f %%P in ('powershell -NoProfile -Command "(Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)"') do (
  set "BACKEND_PID=%%P"
)

if not defined BACKEND_PID (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set "PYTHON_CMD=python"
  ) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
      set "PYTHON_CMD=py -3"
    )
  )

  if not defined PYTHON_CMD (
    echo Python not found in PATH.
    echo Please install Python or add it to PATH, then try again.
    pause
    exit /b 1
  )

  start "Fanbox Dashboard Backend" /min cmd /c "cd /d ""%BACKEND_DIR%"" && %PYTHON_CMD% -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

  powershell -NoProfile -Command ^
    "$ProgressPreference = 'SilentlyContinue';" ^
    "$ok = $false;" ^
    "1..40 | ForEach-Object {" ^
    "  try {" ^
    "    $response = Invoke-WebRequest -Uri '%HEALTH_URL%' -UseBasicParsing -TimeoutSec 2;" ^
    "    if ($response.StatusCode -eq 200) { $ok = $true; break }" ^
    "  } catch {}" ^
    "  Start-Sleep -Milliseconds 500" ^
    "};" ^
    "if (-not $ok) { exit 1 }"

  if errorlevel 1 (
    echo Backend failed to start within the expected time.
    pause
    exit /b 1
  )
)

start "" "%APP_URL%"
exit /b 0
