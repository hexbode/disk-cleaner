# 打包为单文件可执行程序: dist\DiskCleaner.exe
# 用法: .\build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[1/2] 检查 PyInstaller..."
python -c "import PyInstaller" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "未检测到 PyInstaller, 正在安装..."
    python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败" }
}

Write-Host "[2/2] 开始打包..."
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name DiskCleaner `
    --icon app_icon.ico `
    --add-data "app_icon.ico;." `
    disk_cleaner.py
if ($LASTEXITCODE -ne 0) { throw "打包失败" }

Copy-Item "dist\DiskCleaner.exe" ".\DiskCleaner.exe" -Force
Write-Host "打包完成: DiskCleaner.exe"
