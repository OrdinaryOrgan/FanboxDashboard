@echo off
setlocal

set "ROOT=%~dp0"
powershell -ExecutionPolicy Bypass -File "%ROOT%reset_local_env.ps1"

endlocal
