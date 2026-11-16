@echo off
rem Taeglicher Lauf (Windows-Aufgabenplanung): alle neuen VODs clippen, Ausgabe nach logs\daily.log
cd /d "%~dp0"
if not exist logs mkdir logs
where ffmpeg >nul 2>&1 || set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin;%PATH%"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
"C:\Users\sib\AppData\Local\Programs\Python\Python313\python.exe" clipper.py run --all >> logs\daily.log 2>&1
