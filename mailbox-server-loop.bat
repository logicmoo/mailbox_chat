@echo off
setlocal EnableExtensions
set "RELAY_ROOT=%~dp0"
set "RELAY_PYTHON=python"
if exist "%RELAY_ROOT%.venv\Scripts\python.exe" set "RELAY_PYTHON=%RELAY_ROOT%.venv\Scripts\python.exe"
set "PYTHONPATH=%RELAY_ROOT%src;%PYTHONPATH%"
if "%RELAY_RESTART_DELAY%"=="" set "RELAY_RESTART_DELAY=2"
title Mailbox Server 46667 (loop)
rem The restart loop lives in PowerShell so R/Enter restarts the server (reload
rem your edited code) and Ctrl+C quits instantly, without cmd.exe's "Terminate
rem batch job (Y/N)?" prompt. Killing the server process (Task Manager /
rem Stop-Process) also makes the loop relaunch the updated code.
powershell -NoProfile -ExecutionPolicy Bypass -File "%RELAY_ROOT%mailbox-server-loop.ps1" %*
exit /b %ERRORLEVEL%
