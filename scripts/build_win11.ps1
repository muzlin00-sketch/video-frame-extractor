$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$pythonExe = "py"
$pythonPrefixArgs = @("-3")
try {
  & py -3 --version | Out-Null
} catch {
  $pythonExe = "python"
  $pythonPrefixArgs = @()
}

& $pythonExe @pythonPrefixArgs -m pip install --upgrade pip
& $pythonExe @pythonPrefixArgs -m pip install pyinstaller openai scenedetect[opencv] opencv-python gradio

if (Test-Path "$scriptDir\build") { Remove-Item "$scriptDir\build" -Recurse -Force }
if (Test-Path "$scriptDir\dist\VideoAnalyzer") { Remove-Item "$scriptDir\dist\VideoAnalyzer" -Recurse -Force }

$iconPath = "C:\Users\linzhiqiang\Pictures\icon.ico"
$pyiArgs = @(
  "--noconfirm",
  "--clean",
  "--onedir",
  "--name", "VideoAnalyzer",
  "--collect-all", "scenedetect",
  "--collect-all", "cv2",
  "--collect-all", "gradio",
  "--collect-all", "safehttpx",
  "--collect-all", "groovy",
  "--add-data", "$scriptDir\app_config.example.json;."
)
if (Test-Path $iconPath) {
  $pyiArgs += @("--icon", $iconPath)
  Write-Host "Using app icon: $iconPath"
}
$pyiArgs += "$scriptDir\app_gui.py"
& $pythonExe @pythonPrefixArgs -m PyInstaller @pyiArgs

$appDir = "$scriptDir\dist\VideoAnalyzer"
$exePath = "$appDir\VideoAnalyzer.exe"

if (-not (Test-Path "$appDir\app_config.json")) {
  Copy-Item "$scriptDir\app_config.example.json" "$appDir\app_config.json" -Force
}

$launcher = @"
@echo off
cd /d %~dp0
start "" "%~dp0VideoAnalyzer.exe"
"@
Get-ChildItem -Path $appDir -Filter *.bat -ErrorAction SilentlyContinue | Remove-Item -Force
Set-Content -Path "$appDir\LaunchVideoAnalyzer.bat" -Value $launcher -Encoding ASCII

Write-Host ""
Write-Host "Build completed."
Write-Host "GUI Executable: $exePath"
Write-Host "Run executable and open browser UI:"
Write-Host "  .\dist\VideoAnalyzer\VideoAnalyzer.exe"
Write-Host "Config file template:"
Write-Host "  .\dist\VideoAnalyzer\app_config.json"
