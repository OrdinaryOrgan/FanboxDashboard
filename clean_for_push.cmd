@echo off
setlocal

set "ROOT=%~dp0"
powershell -ExecutionPolicy Bypass -File "%ROOT%clean_for_push.ps1"

endlocal
