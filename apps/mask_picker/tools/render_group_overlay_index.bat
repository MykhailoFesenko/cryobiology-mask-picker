@echo off
chcp 65001 > nul
setlocal EnableExtensions

REM Render group overlay HTML index for QA.
REM Usage: render_group_overlay_index.bat --workspace ..\..\..\data\vesicles_good [...other args]

cd /d "%~dp0"

where python >nul 2>nul
if not errorlevel 1 (
  set "PY=python"
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PY=py -3"
  ) else (
    echo [ERROR] Python not found. Install Python 3.10+ and add to PATH.
    pause
    exit /b 1
  )
)

%PY% render_group_overlay_index.py %*
if errorlevel 1 pause
endlocal
