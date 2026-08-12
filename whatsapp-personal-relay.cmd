@echo off
setlocal EnableExtensions
set "COMPANION=%~dp0companions\whatsapp-personal"
if not exist "%COMPANION%\node_modules" (
  echo Run: cd companions\whatsapp-personal ^&^& npm install 1>&2
  exit /b 2
)
pushd "%COMPANION%"
call npm start -- %*
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
