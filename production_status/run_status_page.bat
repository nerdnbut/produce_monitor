@echo off
setlocal
cd /d "%~dp0..\.."

set "STATUS_PYTHON=%PRODUCTION_STATUS_PYTHON%"
if not defined STATUS_PYTHON set "STATUS_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%STATUS_PYTHON%" set "STATUS_PYTHON=python"

"%STATUS_PYTHON%" -c "import streamlit" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The selected Python environment is missing streamlit.
    echo Python: %STATUS_PYTHON%
    echo Set PRODUCTION_STATUS_PYTHON to the Python executable used by production_monitor.py.
    pause
    exit /b 1
)

echo Production Status page Python: %STATUS_PYTHON%
"%STATUS_PYTHON%" -m streamlit run produce_monitor/production_status/status_page.py --server.port 8502 --server.headless true
if errorlevel 1 pause
