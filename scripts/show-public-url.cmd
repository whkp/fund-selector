@echo off
REM Print the public URL currently served by the Cloudflare tunnel.
REM Thin wrapper over url_tool.py so both entry points share one parser.
setlocal
pushd "%~dp0.." 2>nul
set "ROOT=%CD%"
popd

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo [!] Python venv not found: %ROOT%\.venv\Scripts\python.exe
  echo     Run scripts\start-public.cmd first.
  exit /b 1
)

"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\scripts\url_tool.py" --url-only
exit /b %ERRORLEVEL%
