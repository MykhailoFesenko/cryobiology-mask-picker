$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..\..")
$Desktop = [Environment]::GetFolderPath("Desktop")

function Find-Pythonw {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $candidate = Join-Path (Split-Path -Parent $python.Source) "pythonw.exe"
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }

    return $null
}

function New-Shortcut {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$TargetPath,
        [string]$Arguments = "",
        [Parameter(Mandatory=$true)][string]$WorkingDirectory,
        [string]$Description = "",
        [string]$IconLocation = ""
    )

    $shell = New-Object -ComObject WScript.Shell
    $shortcutPath = Join-Path $Desktop ($Name + ".lnk")
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Description = $Description
    if ($IconLocation) { $shortcut.IconLocation = $IconLocation }
    $shortcut.Save()
    Write-Host "Created: $shortcutPath"
}

$pythonw = Find-Pythonw
$maskLauncher = Join-Path $ScriptDir "mask_picker_launcher.pyw"
$maskBat = Join-Path $Root "run_mask_picker.bat"
$segBat = Join-Path $Root "apps\segmentation\run.bat"

if ($pythonw) {
    New-Shortcut `
        -Name "Cryobiology Mask Picker" `
        -TargetPath $pythonw `
        -Arguments "`"$maskLauncher`"" `
        -WorkingDirectory $Root `
        -Description "Start Mask Picker without a console window" `
        -IconLocation "$env:SystemRoot\System32\shell32.dll,177"
} else {
    New-Shortcut `
        -Name "Cryobiology Mask Picker" `
        -TargetPath $maskBat `
        -WorkingDirectory $Root `
        -Description "Start Mask Picker" `
        -IconLocation "$env:SystemRoot\System32\shell32.dll,177"
}

New-Shortcut `
    -Name "Cryobiology Segmentation" `
    -TargetPath $segBat `
    -WorkingDirectory $Root `
    -Description "Run segmentation pipeline" `
    -IconLocation "$env:SystemRoot\System32\shell32.dll,23"
