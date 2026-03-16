$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

& powershell -ExecutionPolicy Bypass -File (Join-Path $scriptDir "build_win11.ps1")

$isccPath = $null
$command = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($command) {
  $isccPath = $command.Source
}

if (-not $isccPath) {
  $candidate1 = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
  $candidate2 = "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
  if (Test-Path $candidate1) {
    $isccPath = $candidate1
  } elseif (Test-Path $candidate2) {
    $isccPath = $candidate2
  }
}

if (-not $isccPath) {
  Write-Host "Inno Setup not found, try installing by winget..."
  & winget install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements
  $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
  if ($command) {
    $isccPath = $command.Source
  } else {
    $candidate1 = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    $candidate2 = "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate1) {
      $isccPath = $candidate1
    } elseif (Test-Path $candidate2) {
      $isccPath = $candidate2
    }
  }
}

if (-not $isccPath) {
  throw "ISCC.exe not found. Install Inno Setup 6 and rerun."
}

$issPath = Join-Path $scriptDir "installer_win11.iss"
Write-Host "Using ISCC: $isccPath"
Write-Host "Compiling: $issPath"

# Use Start-Process to handle arguments better
$proc = Start-Process -FilePath $isccPath -ArgumentList "`"$issPath`"" -Wait -NoNewWindow -PassThru
if ($proc.ExitCode -ne 0) {
  Write-Host "ISCC failed with exit code $($proc.ExitCode)"
  exit $proc.ExitCode
}

Write-Host ""
Write-Host "Setup build completed:"
$setupExe = Get-ChildItem -Path (Join-Path $scriptDir "dist") -Filter *.exe -File |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if ($setupExe) {
  Write-Host "  $($setupExe.FullName)"
} else {
  Write-Host "  未找到安装包，请检查 dist 目录"
}
