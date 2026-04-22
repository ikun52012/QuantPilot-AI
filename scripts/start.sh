#!/usr/bin/env bash
# TradingView Signal Server - Linux/macOS 一键启动脚本
# 使用方式: bash scripts/start.sh
# 可选参数: bash scripts/start.sh --host 0.0.0.0 --port 8000 --debug --check

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# 默认参数
SERVER_HOST="0.0.0.0"
SERVER_PORT="8000"
DEBUG_MODE=false
CHECK_ONLY=false

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --host) SERVER_HOST="$2"; shift 2 ;;
        --port) SERVER_PORT="$2"; shift 2 ;;
        --debug) DEBUG_MODE=true; shift ;;
        --check) CHECK_ONLY=true; shift ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

# ─────────────────────────────────────────────
# 颜色输出
# ─────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; GRAY='\033[0;37m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}[OK]${NC}   $1"; }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $1"; }
info() { echo -e "  ${CYAN}[INFO]${NC} $1"; }

echo ""
echo -e "  ${CYAN}TradingView Signal Server - 启动脚本${NC}"
echo -e "  ${CYAN}======================================${NC}"

# ─────────────────────────────────────────────
# 查找 Python 3.10+
# ─────────────────────────────────────────────
find_python() {
    for py in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$py" &>/dev/null; then
            ver=$("$py" -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')" 2>/dev/null)
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -gt 3 ] || ([ "$major" -eq 3 ] && [ "$minor" -ge 10 ]); then
                echo "$py"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=$(find_python) || { fail "未找到 Python 3.10+，请先安装"; exit 1; }
ok "Python: $($PYTHON --version)"

# ─────────────────────────────────────────────
# 设置 UTF-8
# ─────────────────────────────────────────────
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
export LANG=en_US.UTF-8

# ─────────────────────────────────────────────
# 检查 .env 文件
# ─────────────────────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        warn ".env 不存在，从 .env.example 复制..."
        cp .env.example .env
        info "请编辑 .env 文件填写必要配置后重新运行"
        exit 0
    else
        fail ".env 和 .env.example 都不存在"
        exit 1
    fi
fi
ok ".env 文件存在"

# ─────────────────────────────────────────────
# 创建必要目录
# ─────────────────────────────────────────────
for dir in "data" "data/backups" "logs" "trade_logs"; do
    mkdir -p "$dir"
done
ok "必要目录已创建"

# ─────────────────────────────────────────────
# 运行环境检查
# ─────────────────────────────────────────────
echo ""
info "运行环境检查..."
if ! "$PYTHON" scripts/check_env.py; then
    fail "环境检查失败，请修复上述问题"
    exit 1
fi

if $CHECK_ONLY; then
    info "环境检查完成（--check 模式，不启动服务器）"
    exit 0
fi

# ─────────────────────────────────────────────
# 检查并安装依赖
# ─────────────────────────────────────────────
if ! "$PYTHON" -c "import uvicorn" 2>/dev/null; then
    info "正在安装依赖..."
    "$PYTHON" -m pip install -r requirements.txt -q
    ok "依赖安装完成"
fi

# ─────────────────────────────────────────────
# 启动服务器
# ─────────────────────────────────────────────
echo ""
ok "启动 Signal Server..."
info "地址: http://${SERVER_HOST}:${SERVER_PORT}"
echo -e "  ${GRAY}按 Ctrl+C 停止服务器${NC}"
echo ""

UVICORN_ARGS=("app:app" "--host" "$SERVER_HOST" "--port" "$SERVER_PORT")
if $DEBUG_MODE; then
    UVICORN_ARGS+=("--reload")
    warn "热重载模式已启用"
fi

exec "$PYTHON" -m uvicorn "${UVICORN_ARGS[@]}"
