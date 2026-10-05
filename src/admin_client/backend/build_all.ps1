Write-Host "Building hardened backend with PyInstaller (bytecode optimization level 2, debug stripping)..."
pyinstaller --noconfirm --clean admin-backend-x86_64-pc-windows-msvc.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "Copying backend executable to src-tauri/bin..."
$exePath = "dist\admin-backend-x86_64-pc-windows-msvc.exe"
$targetDir = "..\frontend\src-tauri\bin"
if (-not (Test-Path $targetDir)) { New-Item -ItemType Directory -Force -Path $targetDir }
Copy-Item -Path $exePath -Destination $targetDir -Force

Write-Host "Building frontend with Tauri..."
Set-Location ..\frontend
npm install
if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
npx tauri build
if ($LASTEXITCODE -ne 0) { throw "tauri build failed" }
Write-Host "Build finished successfully!"
