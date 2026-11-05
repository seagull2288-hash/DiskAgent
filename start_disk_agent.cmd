@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_disk_agent.ps1"
if errorlevel 1 pause
