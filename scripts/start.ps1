# TradingView Signal Server - Windows 一键启动脚本
# 使用方式: .\scripts\start.ps1
# 可选参数: .\scripts\start.ps1 -Port 8000 -Host 0.0.0.0 -Check

param(
    [string]$ServerHost = "0.0.0.0",
    [int]$Port = 8000,
    [switch]$Check,       # 仅运行环境检查，不启动服务器
    [switch]$Debug,       # 开启 debug 模式（热重载）
    [switch]$NoBanner
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Set-Location $ProjectRoot

# ─────────────────────────────────────────────
# 查找 Python 可执行文件
# ─────────────────────────────────────────────
function Find-Python {
    $candidates = @(
        "C:\Users\Admini\.workbuddy\binaries\python\versions\3.14.3\python.exe",
        "C:\Users\Admini\.workbuddy\binaries\python\versions\3.13.0\python.exe",
        "C:\Users\Admini\.workbuddy\binaries\python\versions\3.12.0\python.exe",
        "C:\Users\Admini\.workbuddy\binaries\python\versions\3.11.0\python.exe",
        "C:\Users\Admini\.workbuddy\binaries\python\versions\3.10.0\python.exe",
        (Get-Command python3 -ErrorAction SilentlyContinue)?.Source,
        (Get-Command python -ErrorAction SilentlyContinue)?.Source
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) {
            $ver = & $c -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')" 2>$null
            if ($ver -and [version]$ver -ge [version]"3.10") {
                return $c
            }
        }
    }
    return $null
}

if (-not $NoBanner) {
    Write-Host ""
    Write-Host "  TradingView Signal Server - 启动脚本" -ForegroundColor Cyan
    Write-Host "  ======================================" -ForegroundColor Cyan
}

$pyExe = Find-Python
if (-not $pyExe) {
    Write-Host "[FAIL] 未找到 Python 3.10+ 可执行文件" -ForegroundColor Red
    Write-Host "       请安装 Python 3.10+ 或检查路径" -ForegroundColor Yellow
    exit 1
}
Write-Host "  [OK] Python: $pyExe" -ForegroundColor Green

# ─────────────────────────────────────────────
# 设置 UTF-8 编码（解决 Windows GBK 问题）
# ─────────────────────────────────────────────
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ─────────────────────────────────────────────
# 检查 .env 文件
# ─────────────────────────────────────────────
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Write-Host "  [WARN] .env 不存在，从 .env.example 复制..." -ForegroundColor Yellow
        Copy-Item ".env.example" ".env"
        Write-Host "  [INFO] 请编辑 .env 文件填写必要配置后重新运行" -ForegroundColor Cyan
        exit 0
    } else {
        Write-Host "  [FAIL] .env 和 .env.example 都不存在" -ForegroundColor Red
        exit 1
    }
}

# ─────────────────────────────────────────────
# 创建必要目录
# ─────────────────────────────────────────────
@("data", "data\backups", "logs", "trade_logs") | ForEach-Object {
    if (-not (Test-Path $_)) {
        New-Item -ItemType Directory -Path $_ -Force | Out-Null
        Write-Host "  [OK] 创建目录: $_" -ForegroundColor Green
    }
}

# ─────────────────────────────────────────────
# 运行环境检查
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "  运行环境检查..." -ForegroundColor Cyan
& $pyExe "scripts\check_env.py"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  [FAIL] 环境检查失败，请修复上述问题" -ForegroundColor Red
    exit 1
}

if ($Check) {
    Write-Host ""
    Write-Host "  [INFO] 环境检查完成（--Check 模式，不启动服务器）" -ForegroundColor Cyan
    exit 0
}

# ─────────────────────────────────────────────
# 检查依赖是否安装
# ─────────────────────────────────────────────
$uvicornCheck = & $pyExe -c "import uvicorn" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  [INFO] 正在安装依赖..." -ForegroundColor Yellow
    & $pyExe -m pip install -r requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [FAIL] 依赖安装失败" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [OK] 依赖安装完成" -ForegroundColor Green
}

# ─────────────────────────────────────────────
# 启动服务器
# ─────────────────────────────────────────────
Write-Host ""
Write-Host "  启动 Signal Server..." -ForegroundColor Green
Write-Host "  地址: http://${ServerHost}:${Port}" -ForegroundColor Cyan
Write-Host "  按 Ctrl+C 停止服务器" -ForegroundColor Gray
Write-Host ""

$uvicornArgs = @("app:app", "--host", $ServerHost, "--port", $Port.ToString())
if ($Debug) {
    $uvicornArgs += "--reload"
    Write-Host "  [DEBUG] 热重载模式已启用" -ForegroundColor Yellow
}

& $pyExe -m uvicorn @uvicornArgs
