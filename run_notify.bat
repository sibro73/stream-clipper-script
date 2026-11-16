@echo off
rem Telegram-Benachrichtigung (Windows-Aufgabenplanung): "run_notify.bat morning" oder "run_notify.bat remind"
cd /d "%~dp0"
if not exist logs mkdir logs
where ffmpeg >nul 2>&1 || set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin;%PATH%"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
echo ===== %date% %time% %1 >> logs\notify.log
"C:\Users\sib\AppData\Local\Programs\Python\Python313\python.exe" notify.py %* >> logs\notify.log 2>&1
