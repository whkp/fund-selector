@echo off
REM =============================================================
REM  Wrapper: run the FastAPI backend, output captured to logs\backend.log
REM
REM  This lives in its own file on purpose. "cmd /c" only strips its
REM  outer quote pair when the command contains no special characters,
REM  and the redirection below uses > and &. Inlining it into an
REM  already-quoted start command makes cmd mis-parse the whole line.
REM =============================================================
setlocal

if not exist "%~dp0..\logs" mkdir "%~dp0..\logs"

cd /d "%~dp0..\backend"
"%~dp0..\.venv\Scripts\python.exe" run.py 1>> "%~dp0..\logs\backend.log" 2>&1
