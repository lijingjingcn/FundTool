@echo off
chcp 65001 >nul
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10 及以上版本：https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import streamlit, pdfplumber, requests" >nul 2>nul
if errorlevel 1 (
    echo 首次运行，正在安装依赖（约 1-2 分钟）...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
)

set STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
echo 正在启动基金信息查询工具，浏览器将自动打开...
python -m streamlit run app.py
pause
