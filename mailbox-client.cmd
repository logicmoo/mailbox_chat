@echo off
setlocal EnableExtensions
set "RELAY_ROOT=%~dp0"
set "RELAY_PYTHON=python"
if exist "%RELAY_ROOT%.venv\Scripts\python.exe" set "RELAY_PYTHON=%RELAY_ROOT%.venv\Scripts\python.exe"
set "PYTHONPATH=%RELAY_ROOT%src;%PYTHONPATH%"
"%RELAY_PYTHON%" -m mailbox_channels.agent_mailbox %*
exit /b %ERRORLEVEL%
