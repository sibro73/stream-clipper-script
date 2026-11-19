@echo off
rem Echtzeit-Clipper dauerhaft laufen lassen (startet bei Absturz neu). Log: logs\live.log
cd /d "%~dp0"
if not exist logs mkdir logs
where ffmpeg >nul 2>&1 || set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin;%PATH%"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
:loop
"C:\Users\sib\AppData\Local\Programs\Python\Python313\python.exe" -m live run %*
echo %date% %time% Clipper beendet (Code %errorlevel%) - Neustart in 30 s >> logs\live_restarts.log
timeout /t 30 /nobreak >nul
goto loop
