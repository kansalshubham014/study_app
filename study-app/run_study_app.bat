@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Create it with: python -m venv .venv
    pause
    exit /b 1
)

curl.exe -fsS "http://127.0.0.1:8000/health" >nul 2>&1
if not errorlevel 1 goto open_app

start "Study Desk server" /min "%CD%\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

for /L %%I in (1,1,30) do (
    timeout /t 1 /nobreak >nul
    curl.exe -fsS "http://127.0.0.1:8000/health" >nul 2>&1
    if not errorlevel 1 goto open_app
)

echo Study Desk did not start. Check the minimized server window for details.
pause
exit /b 1

:open_app
start "" "http://127.0.0.1:8000"
exit /b 0
