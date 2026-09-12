@echo off
REM =============================================================
REM  Fund Compass - show the current registration invite code
REM
REM  Thin wrapper over invite_code_tool.py so the parsing lives in
REM  one place. Safe to double-click.
REM =============================================================
chcp 65001 >nul
setlocal

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

"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\scripts\invite_code_tool.py"

echo.
pause
exit /b 0
