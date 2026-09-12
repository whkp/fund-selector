@echo off
REM =============================================================
REM  Wrapper: run the Cloudflare quick tunnel, output to logs\tunnel.log
REM
REM  Same reason as run-backend.cmd for living in a separate file:
REM  the redirection operators cannot be inlined into a quoted
REM  "cmd /c" command without breaking cmd's quote stripping.
REM
REM  The public https://<random>.trycloudflare.com URL is printed into
REM  logs\tunnel.log; scripts\show-public-url.cmd reads it back out.
REM =============================================================
setlocal
set "PORT=8080"

if not exist "%~dp0..\logs" mkdir "%~dp0..\logs"

"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:%PORT% --no-autoupdate 1>> "%~dp0..\logs\tunnel.log" 2>&1
