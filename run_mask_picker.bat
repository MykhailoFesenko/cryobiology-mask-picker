@echo off
chcp 65001 > nul
setlocal EnableExtensions

REM Project-root Mask Picker launcher.
REM Default workspace is the current vesicles_good annotation dataset.
REM Pass explicit arguments to override, for example:
REM   run_mask_picker.bat --workspace D:\some_other_workspace

set "ROOT=%~dp0"
for %%I in ("%ROOT%.") do set "ROOT=%%~fI"
set "DEFAULT_WORKSPACE=%ROOT%\data\vesicles_good"

cd /d "%ROOT%\apps\mask_picker"
if "%~1"=="" (
  call run.bat --workspace "%DEFAULT_WORKSPACE%"
) else (
  call run.bat %*
)
endlocal
