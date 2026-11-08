@echo off
REM stop.bat —— Kill Syncore by port (optionally Docker deps)
REM
REM Usage:
REM   stop.bat              kill both backend (8000) and frontend (5173)
REM   stop.bat 8000         kill backend only
REM   stop.bat 5173         kill frontend only
REM   stop.bat deps         kill app ports AND stop Docker dependencies
REM   stop.bat all          same as deps

cd /d "%~dp0"

if /i "%~1"=="deps" (
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\stop.ps1" -Deps
) else if /i "%~1"=="all" (
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\stop.ps1" -Deps
) else if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\stop.ps1"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\stop.ps1" -Port %1
)

echo.
echo 已执行完毕，按任意键关闭本窗口...
pause >nul
