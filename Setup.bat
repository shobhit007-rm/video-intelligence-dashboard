@echo off
title Video Intelligence setup
cd /d "%~dp0"
echo Installing FFmpeg, yt-dlp, Deno and Python 3.12 with winget (skips anything already installed)...
winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements
winget install --id yt-dlp.yt-dlp --exact --accept-package-agreements --accept-source-agreements
winget install --id DenoLand.Deno --exact --accept-package-agreements --accept-source-agreements
winget install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
echo.
echo Creating the Python environment in .venv (this downloads about 2 GB the first time)...
rem A fresh winget install isn't on this window's PATH yet, so look in its default folder first.
set PY=py -3.12
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
%PY% -m venv .venv || goto :error
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
echo.
echo Setup finished. Double-click "Start Dashboard.bat" to open the dashboard.
pause
exit /b 0

:error
echo.
echo Setup failed. Read the messages above, fix the problem and run Setup.bat again.
pause
exit /b 1
