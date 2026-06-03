@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo === Mask Picker SANDBOX (тестова копія, НЕ замовницькі дані) ===
echo Workspace: C:\Users\user\MaskPickerSandbox\vesicles_good
echo.
python apps\mask_picker\app.py --workspace "C:\Users\user\MaskPickerSandbox\vesicles_good" %*
pause
