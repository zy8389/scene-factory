@echo off
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0local_stack.ps1" -Action Stop
if errorlevel 1 pause
