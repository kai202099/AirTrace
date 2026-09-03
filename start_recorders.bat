@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
set "VENV_PYTHON=%ROOT%.venv\Scripts\python.exe"
set "FAIL_REASON="
set "PM25_START_FAILED="
set "WEATHER_START_FAILED="

if not exist "%VENV_PYTHON%" (
    set "FAIL_REASON=.venv is missing or incomplete. Run setup.bat first."
    goto :failure
)
if not exist "%ROOT%scripts\record_pm25.py" (
    set "FAIL_REASON=scripts\record_pm25.py was not found."
    goto :failure
)
if not exist "%ROOT%scripts\record_weather.py" (
    set "FAIL_REASON=scripts\record_weather.py was not found."
    goto :failure
)

echo AirTrace recorders
if not exist "%ROOT%.env" echo WARNING: .env is missing. Recorder API keys are loaded by the existing AirTrace configuration code; no credentials are printed by this helper.
echo Recorders write local ignored DuckDB files under data\ and raw snapshots under data\raw\.
echo.

start "AirTrace PM2.5 Recorder" /D "%ROOT%" "%ComSpec%" /k ""%VENV_PYTHON%" "%ROOT%scripts\record_pm25.py""
if errorlevel 1 set "PM25_START_FAILED=1"
start "AirTrace Weather Recorder" /D "%ROOT%" "%ComSpec%" /k ""%VENV_PYTHON%" "%ROOT%scripts\record_weather.py""
if errorlevel 1 set "WEATHER_START_FAILED=1"

if defined PM25_START_FAILED if defined WEATHER_START_FAILED set "FAIL_REASON=Could not start either recorder window."
if defined PM25_START_FAILED if not defined WEATHER_START_FAILED set "FAIL_REASON=Could not start the PM2.5 recorder window. The weather recorder window was started."
if not defined PM25_START_FAILED if defined WEATHER_START_FAILED set "FAIL_REASON=Could not start the weather recorder window. The PM2.5 recorder window was started."
if defined FAIL_REASON goto :failure

echo.
echo Recorder windows started.
echo PM2.5 database: data\airtrace.duckdb
echo Weather database: data\weather.duckdb
echo Stop each recorder with Ctrl+C in its recorder window.
if not "%AIRTRACE_NO_PAUSE%"=="1" pause
exit /b 0

:failure
echo.
echo RECORDER START FAILED: %FAIL_REASON%
if not "%AIRTRACE_NO_PAUSE%"=="1" pause
exit /b 1
