#!/usr/bin/env python3
"""
Signal Server - 部署前环境检查工具
运行方式: python scripts/check_env.py
"""
import sys
import os
import subprocess
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent
os.chdir(ROOT)

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"

errors = []
warnings = []


def check(label: str, condition: bool, fail_msg: str = "", warn: bool = False) -> bool:
    tag = PASS if condition else (WARN if warn else FAIL)
    status = "ok" if condition else fail_msg
    print(f"  {tag}  {label}: {status}")
    if not condition:
        if warn:
            warnings.append(f"{label}: {fail_msg}")
        else:
            errors.append(f"{label}: {fail_msg}")
    return condition


# ──────────────────────────────────────────────
# 1. Python 版本
# ──────────────────────────────────────────────
print("\n[1/7] Python 版本检查")
vi = sys.version_info
py_ok = vi >= (3, 10)
check("Python >= 3.10", py_ok, f"当前版本 {vi.major}.{vi.minor}.{vi.micro}，需要 3.10+")
print(f"       当前: Python {vi.major}.{vi.minor}.{vi.micro} ({sys.executable})")

# ──────────────────────────────────────────────
# 2. 必要依赖包
# ──────────────────────────────────────────────
print("\n[2/7] Python 依赖包检查")
REQUIRED_PACKAGES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("sqlalchemy", "sqlalchemy"),
    ("aiosqlite", "aiosqlite"),
    ("greenlet", "greenlet"),
    ("httpx", "httpx"),
    ("loguru", "loguru"),
    ("apscheduler", "apscheduler"),
    ("cryptography", "cryptography"),
    ("jwt", "PyJWT"),
    ("pyotp", "pyotp"),
    ("qrcode", "qrcode[pil]"),
    ("aiofiles", "aiofiles"),
    ("dotenv", "python-dotenv"),
]
OPTIONAL_PACKAGES = [
    ("ccxt", "ccxt (实盘交易/行情数据支持)"),
    ("asyncpg", "asyncpg (PostgreSQL 支持)"),
    ("redis", "redis (Redis 缓存支持)"),
    ("prometheus_client", "prometheus-client (监控指标)"),
]

for import_name, pkg_name in REQUIRED_PACKAGES:
    spec = importlib.util.find_spec(import_name)
    check(f"  {pkg_name}", spec is not None, f"未安装，请执行: pip install {pkg_name}")

print("\n  可选依赖 (缺失不影响基础运行):")
for import_name, pkg_name in OPTIONAL_PACKAGES:
    spec = importlib.util.find_spec(import_name)
    check(f"  {pkg_name}", spec is not None, "未安装（可选）", warn=True)

# ──────────────────────────────────────────────
# 3. 环境变量 / .env 文件
# ──────────────────────────────────────────────
print("\n[3/7] 环境配置检查")
env_file = ROOT / ".env"
check(".env 文件存在", env_file.exists(), f"文件不存在，请从 .env.example 复制并填写: cp .env.example .env")

