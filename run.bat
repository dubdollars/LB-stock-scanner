@echo off
REM ============================================================
REM  Minervini SEPA Scanner -- one-click launcher
REM  Double-click this file to start the dashboard.
REM ============================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ========================================================
echo   Minervini SEPA Scanner
echo ========================================================
echo.

REM --- Check Python ---
where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON

REM --- Check for venv ---
if exist ".venv\Scripts\python.exe" goto VENV_OK

REM --- First-run setup ---
echo [Setup] First run detected. Creating virtual environment...
python -m venv .venv
if errorlevel 1 goto VENV_FAIL

echo [Setup] Installing dependencies. This takes 3-5 minutes the first time.
echo.
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto INSTALL_FAIL

echo.
echo [Setup] Done. Future launches will skip this step.
echo.
goto LAUNCH

:VENV_OK
call ".venv\Scripts\activate.bat"
goto LAUNCH

:LAUNCH
echo.
echo ========================================================
echo   Starting dashboard at http://localhost:8501
echo   Press Ctrl+C in this window to stop the app.
echo ========================================================
echo.

REM Open browser after a short delay
start "" cmd /c "timeout /t 4 /nobreak >nul && start http://localhost:8501"

streamlit run app.py --server.headless true --server.port 8501 --browser.gatherUsageStats false

echo.
echo Streamlit stopped.
pause
exit /b 0

:NO_PYTHON
echo [ERROR] Python is not installed or not on PATH.
echo.
echo Install Python 3.11 or newer from:
echo     https://www.python.org/downloads/
echo.
echo During install, CHECK the box that says
echo "Add python.exe to PATH" or "Add Python to environment variables".
echo Then close this window and double-click run.bat again.
echo.
pause
exit /b 1

:VENV_FAIL
echo [ERROR] Failed to create the virtual environment.
echo Try running this command manually to see the error:
echo     python -m venv .venv
echo.
pause
exit /b 1

:INSTALL_FAIL
echo [ERROR] Dependency install failed.
echo Try running these commands manually to see the error:
echo     .venv\Scripts\activate.bat
echo     pip install -r requirements.txt
echo.
pause
exit /b 1
