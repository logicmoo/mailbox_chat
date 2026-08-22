@echo off
setlocal EnableExtensions
rem Ask the running mailbox server to restart, reloading code you just edited.
rem Requires the server to be running under mailbox-server-loop so a fresh
rem process is relaunched. Either you or an assistant can run this.
rem Usage: mailbox-restart.cmd [BASE_URL]   (default http://127.0.0.1:46667)
set "URL=%~1"
if "%URL%"=="" set "URL=http://127.0.0.1:46667"
if defined MAILBOX_RELAY_TOKEN (
  curl -sS -X POST -H "Authorization: ****** %MAILBOX_RELAY_TOKEN%" "%URL%/api/restart"
) else (
  curl -sS -X POST "%URL%/api/restart"
)
echo.
