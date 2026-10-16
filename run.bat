@echo off
cd /d "%~dp0"

set "PY=C:\Users\fhz11\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" main.py
if errorlevel 1 (
    echo.
    echo SSLTool failed to start. Please check the error above.
    echo You may need to run: pip install -r requirements.txt
    pause
)
