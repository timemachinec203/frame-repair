@echo off
rem Command-line use from this folder: rife-repair clip.mov --dry-run   (rife-repair --help)
setlocal
call "%~dp0app\tools\env.cmd" || exit /b 1
"%FR_PY%" -m frame_repair %*
