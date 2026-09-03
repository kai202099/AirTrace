@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
set "ROOT_PUSHED="
set "FRONTEND_PUSHED="
set "SYSTEM_PYTHON="
set "PY_LAUNCHER="
set "SYSTEM_VERSION="
set "PY_MAJOR="
set "PY_MINOR="
set "NODE_VERSION="
set "NODE_MAJOR="
set "NODE_MINOR="
set "FAIL_REASON="
set "STATUS=1"

pushd "%ROOT%" >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Could not open the AirTrace repository root: %ROOT%"
    goto :failure
)
set "ROOT_PUSHED=1"

echo AirTrace setup
echo Repository: %ROOT%
echo.

rem Prefer the Python launcher, then fall back to python on PATH.
where py >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do if not defined SYSTEM_PYTHON set "SYSTEM_PYTHON=%%P"
)
if defined SYSTEM_PYTHON set "PY_LAUNCHER=1"
if not defined SYSTEM_PYTHON (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do if not defined SYSTEM_PYTHON set "SYSTEM_PYTHON=%%P"
    )
)
if not defined SYSTEM_PYTHON (
    set "FAIL_REASON=Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/ and run setup.bat again."
    goto :failure
)

if defined PY_LAUNCHER (
    for /f "delims=" %%V in ('py -3 -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2^>nul') do if not defined SYSTEM_VERSION set "SYSTEM_VERSION=%%V"
) else (
    for /f "delims=" %%V in ('python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2^>nul') do if not defined SYSTEM_VERSION set "SYSTEM_VERSION=%%V"
)
if not defined SYSTEM_VERSION (
    set "FAIL_REASON=Could not read the version of the detected Python installation: %SYSTEM_PYTHON%"
    goto :failure
)
for /f "tokens=1-3 delims=." %%A in ("%SYSTEM_VERSION%") do (
    set "PY_MAJOR=%%A"
    set "PY_MINOR=%%B"
)
if not defined PY_MAJOR (
    set "FAIL_REASON=Could not parse the detected Python version: %SYSTEM_VERSION%"
    goto :failure
)
set "PYTHON_SUPPORTED="
if %PY_MAJOR% GTR 3 set "PYTHON_SUPPORTED=1"
if %PY_MAJOR% EQU 3 if %PY_MINOR% GEQ 11 set "PYTHON_SUPPORTED=1"
if not defined PYTHON_SUPPORTED (
    set "FAIL_REASON=Python %SYSTEM_VERSION% is unsupported. AirTrace requires Python 3.11 or newer."
    goto :failure
)
echo Found Python %SYSTEM_VERSION%: %SYSTEM_PYTHON%

if not exist "%ROOT%.venv\Scripts\python.exe" (
    if exist "%ROOT%.venv" (
        set "FAIL_REASON=%ROOT%.venv exists but is incomplete (missing .venv\Scripts\python.exe). Remove that incomplete environment manually, then run setup.bat again."
        goto :failure
    )
    echo Creating project-local .venv ...
    if defined PY_LAUNCHER (
        py -3 -m venv "%ROOT%.venv"
    ) else (
        python -m venv "%ROOT%.venv"
    )
    if errorlevel 1 (
        set "FAIL_REASON=Could not create the project-local .venv. Check that Python can create virtual environments."
        goto :failure
    )
)

set "VENV_PYTHON=%ROOT%.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
    set "FAIL_REASON=The project-local Python was not created at %VENV_PYTHON%"
    goto :failure
)
"%VENV_PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=The project-local Python cannot be executed: %VENV_PYTHON%"
    goto :failure
)

echo Upgrading pip in .venv ...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    set "FAIL_REASON=pip upgrade failed. Check network access and the Python installation, then run setup.bat again."
    goto :failure
)
echo Installing runtime dependencies ...
"%VENV_PYTHON%" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 (
    set "FAIL_REASON=Runtime dependency installation failed from requirements.txt."
    goto :failure
)
echo Installing development and test dependencies ...
"%VENV_PYTHON%" -m pip install -r "%ROOT%requirements-dev.txt"
if errorlevel 1 (
    set "FAIL_REASON=Development dependency installation failed from requirements-dev.txt."
    goto :failure
)

where node >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Node.js was not found. Install a supported Node.js release (20.19.0 or newer in the 20.x line, or 22.12.0 or newer) and run setup.bat again."
    goto :failure
)
where npm >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=npm was not found. Reinstall Node.js with npm included, then run setup.bat again."
    goto :failure
)
for /f "delims=" %%V in ('node --version 2^>nul') do if not defined NODE_VERSION set "NODE_VERSION=%%V"
if not defined NODE_VERSION (
    set "FAIL_REASON=Could not read the installed Node.js version."
    goto :failure
)
set "NODE_VERSION=%NODE_VERSION:v=%"
for /f "tokens=1-3 delims=." %%A in ("%NODE_VERSION%") do (
    set "NODE_MAJOR=%%A"
    set "NODE_MINOR=%%B"
)
if not defined NODE_MAJOR (
    set "FAIL_REASON=Could not parse the installed Node.js version: %NODE_VERSION%"
    goto :failure
)
set "NODE_SUPPORTED="
if %NODE_MAJOR% EQU 20 if %NODE_MINOR% GEQ 19 set "NODE_SUPPORTED=1"
if %NODE_MAJOR% EQU 22 if %NODE_MINOR% GEQ 12 set "NODE_SUPPORTED=1"
if %NODE_MAJOR% GTR 22 set "NODE_SUPPORTED=1"
if not defined NODE_SUPPORTED (
    set "FAIL_REASON=Node.js %NODE_VERSION% is incompatible with frontend\package.json. Required: ^20.19.0 || >=22.12.0."
    goto :failure
)
echo Found supported Node.js %NODE_VERSION%.

if not exist "%ROOT%frontend\package.json" (
    set "FAIL_REASON=frontend\package.json was not found."
    goto :failure
)
pushd "%ROOT%frontend" >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Could not open the frontend directory: %ROOT%frontend"
    goto :failure
)
set "FRONTEND_PUSHED=1"
echo Installing frontend dependencies with npm ci ...
call npm ci
if errorlevel 1 (
    set "FAIL_REASON=Frontend dependency installation failed during npm ci."
    goto :failure
)
popd >nul
set "FRONTEND_PUSHED="

if exist "%ROOT%.env" (
    echo Existing .env preserved. setup.bat never overwrites an existing .env.
) else (
    if not exist "%ROOT%.env.example" (
        set "FAIL_REASON=.env.example was not found, so the local .env could not be initialized."
        goto :failure
    )
    copy /Y "%ROOT%.env.example" "%ROOT%.env" >nul
    if errorlevel 1 (
        set "FAIL_REASON=Could not create .env from .env.example."
        goto :failure
    )
    echo Created local .env from .env.example.
)

echo.
echo Setup completed successfully.
echo LIVE mode reminder: review .env and fill any required MOENV_API_KEY and CWA_API_KEY values before running recorders. FIRMS_MAP_KEY is optional.
echo REPLAY synthetic validation works without live API credentials or recorder data.
set "STATUS=0"
goto :finish

:failure
echo.
echo SETUP FAILED: %FAIL_REASON%
echo No credentials were printed or modified by this script.
set "STATUS=1"

:finish
if defined FRONTEND_PUSHED popd >nul
if defined ROOT_PUSHED popd >nul
if not "%AIRTRACE_NO_PAUSE%"=="1" pause
exit /b %STATUS%
