# QuantPilot AI - 閮ㄧ讲鎸囧崡

> 鐗堟湰锛歷4.1.0 | 鏈€鍚庢洿鏂帮細2026-04

---

## 鐩綍

1. [绯荤粺瑕佹眰](#1-绯荤粺瑕佹眰)
2. [蹇€熷惎鍔紙鏈湴寮€鍙戯級](#2-蹇€熷惎鍔ㄦ湰鍦板紑鍙?
3. [鐢熶骇閮ㄧ讲锛圖ocker锛塢(#3-鐢熶骇閮ㄧ讲docker)
4. [鐢熶骇閮ㄧ讲锛堣８鏈猴級](#4-鐢熶骇閮ㄧ讲瑁告満)
5. [鐜鍙橀噺閰嶇疆](#5-鐜鍙橀噺閰嶇疆)
6. [甯歌闂鎺掓煡](#6-甯歌闂鎺掓煡)
7. [瀹夊叏鍔犲浐娓呭崟](#7-瀹夊叏鍔犲浐娓呭崟)

---

## 1. 绯荤粺瑕佹眰

| 缁勪欢 | 鏈€浣庣増鏈?| 璇存槑 |
|------|---------|------|
| Python | **3.10+** | 椤圭洰浣跨敤 `X\|Y` union 绫诲瀷锛?.9 涓嶅吋瀹?|
| pip | 21.0+ | - |
| Docker | 24.0+ | 浠?Docker 閮ㄧ讲闇€瑕?|
| Docker Compose | 2.20+ | 浠?Docker 閮ㄧ讲闇€瑕?|
| SQLite | 3.37+ | 榛樿鏁版嵁搴擄紙鍐呯疆浜?Python锛?|
| PostgreSQL | 14+ | 鐢熶骇鐜鎺ㄨ崘 |
| Redis | 6.0+ | 鍙€夛紝缂哄け鏃惰嚜鍔ㄤ娇鐢ㄥ唴瀛樼紦瀛?|

**纭欢鎺ㄨ崘锛堢敓浜э級锛?*
- CPU锛?鏍?
- 鍐呭瓨锛?12MB+锛堝惈 AI 鍒嗘瀽寤鸿 1GB+锛?
- 纾佺洏锛?0GB+锛堟暟鎹簱 + 鏃ュ織锛?

---

## 2. 蹇€熷惎鍔紙鏈湴寮€鍙戯級

### 2.1 鍏嬮殕骞惰繘鍏ョ洰褰?

```bash
cd signal-server
```

### 2.2 瀹夎渚濊禆

```bash
# 鎺ㄨ崘浣跨敤铏氭嫙鐜
python3 -m venv venv
source venv/bin/activate        # Linux/macOS
# 鎴?
.\venv\Scripts\Activate.ps1     # Windows PowerShell

pip install -r requirements.txt
```

### 2.3 鍒涘缓閰嶇疆鏂囦欢

```bash
cp .env.example .env
# 缂栬緫 .env锛岃嚦灏戝～鍐欎互涓嬪瓧娈碉細
#   JWT_SECRET=<闅忔満32瀛楄妭鍗佸叚杩涘埗>
#   WEBHOOK_SECRET=<浣犵殑TradingView Webhook瀵嗙爜>
```

鐢熸垚瀹夊叏瀵嗛挜锛?

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 2.4 鐜妫€鏌ワ紙鎺ㄨ崘锛?

```bash
python scripts/check_env.py
```

鎵€鏈夋鏌ラ€氳繃鍚庯細

### 2.5 鍚姩鏈嶅姟鍣?

**Windows锛?*
```powershell
.\scripts\start.ps1
# 鎴栨墜鍔細
$env:PYTHONIOENCODING="utf-8"; python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

**Linux/macOS锛?*
```bash
bash scripts/start.sh
# 鎴栨墜鍔細
PYTHONIOENCODING=utf-8 uvicorn app:app --host 0.0.0.0 --port 8000
```

### 2.6 楠岃瘉鏈嶅姟

```bash
curl http://localhost:8000/health
# 鏈熸湜杈撳嚭: {"status":"healthy","version":"4.1.0","database":"ok","cache":"ok"}
```

鎵撳紑娴忚鍣ㄨ闂?`http://localhost:8000`锛屼娇鐢ㄩ粯璁よ处鍙风櫥褰曪細
- 鐢ㄦ埛鍚嶏細`admin`
- 瀵嗙爜锛歚123456`锛堥娆＄櫥褰曞悗璇风珛鍗充慨鏀癸紒锛?

---

## 3. 鐢熶骇閮ㄧ讲锛圖ocker锛?

### 3.1 鍓嶇疆鍑嗗

```bash
# 纭繚 .env 鏂囦欢宸查厤缃紙鍙傝€冪5鑺傦級
cp .env.example .env
# 璁剧疆寮哄瘑鐮?
echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)" >> .env
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env
echo "WEBHOOK_SECRET=$(openssl rand -hex 16)" >> .env
```

### 3.2 鏋勫缓骞跺惎鍔?

```bash
docker compose up -d --build
```

### 3.3 鏌ョ湅鍚姩鏃ュ織

```bash
docker compose logs -f signal-server
```

### 3.4 楠岃瘉鏈嶅姟

```bash
curl http://localhost:8000/health
```

### 3.5 鏈嶅姟绠＄悊

```bash
# 鍋滄鎵€鏈夋湇鍔?
docker compose down

# 閲嶅惎搴旂敤锛堜笉閲嶅缓鏁版嵁搴擄級
docker compose restart signal-server

# 鏇存柊浠ｇ爜鍚庨噸鏂版瀯寤?
docker compose up -d --build signal-server

# 鏌ョ湅鎵€鏈夊鍣ㄧ姸鎬?
docker compose ps
```

### 3.6 鏁版嵁澶囦唤

```bash
# 澶囦唤 SQLite 鏁版嵁搴擄紙濡備娇鐢?SQLite锛?
docker compose exec signal-server cp /app/data/server.db /app/data/backups/server_$(date +%Y%m%d).db

# 澶囦唤 PostgreSQL 鏁版嵁
docker compose exec postgres pg_dump -U signal signal_server > backup_$(date +%Y%m%d).sql
```

---

## 4. 鐢熶骇閮ㄧ讲锛堣８鏈猴級

閫傜敤浜庝笉浣跨敤 Docker 鐨勬湇鍔″櫒銆?

### 4.1 瀹夎 Python 3.10+

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3.12 python3.12-venv python3-pip -y

# CentOS/RHEL
sudo dnf install python3.12 -y
```

### 4.2 鍒涘缓绯荤粺鐢ㄦ埛

```bash
sudo useradd --system --no-create-home --shell /bin/false signal
sudo mkdir -p /opt/signal-server
sudo chown signal:signal /opt/signal-server
```

### 4.3 閮ㄧ讲浠ｇ爜

```bash
sudo cp -r . /opt/signal-server/
cd /opt/signal-server
sudo -u signal python3.12 -m venv venv
sudo -u signal ./venv/bin/pip install -r requirements.txt
```

### 4.4 閰嶇疆 systemd 鏈嶅姟

鍒涘缓 `/etc/systemd/system/signal-server.service`锛?

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

### 4.5 閰嶇疆 Nginx 鍙嶅悜浠ｇ悊

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

## 5. 鐜鍙橀噺閰嶇疆

### 5.1 蹇呭～鍙橀噺

| 鍙橀噺 | 璇存槑 | 绀轰緥 |
|------|------|------|
| `JWT_SECRET` | JWT 绛惧悕瀵嗛挜锛?2瀛楄妭鍗佸叚杩涘埗锛?| `openssl rand -hex 32` |
| `WEBHOOK_SECRET` | TradingView Webhook 瀵嗙爜 | 鑷畾涔夊瓧绗︿覆 |
| `DATABASE_URL` | 鏁版嵁搴撹繛鎺ヤ覆 | 瑙佷笅鏂?|

### 5.2 鏁版嵁搴撻厤缃?

```bash
# SQLite锛堝紑鍙?灏忚妯＄敓浜э級
DATABASE_URL=sqlite+aiosqlite:///./data/server.db

# PostgreSQL锛堟帹鑽愮敓浜э級
DATABASE_URL=postgresql+asyncpg://鐢ㄦ埛鍚?瀵嗙爜@涓绘満:5432/鏁版嵁搴撳悕
```

### 5.3 浜ゆ槗鎵€閰嶇疆

```bash
EXCHANGE=binance          # binance / okx / bybit / bitget / gate / coinbase
EXCHANGE_API_KEY=xxx
EXCHANGE_API_SECRET=xxx
EXCHANGE_PASSWORD=xxx     # 閮ㄥ垎浜ゆ槗鎵€闇€瑕侊紙濡?OKX锛?
LIVE_TRADING=false        # 瀹炵洏蹇呴』鏄惧紡璁句负 true
EXCHANGE_SANDBOX_MODE=false
```

### 5.4 AI 閰嶇疆

```bash
AI_PROVIDER=deepseek      # openai / anthropic / deepseek / custom
DEEPSEEK_API_KEY=xxx
# 鎴?
OPENAI_API_KEY=xxx
# 鎴?
ANTHROPIC_API_KEY=xxx
```

### 5.5 Telegram 閫氱煡锛堝彲閫夛級

```bash
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx
```

### 5.6 Redis锛堝彲閫夛級

```bash
REDIS_ENABLED=true
REDIS_URL=redis://localhost:6379/0
```

---

## 6. 甯歌闂鎺掓煡

### Q: 鍚姩鎶?`SettingsError: error parsing value for field "exchange"`

**鍘熷洜**锛氭棫鐗?`core/config.py` 浣跨敤 `BaseSettings` 鏃?pydantic-settings v2.14+ 灏嗙幆澧冨彉閲?`EXCHANGE=binance` 璇В鏋愪负 JSON銆?

**瑙ｅ喅**锛氬凡淇锛宍Settings` 绫诲凡鏀逛负 `BaseModel` + `from_env()` 宸ュ巶鏂规硶銆傜‘璁?`core/config.py` 鏈熬鏄細
```python
settings = Settings.from_env()
```

---

### Q: Windows 鎺у埗鍙板嚭鐜?`UnicodeEncodeError: 'gbk' codec can't encode character`

**鍘熷洜**锛歭oguru 鏃ュ織涓惈 emoji锛學indows GBK 缁堢鏃犳硶缂栫爜銆?

**瑙ｅ喅**锛氬凡淇锛宍app.py` 涓?console handler 浣跨敤 UTF-8 鍖呰鍣ㄣ€傝繍琛屾椂璁剧疆锛?
```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

---

### Q: `ModuleNotFoundError: No module named 'passlib'` 鎴?`'jose'`

**瑙ｅ喅**锛?
```bash
pip install "passlib[bcrypt]" "python-jose[cryptography]"
```
杩欎袱涓寘宸茶ˉ鍏呭埌 `requirements.txt`锛屾墽琛?`pip install -r requirements.txt` 鍗冲彲銆?

---

### Q: 鏁版嵁搴撶洰褰曚笉瀛樺湪锛孲QLite 鏃犳硶鍒涘缓鏂囦欢

**瑙ｅ喅**锛?
```bash
mkdir -p data/backups logs trade_logs
```
鍚姩鑴氭湰浼氳嚜鍔ㄥ垱寤鸿繖浜涚洰褰曘€?

---

### Q: `/health` 杩斿洖 `database: error`

**妫€鏌ユ楠?*锛?
1. 纭 `DATABASE_URL` 閰嶇疆姝ｇ‘
2. 瀵逛簬 PostgreSQL锛氱‘璁ゆ湇鍔″湪杩愯锛岀敤鎴锋潈闄愭纭?
3. 鏌ョ湅鏃ュ織锛歚docker compose logs signal-server` 鎴?`logs/server_*.log`

---

### Q: Docker 瀹瑰櫒鍚姩鍚庣珛鍗抽€€鍑?

```bash
docker compose logs signal-server
```
甯歌鍘熷洜锛?
- `.env` 涓己灏戝繀瑕佸彉閲?
- PostgreSQL 鍋ュ悍妫€鏌ユ湭閫氳繃锛堢瓑寰呮椂闂翠笉澶燂級
- 渚濊禆瀹夎澶辫触

---

### Q: Python 3.9 杩愯鎶?`TypeError: unsupported type`

椤圭洰浣跨敤 Python 3.10+ 鐨勫師鐢熺被鍨嬫彁绀鸿娉曪紙`list[str]`銆乣X | Y`锛夛紝蹇呴』鍗囩骇 Python銆?

---

## 7. 瀹夊叏鍔犲浐娓呭崟

鐢熶骇閮ㄧ讲鍓嶈閫愰」纭锛?

- [ ] `JWT_SECRET` 浣跨敤鑷冲皯 32 瀛楄妭闅忔満鍊硷紙`openssl rand -hex 32`锛?
- [ ] `WEBHOOK_SECRET` 宸茶缃紝涓?TradingView 鍛婅閰嶇疆涓€鑷?
- [ ] `DEFAULT_ADMIN_PASSWORD` 宸蹭慨鏀癸紙鎴栭娆＄櫥褰曞悗绔嬪嵆淇敼锛?
- [ ] `LIVE_TRADING=false`锛堥櫎闈炵‘璁よ瀹炵洏锛屾槑纭涓?`true`锛?
- [ ] PostgreSQL 瀵嗙爜涓嶄娇鐢ㄩ粯璁ゅ€?`signal`
- [ ] 绔彛 `8000` 涓嶅鍏綉鐩存帴鏆撮湶锛岄€氳繃 Nginx/鍙嶅悜浠ｇ悊璁块棶
- [ ] HTTPS 宸查厤缃紙Let's Encrypt 鎴栧晢涓氳瘉涔︼級
- [ ] 鏃ュ織鐩綍璁剧疆浜嗚疆杞紙宸查厤缃細100MB / 30澶╋級
- [ ] 瀹氭湡澶囦唤 `data/` 鐩綍

---

## 闄勫綍锛氱洰褰曠粨鏋?

```
signal-server/
鈹溾攢鈹€ app.py                  # FastAPI 涓诲叆鍙?
鈹溾攢鈹€ requirements.txt        # Python 渚濊禆
鈹溾攢鈹€ .env                    # 鐜閰嶇疆锛堜笉鎻愪氦鍒?git锛?
鈹溾攢鈹€ .env.example            # 閰嶇疆妯℃澘
鈹溾攢鈹€ Dockerfile              # Docker 闀滃儚瀹氫箟
鈹溾攢鈹€ docker-compose.yml      # Docker Compose 缂栨帓
鈹溾攢鈹€ DEPLOY.md               # 鏈枃妗?
鈹溾攢鈹€ core/
鈹?  鈹溾攢鈹€ config.py           # 閰嶇疆鍔犺浇锛圔aseModel + from_env锛?
鈹?  鈹溾攢鈹€ database.py         # SQLAlchemy 寮傛鏁版嵁搴撳眰
鈹?  鈹溾攢鈹€ security.py         # Fernet 鍔犲瘑 + 瀵嗙爜鍝堝笇
鈹?  鈹溾攢鈹€ auth.py             # JWT 璁よ瘉
鈹?  鈹溾攢鈹€ cache.py            # Redis / 鍐呭瓨缂撳瓨
鈹?  鈹斺攢鈹€ middleware.py       # CORS銆侀檺娴併€丆SRF 涓棿浠?
鈹溾攢鈹€ routers/
鈹?  鈹溾攢鈹€ webhook.py          # TradingView Webhook 澶勭悊
鈹?  鈹溾攢鈹€ auth.py             # 鐧诲綍/娉ㄥ唽鎺ュ彛
鈹?  鈹溾攢鈹€ admin.py            # 绠＄悊鍛樻帴鍙?
鈹?  鈹溾攢鈹€ user.py             # 鐢ㄦ埛鎺ュ彛
鈹?  鈹斺攢鈹€ subscription.py     # 璁㈤槄绠＄悊
鈹溾攢鈹€ scripts/
鈹?  鈹溾攢鈹€ check_env.py        # 閮ㄧ讲鍓嶇幆澧冩鏌ュ伐鍏?
鈹?  鈹溾攢鈹€ start.ps1           # Windows 涓€閿惎鍔ㄨ剼鏈?
鈹?  鈹斺攢鈹€ start.sh            # Linux/macOS 涓€閿惎鍔ㄨ剼鏈?
鈹溾攢鈹€ static/                 # 鍓嶇闈欐€佹枃浠?
鈹溾攢鈹€ data/                   # 鏁版嵁搴撴枃浠跺拰澶囦唤
鈹溾攢鈹€ logs/                   # 搴旂敤鏃ュ織锛堣疆杞級
鈹斺攢鈹€ trade_logs/             # 浜ゆ槗璁板綍鏃ュ織
```
