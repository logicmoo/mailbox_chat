@echo off
setlocal EnableExtensions
rem Ask the running mailbox server, and the mailbox-server-loop supervising it,
rem to stop (the server returns SHUTDOWN_EXIT_CODE 42 so the loop quits instead
rem of relaunching). Either you or an assistant can run this.
rem Usage: mailbox-stop.cmd [BASE_URL]   (default http://127.0.0.1:46667)
set "URL=%~1"
if "%URL%"=="" set "URL=http://127.0.0.1:46667"
if defined MAILBOX_RELAY_TOKEN (
  curl -sS -X POST -H "Authorization: ****** %MAILBOX_RELAY_TOKEN%" "%URL%/api/shutdown"
) else (
  curl -sS -X POST "%URL%/api/shutdown"
)
echo.
