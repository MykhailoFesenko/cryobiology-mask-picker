@echo off
chcp 65001 > nul
setlocal EnableExtensions

REM Project-root group overlay regenerator.
REM Перезаписує group_overlay_index.html у корені проекту з останніми
REM groups/polygons даними з workspace.
REM
REM Default workspace = data\vesicles_good. Override прикладами:
REM   render_group_overlay.bat --workspace D:\some_other_workspace
REM   render_group_overlay.bat --workspace data\vesicles_good --max-dim 1600

set "ROOT=%~dp0"
for %%I in ("%ROOT%.") do set "ROOT=%%~fI"
set "DEFAULT_WORKSPACE=%ROOT%\data\vesicles_good"
set "OUT_HTML=%ROOT%\group_overlay_index.html"
set "SCRIPT=%ROOT%\apps\mask_picker\tools\render_group_overlay_index.py"

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

echo === Render group overlay -^> %OUT_HTML% ===
if "%~1"=="" (
  %PY% "%SCRIPT%" --workspace "%DEFAULT_WORKSPACE%" --out "%OUT_HTML%"
) else (
  %PY% "%SCRIPT%" --out "%OUT_HTML%" %*
)
if errorlevel 1 (
  echo.
  echo [ERROR] render_group_overlay_index.py failed
  pause
  exit /b 1
)
echo.
echo Done. Open: %OUT_HTML%
endlocal
