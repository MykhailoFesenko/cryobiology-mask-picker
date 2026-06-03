@echo off
REM Створює ZIP-бекап основних результатів проекту в папку _backups\ (v1.6.6).
REM Вмикає: data/vesicles_good/images, overlay/png/yolo результати моделей,
REM         apps/mask_picker, apps/segmentation, конфіги і скрипти.
REM НЕ вмикає: .npy (жирні), shared/cellsegkit (вендор-код), cryobiology4/weights (1.8 GB).

setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Timestamp у форматі YYYY-MM-DD_HHMM
for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value ^| find "="') do set dt=%%a
set TS=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%_%dt:~8,2%%dt:~10,2%

set BACKUP_DIR=_backups
set OUT=%BACKUP_DIR%\cryobiology_%TS%.zip

if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"

echo.
echo === Створюю бекап: %OUT% ===
echo (може зайняти 1-3 хв залежно від розміру)
echo.

REM Використовуємо PowerShell Compress-Archive (вбудований у Windows 10+)
powershell -NoProfile -Command ^
    "$items = @('data\vesicles_good\images', 'data\vesicles_good\polygons', 'data\vesicles_good\selections.json', 'data\vesicles_good\labels.json', 'data\vesicles_good\output', 'apps\mask_picker', 'apps\segmentation', 'tools\launchers', 'tools\check_roundtrip.py', 'docs', 'bake_all.bat', 'run_mask_picker.bat'); $exist = $items | Where-Object { Test-Path $_ }; Compress-Archive -Path $exist -DestinationPath '%OUT%' -CompressionLevel Optimal -Force"

if exist "%OUT%" (
    echo.
    echo === Готово ===
    for %%I in ("%OUT%") do echo   Файл: %%I
    for %%I in ("%OUT%") do echo   Розмір: %%~zI байт
) else (
    echo [!] Бекап не створено
)

pause
