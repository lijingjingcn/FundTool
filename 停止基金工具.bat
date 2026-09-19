@echo off
rem ============================================
rem  Fund Info Tool stopper
rem  Stops the Streamlit server listening on
rem  port 8501 (e.g. one started in background).
rem  ASCII-only content (see launcher note).
rem ============================================
setlocal
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":8501" ^| findstr /c:"LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo Fund Info Tool server stopped (if it was running).
pause
