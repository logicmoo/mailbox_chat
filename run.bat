@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
set "PYTHON_EXE=python"
if exist "%ROOT%.venv\Scripts\python.exe" set "PYTHON_EXE=%ROOT%.venv\Scripts\python.exe"
title Mailbox Channel Relay Bridging Proxy 46667
cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"
"%PYTHON_EXE%" -m mailbox_channel_relay_bridging_proxy.server %*
