@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The project Python environment is missing.
    echo Open README.md and run the setup commands first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" bot.py
pause
