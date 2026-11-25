@rem Environment for the launchers in the folder above: call "...\app\tools\env.cmd"
set "FR_APP=%~dp0.."
if exist "%FR_APP%\.venv\Scripts\python.exe" goto :ok
echo Frame Repair isn't set up yet: double-click Install first.
exit /b 1
:ok
set "FR_PY=%FR_APP%\.venv\Scripts\python.exe"
set "PYTHONPATH=%FR_APP%"
set "PYTHONUTF8=1"
if not defined FRAME_REPAIR_WEIGHTS set "FRAME_REPAIR_WEIGHTS=%FR_APP%\weights\rife-v4.25.rmw"
if exist "%FR_APP%\settings.cmd" call "%FR_APP%\settings.cmd"
exit /b 0
