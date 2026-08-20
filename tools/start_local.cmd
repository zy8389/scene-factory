@echo off
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0local_stack.ps1" -Action Start -OpenBrowser
if errorlevel 1 pause
