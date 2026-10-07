@echo off
title NSE Tools Python Algo - Multi-Broker Edition
cd /d "%~dp0"

echo Cleaning up any old algo processes...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8005 ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)
wmic process where "name='python.exe' and commandline like '%%launcher.py%%'" call terminate >nul 2>&1

echo Starting Algo Server on http://127.0.0.1:8005...

REM Start browser after 2 seconds
start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:8005"

REM Start server
python -m uvicorn main:app --host 127.0.0.1 --port 8005

pause
