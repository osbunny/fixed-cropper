# build.ps1
$ErrorActionPreference = "Stop"

# プロジェクトルート（このps1がある場所）へ移動
Set-Location -Path $PSScriptRoot

# 既存ビルド成果物を削除（あれば）
if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
if (Test-Path "dist")  { Remove-Item "dist"  -Recurse -Force }
Get-ChildItem -Filter "*.spec" | Remove-Item -Force -ErrorAction SilentlyContinue

# ---- version_info.txt を APP_VER から自動生成 ----

# constants.py から APP_VER を取得（例: "1.0.0"）
$constantsPath = "fixed_cropper\constants.py"
$verLine = Select-String -Path $constantsPath -Pattern "APP_VER\s*="
$APP_VER = ($verLine -split "=")[1].Trim().Trim('"').Trim("'")

# "1.0.0" -> 1,0,0 （足りなければ0埋め、余れば切り捨て）
$parts = $APP_VER.Split(".") | ForEach-Object { [int]$_ }
$major = if ($parts.Length -ge 1) { $parts[0] } else { 0 }
$minor = if ($parts.Length -ge 2) { $parts[1] } else { 0 }
$patch = if ($parts.Length -ge 3) { $parts[2] } else { 0 }
$build = 0

# VSVersionInfo 用：4整数タプル（これが必須）
$filevers = "($major, $minor, $patch, $build)"
$prodvers = "($major, $minor, $patch, $build)"

@"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=$filevers,
    prodvers=$prodvers,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [
            StringStruct('CompanyName', 'ヨニキ'),
            StringStruct('FileDescription', 'Fixed Cropper'),
            StringStruct('FileVersion', '$APP_VER'),
            StringStruct('InternalName', 'FixedCropper'),
            StringStruct('OriginalFilename', 'FixedCropper.exe'),
            StringStruct('ProductName', 'Fixed Cropper'),
            StringStruct('ProductVersion', '$APP_VER'),
            StringStruct('LegalCopyright', '© 2026 ヨニキ. All rights reserved. supported by PRIMROSE')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])])
  ]
)
"@ | Set-Content -Encoding Default version_info.txt


# ビルド
pyinstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --clean `
  --strip `
  --exclude-module unittest `
  --exclude-module pydoc `
  --exclude-module tkinter `
  --name "FixedCropper" `
  --icon "fixed_cropper\resources\icon.ico" `
  --version-file "version_info.txt" `
  --add-data "fixed_cropper\resources\icon.ico;fixed_cropper\resources" `
  app.py

Write-Host ""
Write-Host "Build finished: dist\FixedCropper.exe" -ForegroundColor Green
