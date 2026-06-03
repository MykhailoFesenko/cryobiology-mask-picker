@echo off
chcp 65001 > nul
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>nul
if not errorlevel 1 (
  set "PY=python"
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PY=py -3"
  ) else (
    echo [ERROR] Python not found. Install Python 3.10+ and enable Add to PATH.
    pause
    exit /b 1
  )
)

%PY% run_segmentation.py %*
pause
