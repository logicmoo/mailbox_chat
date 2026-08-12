@echo off
setlocal EnableExtensions
set "RELAY_ROOT=%~dp0"
set "RELAY_PYTHON=python"
if exist "%RELAY_ROOT%.venv\Scripts\python.exe" set "RELAY_PYTHON=%RELAY_ROOT%.venv\Scripts\python.exe"
title Mailbox Channel Relay Bridging Proxy 46667
set "PYTHONPATH=%RELAY_ROOT%src;%PYTHONPATH%"
"%RELAY_PYTHON%" -m mailbox_channel_relay_bridging_proxy.server %*
exit /b %ERRORLEVEL%
