@echo off
setlocal
cd /d "%~dp0..\.."

set "STATUS_PYTHON=%PRODUCTION_STATUS_PYTHON%"
if not defined STATUS_PYTHON set "STATUS_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%STATUS_PYTHON%" set "STATUS_PYTHON=python"

"%STATUS_PYTHON%" -c "import lark_oapi, pandas, streamlit" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The selected Python environment is missing lark_oapi, pandas, or streamlit.
    echo Python: %STATUS_PYTHON%
    echo Set PRODUCTION_STATUS_PYTHON to the Python executable used by production_monitor.py.
    pause
    exit /b 1
)

echo Production Status collector Python: %STATUS_PYTHON%
"%STATUS_PYTHON%" -m produce_monitor.production_status.collector %*
if errorlevel 1 pause
