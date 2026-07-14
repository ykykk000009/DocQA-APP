param(
    [switch]$IncludeModels,
    [string]$ModelCachePath = "..\models\huggingface",
    [string]$BootstrapPython = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$BuildVenv = Join-Path $ProjectRoot ".venv-package"
$Python = Join-Path $BuildVenv "Scripts\python.exe"
$DistRoot = Join-Path $ProjectRoot "dist\windows"
$AppDir = Join-Path $DistRoot "EnterpriseDocumentRAG"
$PackageTemp = Join-Path $ProjectRoot ".tmp-package"
$PyInstallerCache = Join-Path $ProjectRoot ".pyinstaller-cache"

Set-Location $ProjectRoot
New-Item -ItemType Directory -Force $PackageTemp, $PyInstallerCache | Out-Null
$env:TEMP = $PackageTemp
$env:TMP = $PackageTemp
$env:PYINSTALLER_CONFIG_DIR = $PyInstallerCache

$ExistingUserData = Join-Path $AppDir "user-data"
if (Test-Path $ExistingUserData) {
    throw "user-data exists in the dist build directory. Back it up or move it to a separate installation before packaging."
}
if (Test-Path $AppDir) {
    Remove-Item $AppDir -Recurse -Force
}

if (-not (Test-Path $Python)) {
    & $BootstrapPython -m venv $BuildVenv
}

& $Python -m pip install --upgrade pip
& $Python -m pip install ".[dev]" pyinstaller
& $Python -m pytest

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name EnterpriseDocumentRAG `
    --distpath $DistRoot `
    --workpath (Join-Path $ProjectRoot "build\pyinstaller") `
    --specpath (Join-Path $ProjectRoot "build") `
    --collect-all enterprise_document_rag `
    --collect-all sentence_transformers `
    --collect-all transformers `
    --collect-all rapidocr_onnxruntime `
    --collect-all qdrant_client `
    --collect-submodules onnxruntime `
    windows_launcher.py

Copy-Item (Join-Path $PSScriptRoot "QUICK_START.txt") (Join-Path $AppDir "QUICK_START.txt") -Force
Copy-Item (Join-Path $PSScriptRoot "portable.mode") $AppDir -Force
Copy-Item (Join-Path $ProjectRoot "README.md") $AppDir -Force

if ($IncludeModels) {
    $ResolvedModels = (Resolve-Path $ModelCachePath).Path
    $ModelTarget = Join-Path $AppDir "models\huggingface"
    New-Item -ItemType Directory -Force $ModelTarget | Out-Null
    Copy-Item (Join-Path $ResolvedModels "*") $ModelTarget -Recurse -Force
}

$ZipName = if ($IncludeModels) {
    "EnterpriseDocumentRAG-windows-x64-offline.zip"
} else {
    "EnterpriseDocumentRAG-windows-x64-online.zip"
}
$ZipPath = Join-Path $DistRoot $ZipName
if (Test-Path $ZipPath) {
    Remove-Item $ZipPath -Force
}
if (Test-Path (Join-Path $AppDir "user-data")) {
    throw "user-data was found in the build output. Packaging stopped."
}
Compress-Archive -Path $AppDir -DestinationPath $ZipPath -CompressionLevel Optimal
$Hash = (Get-FileHash $ZipPath -Algorithm SHA256).Hash
Set-Content `
    -LiteralPath (Join-Path $DistRoot "SHA256SUMS.txt") `
    -Value "$Hash  $ZipName" `
    -Encoding ASCII
Write-Host "Build completed: $ZipPath"
Write-Host "SHA-256: $Hash"
