@echo off
REM =============================================================
REM  Fund Compass - show the public address (double-click friendly)
REM
REM  Prints the live tunnel URL, checks that the backend and the
REM  tunnel are both up, verifies the address answers from the
REM  public internet, copies it to the clipboard, and opens a QR
REM  code for phone access.
REM =============================================================
chcp 65001 >nul
setlocal

REM Resolve the repo root from this script's own location.
pushd "%~dp0.." 2>nul
set "ROOT=%CD%"
popd

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo [!] Python venv not found: %ROOT%\.venv\Scripts\python.exe
  echo     Run scripts\start-public.cmd once to set things up.
  echo.
  pause
  exit /b 1
)

"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\scripts\url_tool.py"

echo.
pause
exit /b 0
