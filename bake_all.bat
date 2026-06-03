@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo === Bake all selected (cleanup + polygons) ===
python tools\launchers\bake_all.py --pack %*
echo.
pause
