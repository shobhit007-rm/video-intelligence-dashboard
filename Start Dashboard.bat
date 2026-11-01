@echo off
title Video Intelligence dashboard
cd /d "%~dp0"
echo Starting the Video Intelligence dashboard...
echo Keep this window open while you use it. Close it to stop.
echo.
".venv\Scripts\python.exe" dashboard\server.py --open
echo.
echo The dashboard has stopped.
pause
