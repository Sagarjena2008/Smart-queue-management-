@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3 was not found.
    echo Install Python from https://www.python.org/downloads/ and select "Add Python to PATH".
    pause
    exit /b 1
)
python app.py
if errorlevel 1 pause
