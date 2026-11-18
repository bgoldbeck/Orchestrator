# Build Orchestrator.exe (one-folder) and a zip; also an installer if Inno Setup 6 is installed.
#   powershell -ExecutionPolicy Bypass -File scripts\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$py = ".venv\Scripts\python.exe"

& $py -m pip install -q -r requirements.txt -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& $py scripts\make_icon.py
if ($LASTEXITCODE -ne 0) { throw "icon failed" }
& $py -m PyInstaller --noconfirm --clean orchestrator.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$version = (Select-String -Path pyproject.toml -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
$zip = "dist\Orchestrator-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\Orchestrator -DestinationPath $zip
Write-Host "exe: dist\Orchestrator\Orchestrator.exe"
Write-Host "zip: $zip"

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc "/DAppVersion=$version" installer\orchestrator.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "installer: dist\Orchestrator-$version-setup.exe"
} else {
    Write-Host "Inno Setup 6 not found: skipped the installer (the zip/exe above are complete)."
}
