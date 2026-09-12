@echo off
REM Print the public URL currently served by the Cloudflare tunnel.
REM Reads the most recent trycloudflare.com line from the tunnel log.
setlocal enabledelayedexpansion
set "LOG=C:\Users\hkp\fund-selector\logs\tunnel.log"

if not exist "%LOG%" (
  echo [!] %LOG% not found. Run scripts\start-public.cmd first.
  exit /b 1
)

set "LAST="
for /f "delims=" %%L in ('findstr /C:"trycloudflare.com" "%LOG%"') do set "LAST=%%L"

if not defined LAST (
  echo [!] No URL logged yet. The tunnel may still be starting - retry in about 15 seconds.
  exit /b 1
)

set "LAST=!LAST:*https://=!"
for /f "tokens=1" %%U in ("!LAST!") do set "HOST=%%U"

echo.
echo   Fund Compass public URL:
echo     https://!HOST!
echo.
exit /b 0
