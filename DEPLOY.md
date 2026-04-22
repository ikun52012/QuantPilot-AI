# QuantPilot AI - 部署指南

> 版本：v4.1.0 | 最后更新：2026-04

---

## 目录

1. [系统要求](#1-系统要求)
2. [快速启动（本地开发）](#2-快速启动本地开发)
3. [生产部署（Docker）](#3-生产部署docker)
4. [生产部署（裸机）](#4-生产部署裸机)
5. [环境变量配置](#5-环境变量配置)
6. [常见问题排查](#6-常见问题排查)
7. [安全加固清单](#7-安全加固清单)

---

## 1. 系统要求

| 组件 | 最低版本 | 说明 |
|------|---------|------|
| Python | **3.10+** | 项目使用 `X\|Y` union 类型，3.9 不兼容 |
| pip | 21.0+ | - |
| Docker | 24.0+ | 仅 Docker 部署需要 |
| Docker Compose | 2.20+ | 仅 Docker 部署需要 |
| SQLite | 3.37+ | 默认数据库（内置于 Python） |
| PostgreSQL | 14+ | 生产环境推荐 |
| Redis | 6.0+ | 可选，缺失时自动使用内存缓存 |

**硬件推荐（生产）：**
- CPU：2核+
- 内存：512MB+（含 AI 分析建议 1GB+）
- 磁盘：10GB+（数据库 + 日志）

---

## 2. 快速启动（本地开发）

### 2.1 克隆并进入目录

```bash
cd signal-server
```

### 2.2 安装依赖

```bash
# 推荐使用虚拟环境
python3 -m venv venv
source venv/bin/activate        # Linux/macOS
# 或
.\venv\Scripts\Activate.ps1     # Windows PowerShell

pip install -r requirements.txt
```

### 2.3 创建配置文件

```bash
cp .env.example .env
# 编辑 .env，至少填写以下字段：
#   JWT_SECRET=<随机32字节十六进制>
#   WEBHOOK_SECRET=<你的TradingView Webhook密码>
```

生成安全密钥：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 2.4 环境检查（推荐）

```bash
python scripts/check_env.py
```

所有检查通过后：

### 2.5 启动服务器

**Windows：**
```powershell
.\scripts\start.ps1
# 或手动：
$env:PYTHONIOENCODING="utf-8"; python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

**Linux/macOS：**
```bash
bash scripts/start.sh
# 或手动：
PYTHONIOENCODING=utf-8 uvicorn app:app --host 0.0.0.0 --port 8000
```

### 2.6 验证服务

```bash
curl http://localhost:8000/health
# 期望输出: {"status":"healthy","version":"4.1.0","database":"ok","cache":"ok"}
```

打开浏览器访问 `http://localhost:8000`，使用默认账号登录：
- 用户名：`admin`
- 密码：`123456`（首次登录后请立即修改！）

---

## 3. 生产部署（Docker）

### 3.1 前置准备

```bash
# 确保 .env 文件已配置（参考第5节）
cp .env.example .env
# 设置强密码
echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)" >> .env
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env
echo "WEBHOOK_SECRET=$(openssl rand -hex 16)" >> .env
```

### 3.2 构建并启动

```bash
docker compose up -d --build
```

### 3.3 查看启动日志

```bash
docker compose logs -f signal-server
```

### 3.4 验证服务

```bash
curl http://localhost:8000/health
```

### 3.5 服务管理

```bash
# 停止所有服务
docker compose down

# 重启应用（不重建数据库）
docker compose restart signal-server

# 更新代码后重新构建
docker compose up -d --build signal-server

# 查看所有容器状态
docker compose ps
```

### 3.6 数据备份

```bash
# 备份 SQLite 数据库（如使用 SQLite）
docker compose exec signal-server cp /app/data/server.db /app/data/backups/server_$(date +%Y%m%d).db

# 备份 PostgreSQL 数据
docker compose exec postgres pg_dump -U signal signal_server > backup_$(date +%Y%m%d).sql
```

---

## 4. 生产部署（裸机）

适用于不使用 Docker 的服务器。

### 4.1 安装 Python 3.10+

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3.12 python3.12-venv python3-pip -y

# CentOS/RHEL
sudo dnf install python3.12 -y
```

### 4.2 创建系统用户

```bash
sudo useradd --system --no-create-home --shell /bin/false signal
sudo mkdir -p /opt/signal-server
sudo chown signal:signal /opt/signal-server
```

### 4.3 部署代码

```bash
sudo cp -r . /opt/signal-server/
cd /opt/signal-server
sudo -u signal python3.12 -m venv venv
sudo -u signal ./venv/bin/pip install -r requirements.txt
```

### 4.4 配置 systemd 服务

创建 `/etc/systemd/system/signal-server.service`：

```ini
[Unit]
Description=QuantPilot AI
After=network.target postgresql.service

[Service]
Type=simple
User=signal
Group=signal
WorkingDirectory=/opt/signal-server
EnvironmentFile=/opt/signal-server/.env
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUTF8=1
ExecStart=/opt/signal-server/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable signal-server
sudo systemctl start signal-server
sudo systemctl status signal-server
```

### 4.5 配置 Nginx 反向代理

```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 90s;
    }
}
```

---

## 5. 环境变量配置

### 5.1 必填变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `JWT_SECRET` | JWT 签名密钥（32字节十六进制） | `openssl rand -hex 32` |
| `WEBHOOK_SECRET` | TradingView Webhook 密码 | 自定义字符串 |
| `DATABASE_URL` | 数据库连接串 | 见下方 |

### 5.2 数据库配置

```bash
# SQLite（开发/小规模生产）
DATABASE_URL=sqlite+aiosqlite:///./data/server.db

# PostgreSQL（推荐生产）
DATABASE_URL=postgresql+asyncpg://用户名:密码@主机:5432/数据库名
```

### 5.3 交易所配置

```bash
EXCHANGE=binance          # binance / okx / bybit / bitget / gate / coinbase
EXCHANGE_API_KEY=xxx
EXCHANGE_API_SECRET=xxx
EXCHANGE_PASSWORD=xxx     # 部分交易所需要（如 OKX）
LIVE_TRADING=false        # 实盘必须显式设为 true
EXCHANGE_SANDBOX_MODE=false
```

### 5.4 AI 配置

```bash
AI_PROVIDER=deepseek      # openai / anthropic / deepseek / custom
DEEPSEEK_API_KEY=xxx
# 或
OPENAI_API_KEY=xxx
# 或
ANTHROPIC_API_KEY=xxx
```

### 5.5 Telegram 通知（可选）

```bash
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx
```

### 5.6 Redis（可选）

```bash
REDIS_ENABLED=true
REDIS_URL=redis://localhost:6379/0
```

---

## 6. 常见问题排查

### Q: 启动报 `SettingsError: error parsing value for field "exchange"`

**原因**：旧版 `core/config.py` 使用 `BaseSettings` 时 pydantic-settings v2.14+ 将环境变量 `EXCHANGE=binance` 误解析为 JSON。

**解决**：已修复，`Settings` 类已改为 `BaseModel` + `from_env()` 工厂方法。确认 `core/config.py` 末尾是：
```python
settings = Settings.from_env()
```

---

### Q: Windows 控制台出现 `UnicodeEncodeError: 'gbk' codec can't encode character`

**原因**：loguru 日志中含 emoji，Windows GBK 终端无法编码。

**解决**：已修复，`app.py` 中 console handler 使用 UTF-8 包装器。运行时设置：
```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

---

### Q: `ModuleNotFoundError: No module named 'passlib'` 或 `'jose'`

**解决**：
```bash
pip install "passlib[bcrypt]" "python-jose[cryptography]"
```
这两个包已补充到 `requirements.txt`，执行 `pip install -r requirements.txt` 即可。

---

### Q: 数据库目录不存在，SQLite 无法创建文件

**解决**：
```bash
mkdir -p data/backups logs trade_logs
```
启动脚本会自动创建这些目录。

---

### Q: `/health` 返回 `database: error`

**检查步骤**：
1. 确认 `DATABASE_URL` 配置正确
2. 对于 PostgreSQL：确认服务在运行，用户权限正确
3. 查看日志：`docker compose logs signal-server` 或 `logs/server_*.log`

---

### Q: Docker 容器启动后立即退出

```bash
docker compose logs signal-server
```
常见原因：
- `.env` 中缺少必要变量
- PostgreSQL 健康检查未通过（等待时间不够）
- 依赖安装失败

---

### Q: Python 3.9 运行报 `TypeError: unsupported type`

项目使用 Python 3.10+ 的原生类型提示语法（`list[str]`、`X | Y`），必须升级 Python。

---

## 7. 安全加固清单

生产部署前请逐项确认：

- [ ] `JWT_SECRET` 使用至少 32 字节随机值（`openssl rand -hex 32`）
- [ ] `WEBHOOK_SECRET` 已设置，与 TradingView 告警配置一致
- [ ] `DEFAULT_ADMIN_PASSWORD` 已修改（或首次登录后立即修改）
- [ ] `LIVE_TRADING=false`（除非确认要实盘，明确设为 `true`）
- [ ] PostgreSQL 密码不使用默认值 `signal`
- [ ] 端口 `8000` 不对公网直接暴露，通过 Nginx/反向代理访问
- [ ] HTTPS 已配置（Let's Encrypt 或商业证书）
- [ ] 日志目录设置了轮转（已配置：100MB / 30天）
- [ ] 定期备份 `data/` 目录

---

## 附录：目录结构

```
signal-server/
├── app.py                  # FastAPI 主入口
├── requirements.txt        # Python 依赖
├── .env                    # 环境配置（不提交到 git）
├── .env.example            # 配置模板
├── Dockerfile              # Docker 镜像定义
├── docker-compose.yml      # Docker Compose 编排
├── DEPLOY.md               # 本文档
├── core/
│   ├── config.py           # 配置加载（BaseModel + from_env）
│   ├── database.py         # SQLAlchemy 异步数据库层
│   ├── security.py         # Fernet 加密 + 密码哈希
│   ├── auth.py             # JWT 认证
│   ├── cache.py            # Redis / 内存缓存
│   └── middleware.py       # CORS、限流、CSRF 中间件
├── routers/
│   ├── webhook.py          # TradingView Webhook 处理
│   ├── auth.py             # 登录/注册接口
│   ├── admin.py            # 管理员接口
│   ├── user.py             # 用户接口
│   └── subscription.py     # 订阅管理
├── scripts/
│   ├── check_env.py        # 部署前环境检查工具
│   ├── start.ps1           # Windows 一键启动脚本
│   └── start.sh            # Linux/macOS 一键启动脚本
├── static/                 # 前端静态文件
├── data/                   # 数据库文件和备份
├── logs/                   # 应用日志（轮转）
└── trade_logs/             # 交易记录日志
```
