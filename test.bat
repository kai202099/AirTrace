@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
set "VENV_PYTHON=%ROOT%.venv\Scripts\python.exe"
set "FAIL_REASON="
set "ROOT_PUSHED="
set "FRONTEND_PUSHED="

pushd "%ROOT%" >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Could not open the AirTrace repository root: %ROOT%"
    goto :failure
)
set "ROOT_PUSHED=1"

if not exist "%VENV_PYTHON%" (
    set "FAIL_REASON=.venv is missing or incomplete. Run setup.bat first."
    goto :failure
)
if not exist "%ROOT%frontend\node_modules" (
    set "FAIL_REASON=frontend\node_modules is missing. Run setup.bat first."
    goto :failure
)

echo Running Python tests ...
"%VENV_PYTHON%" -m pytest
if errorlevel 1 (
    set "FAIL_REASON=Python tests failed."
    goto :failure
)

echo Running Python compileall ...
"%VENV_PYTHON%" -m compileall -q airtrace scripts tests
if errorlevel 1 (
    set "FAIL_REASON=Python compileall failed."
    goto :failure
)

pushd "%ROOT%frontend" >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Could not open the frontend directory: %ROOT%frontend"
    goto :failure
)
set "FRONTEND_PUSHED=1"
echo Running frontend tests ...
call npm test
if errorlevel 1 (
    set "FAIL_REASON=Frontend tests failed."
    goto :failure
)
echo Building frontend ...
call npm run build
if errorlevel 1 (
    set "FAIL_REASON=Frontend build failed."
    goto :failure
)
popd >nul
set "FRONTEND_PUSHED="

echo.
echo All Python and frontend checks passed.
if defined ROOT_PUSHED popd >nul
if not "%AIRTRACE_NO_PAUSE%"=="1" pause
exit /b 0

:failure
echo.
echo TEST FAILED: %FAIL_REASON%
if defined FRONTEND_PUSHED popd >nul
if defined ROOT_PUSHED popd >nul
if not "%AIRTRACE_NO_PAUSE%"=="1" pause
exit /b 1
