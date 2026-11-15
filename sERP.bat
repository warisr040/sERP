@echo off
setlocal
cd /d "%~dp0"
title sERP - simply handle IT ERP

rem ---------------------------------------------------------------
rem  sERP launcher. First start: find (or install) Python and create
rem  a private environment in .venv. No "Add to PATH" needed.
rem ---------------------------------------------------------------
if exist ".venv\Scripts\python.exe" goto run

call :findpy
if defined PY goto prepare

echo Python 3.10 or newer was not found on this computer.
where winget >nul 2>nul || goto nowinget
echo Installing Python 3.12 for your Windows user (no admin rights needed)...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpy
if not defined PY goto nopy

:prepare
echo Using Python: %PY%
echo Preparing sERP for the first start (about a minute)...
"%PY%" -m venv .venv || goto nopy
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto pipfail

:run
echo Starting sERP... the app window opens by itself. Close this window to stop sERP.
".venv\Scripts\python.exe" serp_server.py
if errorlevel 1 pause
goto :eof

rem ---------------- find a usable python.exe ----------------
:findpy
set "PY="
rem 1) the Python launcher (installed with python.org Python)
for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY call :checkpy && exit /b 0
rem 2) python on PATH (the Microsoft Store placeholder prints nothing and is skipped)
for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY call :checkpy && exit /b 0
rem 3) the usual install folders, even when they are not on PATH
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "%ProgramFiles(x86)%\Python3*") do (
  if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
)
if defined PY call :checkpy && exit /b 0
set "PY="
exit /b 1

:checkpy
"%PY%" -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (set "PY=" & exit /b 1)
exit /b 0

rem ---------------- messages ----------------
:nowinget
echo.
echo Could not install Python automatically (winget is not available).
echo Please install Python 3.12 from https://www.python.org/downloads/
echo then run sERP.bat again. Adding it to PATH is NOT required.
pause
exit /b 1

:nopy
echo.
echo Python could not be set up. Install Python 3.12 from https://www.python.org/downloads/
echo then run sERP.bat again. Adding it to PATH is NOT required.
if exist .venv rmdir /s /q .venv
pause
exit /b 1

:pipfail
echo.
echo Could not install the packages sERP needs. Check the internet connection and run sERP.bat again.
rmdir /s /q .venv
pause
exit /b 1
