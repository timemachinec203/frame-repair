@echo off
setlocal
cd /d "%~dp0"
title Frame Repair setup
echo Frame Repair setup
echo.
echo Sets up Frame Repair inside this folder: Python packages including PyTorch for
echo your graphics card, plus ffmpeg if it's missing. Needs an internet connection
echo and up to 6 GB of space. Options are passed on, e.g.  Install.cmd --gpu cpu
echo.

set "PY="
call :try py -3.12
if not defined PY call :try py -3.13
if not defined PY call :try py -3.11
if not defined PY call :try py -3
if not defined PY call :try python
if not defined PY call :trypath "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto :havepython

echo Python 3.9 or newer was not found.
where winget >nul 2>nul || goto :nopython
choice /M "Install Python 3.12 now with winget"
if errorlevel 2 goto :nopython
winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
call :trypath "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto :havepython
:nopython
echo Install Python 3.12 from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then run Install again.
goto :fail

:havepython
echo Python: %PY%
if exist "app\.venv\Scripts\python.exe" goto :haveenv
echo Creating the environment in app\.venv ...
%PY% -m venv "app\.venv" || goto :fail
:haveenv
"app\.venv\Scripts\python.exe" "app\tools\setup_env.py" %* || goto :fail
echo.
echo Done. Open "Frame Repair" from the Start menu, the desktop or this folder.
echo Or drag video clips onto "Analyze", "Film look (24 fps)", "Fix dropped frames"
echo or "Smooth 60 fps" in this folder.
echo.
pause
exit /b 0

:fail
echo.
echo Setup did not finish. See the messages above.
pause
exit /b 1

:try
%* -c "import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] < (3, 15) else 1)" >nul 2>nul && set "PY=%*"
exit /b 0

:trypath
if not exist "%~1" exit /b 0
"%~1" -c "import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] < (3, 15) else 1)" >nul 2>nul && set PY="%~1"
exit /b 0