if env_file.exists():
    from dotenv import dotenv_values
    env_vals = dotenv_values(env_file)

    jwt_secret = env_vals.get("JWT_SECRET", "")
    check("JWT_SECRET 已设置", bool(jwt_secret), "未设置，建议生成: python -c \"import secrets; print(secrets.token_hex(32))\"", warn=True)

    live_trading = env_vals.get("LIVE_TRADING", "false").lower() == "true"
    ccxt_available = importlib.util.find_spec("ccxt") is not None
    if live_trading:
        check("LIVE_TRADING=true 时 ccxt 已安装",
              ccxt_available,
              "实盘交易必须安装 ccxt；当前 32-bit Python 可能需要换 64-bit Python")
        check("LIVE_TRADING=true 时 EXCHANGE_API_KEY 已设置",
              bool(env_vals.get("EXCHANGE_API_KEY", "")),
              "实盘交易必须设置 Exchange API Key")
        check("LIVE_TRADING=true 时 EXCHANGE_API_SECRET 已设置",
              bool(env_vals.get("EXCHANGE_API_SECRET", "")),
              "实盘交易必须设置 Exchange API Secret")
        check("LIVE_TRADING=true 时 JWT_SECRET 已设置",
              bool(jwt_secret),
              "实盘交易必须设置 JWT_SECRET")
    else:
        print(f"  {INFO}  LIVE_TRADING=false (模拟交易模式，安全)")
        if not ccxt_available:
            print(f"  {INFO}  ccxt 未安装：纸交易/后台可启动，实盘交易和交易所实时行情不可用")

    db_url = env_vals.get("DATABASE_URL", "sqlite+aiosqlite:///./data/server.db")
    check("DATABASE_URL 已配置", bool(db_url), "DATABASE_URL 未设置")
    print(f"  {INFO}  数据库: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    redis_enabled = env_vals.get("REDIS_ENABLED", "false").lower() == "true"
    if redis_enabled:
        redis_url = env_vals.get("REDIS_URL", "redis://localhost:6379/0")
        print(f"  {INFO}  Redis: {redis_url}")
    else:
        print(f"  {INFO}  Redis: 已禁用 (使用内存缓存)")

# ──────────────────────────────────────────────
# 4. 必要目录
# ──────────────────────────────────────────────
print("\n[4/7] 必要目录检查")
REQUIRED_DIRS = [
    ROOT / "data",
    ROOT / "data" / "backups",
    ROOT / "logs",
    ROOT / "trade_logs",
    ROOT / "static",
]
for d in REQUIRED_DIRS:
    exists = d.exists()
    if not exists:
        try:
            d.mkdir(parents=True, exist_ok=True)
            check(f"{d.relative_to(ROOT)}", True, "（已自动创建）")
        except Exception as e:
            check(f"{d.relative_to(ROOT)}", False, f"无法创建: {e}")
    else:
        check(f"{d.relative_to(ROOT)}", True)

# ──────────────────────────────────────────────
# 5. 必要源文件
# ──────────────────────────────────────────────
print("\n[5/7] 关键源文件检查")
REQUIRED_FILES = [
    ROOT / "app.py",
    ROOT / "core" / "config.py",
    ROOT / "core" / "database.py",
    ROOT / "core" / "security.py",
    ROOT / "core" / "cache.py",
    ROOT / "core" / "middleware.py",
    ROOT / "core" / "auth.py",
    ROOT / "routers" / "webhook.py",
    ROOT / "routers" / "auth.py",
    ROOT / "routers" / "admin.py",
    ROOT / "routers" / "user.py",
    ROOT / "static" / "index.html",
    ROOT / "static" / "login.html",
]
for f in REQUIRED_FILES:
    check(str(f.relative_to(ROOT)), f.exists(), "文件缺失")

# ──────────────────────────────────────────────
# 6. 配置加载测试
# ──────────────────────────────────────────────
print("\n[6/7] 配置加载测试")
try:
    sys.path.insert(0, str(ROOT))
    from core.config import settings
    check("core.config 加载成功", True)
    check("settings.app_name 正常", bool(settings.app_name))
    check("settings.exchange 配置正常", settings.exchange is not None)
    check("settings.database.url 已设置", bool(settings.database.url))
    print(f"  {INFO}  应用名称: {settings.app_name} v{settings.app_version}")
    print(f"  {INFO}  Exchange: {settings.exchange.name}")
    print(f"  {INFO}  数据库驱动: {'PostgreSQL' if 'postgresql' in settings.database.url else 'SQLite'}")
except Exception as e:
    check("core.config 加载", False, f"{type(e).__name__}: {e}")

# ──────────────────────────────────────────────
# 7. 端口可用性
# ──────────────────────────────────────────────
print("\n[7/7] 端口检查")
import socket
port = 8000
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    result = s.connect_ex(("127.0.0.1", port))
    s.close()
    if result == 0:
        check(f"端口 {port} 可用", False, f"端口已被占用，请停止占用进程或修改 PORT 环境变量", warn=True)
    else:
        check(f"端口 {port} 可用", True)
except Exception:
    check(f"端口 {port} 检测", False, "检测失败", warn=True)

# ──────────────────────────────────────────────
# 汇总
# ──────────────────────────────────────────────
print("\n" + "=" * 55)
if errors:
    print(f"[FAIL] 发现 {len(errors)} 个错误（必须修复才能启动）:")
    for e in errors:
        print(f"   - {e}")
if warnings:
    print(f"[WARN] 发现 {len(warnings)} 个警告（建议修复）:")
    for w in warnings:
        print(f"   - {w}")
if not errors and not warnings:
    print("[PASS] 所有检查通过！可以启动服务器。")
    print("\n启动命令:")
    print("  uvicorn app:app --host 0.0.0.0 --port 8000")
elif not errors:
    print("\n[PASS] 无致命错误，可以启动服务器（建议先处理警告）。")
    print("\n启动命令:")
    print("  uvicorn app:app --host 0.0.0.0 --port 8000")
else:
    print("\n[FAIL] 请修复以上错误后再启动。")
    sys.exit(1)
