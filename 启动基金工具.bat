@echo off
rem ============================================
rem  Fund Info Tool launcher
rem  NOTE: keep this file ASCII-only. Chinese text
rem  breaks cmd.exe parsing due to codepage issues.
rem ============================================
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ first:
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import streamlit, pdfplumber, requests" >nul 2>nul
if errorlevel 1 (
    echo First run: installing dependencies, please wait 1-2 minutes...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies. Check network and retry.
        pause
        exit /b 1
    )
)

echo Starting Fund Info Tool...
echo Browser will open at http://localhost:8501 automatically.
echo Keep this window open while using the tool. Press Ctrl+C here to stop.
rem open browser after a short delay (server needs a few seconds to start)
start "" cmd /c "timeout /t 6 /nobreak >nul & start http://localhost:8501"
python -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
pause
