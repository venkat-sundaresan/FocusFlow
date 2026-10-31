@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set "FOCUSFLOW_PYTHON=%CD%\.venv\Scripts\python.exe"
) else (
    set "FOCUSFLOW_PYTHON=python"
)
echo Starting FocusFlow with "%FOCUSFLOW_PYTHON%"
"%FOCUSFLOW_PYTHON%" -u -X faulthandler -m focusflow %*
set "FOCUSFLOW_EXIT=%ERRORLEVEL%"
if "%FOCUSFLOW_EXIT%"=="0" exit /b 0
echo.
echo FocusFlow failed with exit code %FOCUSFLOW_EXIT%.
echo See the startup log path printed above, normally %%APPDATA%%\FocusFlow\focusflow.log.
echo Install dependencies with: "%FOCUSFLOW_PYTHON%" -m pip install --upgrade -r requirements.txt
echo Run diagnostics with: "%FOCUSFLOW_PYTHON%" -m focusflow --diagnose
if not defined FOCUSFLOW_NO_PAUSE pause
exit /b %FOCUSFLOW_EXIT%
