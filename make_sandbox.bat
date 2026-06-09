@echo off
REM Зробити пісочницю-копію датасета для безпечних експериментів з розміткою.
REM За замовчуванням кладе ПОЗА OneDrive (%USERPROFILE%\cryobiology_sandboxes\).
REM Оригінал data\vesicles_good НЕ чіпається.
REM
REM Приклади:
REM   make_sandbox.bat                 (vesicles_good -> ...\cryobiology_sandboxes\vesicles_sandbox)
REM   make_sandbox.bat --name try2     (інша назва)
REM   make_sandbox.bat --lean          (без selected\, ~1.2 ГБ менше)
REM   make_sandbox.bat --list          (показати наявні пісочниці)
REM   make_sandbox.bat --dry-run       (лише план, нічого не копіює)
cd /d "%~dp0"
python tools\make_sandbox.py %*
pause
