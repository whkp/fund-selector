@echo off
REM =============================================================
REM  Fund Compass - start local backend + public Cloudflare tunnel
REM
REM  Double-click to start (or restart) the service. There is no
REM  boot/logon entry by design, so this is the normal way to bring
REM  it back up after a reboot.
REM
REM  Idempotent: each step is skipped when the thing is already up,
REM  so running this twice is harmless.
REM
REM  Output:
REM    logs\launcher.log  - what the launcher decided to do
REM    logs\backend.log   - uvicorn / FastAPI output
REM    logs\tunnel.log    - cloudflared output; the public URL is logged here
REM =============================================================
setlocal

REM Resolve the repository root from this script's own location, so the file
REM stays portable and carries no hard-coded user path.
pushd "%~dp0.." 2>nul
set "ROOT=%CD%"
popd

REM Actual launch commands and their log redirection live in run-backend.cmd
REM and run-tunnel.cmd, invoked below.
set "LOGDIR=%ROOT%\logs"
set "PORT=8080"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo [%DATE% %TIME%] launcher start >> "%LOGDIR%\launcher.log"

REM ---- 1) backend: skip when the port is already served ----
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 (
  echo [%DATE% %TIME%] starting backend on %PORT% >> "%LOGDIR%\launcher.log"
  start "fund-compass-api" /min cmd /c "%~dp0run-backend.cmd"
) else (
  echo [%DATE% %TIME%] backend already listening on %PORT%, skipped >> "%LOGDIR%\launcher.log"
)

REM ---- 2) give uvicorn a moment to bind before the tunnel dials it ----
timeout /t 8 /nobreak >nul

REM ---- 3) tunnel: skip when cloudflared is already running ----
tasklist /FI "IMAGENAME eq cloudflared.exe" 2>nul | findstr /I "cloudflared.exe" >nul
if errorlevel 1 (
  echo [%DATE% %TIME%] starting cloudflare tunnel >> "%LOGDIR%\launcher.log"
  start "cloudflared-tunnel" /min cmd /c "%~dp0run-tunnel.cmd"
) else (
  echo [%DATE% %TIME%] cloudflared already running, skipped >> "%LOGDIR%\launcher.log"
)

exit /b 0
