@echo off
REM =============================================================
REM  Wrapper: run the NAMED Cloudflare tunnel, output to logs\tunnel.log
REM
REM  Switched from the anonymous quick tunnel (random *.trycloudflare.com
REM  URL, no SLA) to a named tunnel bound to our own domain:
REM
REM    https://funds.kpcode.xyz  ->  http://127.0.0.1:8080
REM
REM  Tunnel identity lives in %USERPROFILE%\.cloudflared\ :
REM    cert.pem        - origin certificate (cloudflared tunnel login)
REM    <tunnel-id>.json- tunnel credentials (cloudflared tunnel create)
REM    config.yml      - ingress rules (hostname -> local service)
REM
REM  The DNS CNAME record was created once via:
REM    cloudflared tunnel route dns fund-compass funds.kpcode.xyz
REM
REM  Same reason as run-backend.cmd for living in a separate file:
REM  the redirection operators cannot be inlined into a quoted
REM  "cmd /c" command without breaking cmd's quote stripping.
REM =============================================================
setlocal

if not exist "%~dp0..\logs" mkdir "%~dp0..\logs"

"C:\Program Files (x86)\cloudflared\cloudflared.exe" --no-autoupdate tunnel run fund-compass 1>> "%~dp0..\logs\tunnel.log" 2>&1
