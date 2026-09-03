@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
set "VENV_PYTHON=%ROOT%.venv\Scripts\python.exe"
set "FAIL_REASON="
set "BACKEND_START_FAILED="
set "FRONTEND_START_FAILED="

if not exist "%VENV_PYTHON%" (
    set "FAIL_REASON=.venv is missing or incomplete. Run setup.bat first."
    goto :failure
)
if not exist "%ROOT%frontend\node_modules" (
    set "FAIL_REASON=frontend\node_modules is missing. Run setup.bat first."
    goto :failure
)
if not exist "%ROOT%frontend\package.json" (
    set "FAIL_REASON=frontend\package.json was not found."
    goto :failure
)

echo AirTrace local runtime
if not exist "%ROOT%.env" echo WARNING: .env is missing. Run setup.bat or create it from .env.example if LIVE mode is needed.
echo.
echo Starting backend and frontend in separate windows ...
start "AirTrace Backend" /D "%ROOT%" "%ComSpec%" /k ""%VENV_PYTHON%" -m uvicorn airtrace.api.app:app --reload"
if errorlevel 1 set "BACKEND_START_FAILED=1"
start "AirTrace Frontend" /D "%ROOT%frontend" "%ComSpec%" /k "npm run dev"
if errorlevel 1 set "FRONTEND_START_FAILED=1"

if defined BACKEND_START_FAILED if defined FRONTEND_START_FAILED set "FAIL_REASON=Could not start either the backend or frontend window."
if defined BACKEND_START_FAILED if not defined FRONTEND_START_FAILED set "FAIL_REASON=Could not start the backend window. The frontend window was started."
if not defined BACKEND_START_FAILED if defined FRONTEND_START_FAILED set "FAIL_REASON=Could not start the frontend window. The backend window was started."
if defined FAIL_REASON goto :failure

echo.
echo Services started.
echo Backend:  http://127.0.0.1:8000
echo Frontend: http://localhost:5173  (Vite may choose another port if busy)
echo.
echo LIVE mode requires recorder data and enough collected history.
echo REPLAY synthetic validation works without live API credentials or recorder data.
echo start.bat does not start PM2.5 or weather recorders.
if not "%AIRTRACE_NO_PAUSE%"=="1" pause
exit /b 0

:failure
echo.
echo START FAILED: %FAIL_REASON%
if not "%AIRTRACE_NO_PAUSE%"=="1" pause
exit /b 1
