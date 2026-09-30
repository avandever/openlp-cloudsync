@echo off
REM Double-click entry point for the OpenLP Cloud Sync installer.
REM Runs the PowerShell installer, bypassing the execution policy for this run only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-CloudSync.ps1" %*
if errorlevel 1 (
    echo.
    echo Install failed. Press any key to close.
    pause >nul
)
