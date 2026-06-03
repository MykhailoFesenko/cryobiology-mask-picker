@echo off
chcp 65001 > nul
setlocal EnableExtensions

REM Mask Picker Windows launcher.
REM It does not use a hardcoded Python path: first tries `python`, then `py -3`.

cd /d "%~dp0"
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PYTHONPATH=%ROOT%\shared\cellsegkit;%PYTHONPATH%"

where python >nul 2>nul
if not errorlevel 1 (
  set "PY=python"
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PY=py -3"
  ) else (
    echo.
    echo [ERROR] Python not found. Install Python 3.10+ and enable Add to PATH.
    echo https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
  )
)

%PY% -c "import flask, yaml, numpy, PIL, cv2, matplotlib, skimage" >nul 2>nul
if errorlevel 1 (
  echo [i] Installing Mask Picker requirements...
  %PY% -m pip install --user -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed. See the message above.
    pause
    exit /b 1
  )
)

%PY% -c "import cellsegkit" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Local cellsegkit not found at "%ROOT%\shared\cellsegkit".
  pause
  exit /b 1
)

%PY% app.py %*
if errorlevel 1 pause
endlocal
