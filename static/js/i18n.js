(function () {
    const supported = ['en', 'zh', 'ja', 'ko', 'es'];
    const names = { en: 'English', zh: '中文', ja: '日本語', ko: '한국어', es: 'Español' };
    const originalText = new WeakMap();
    const originalAttr = new WeakMap();
    let translations = {};
    let currentLang = localStorage.getItem('qp_lang') || (navigator.language || 'en').split('-')[0] || 'en';
    if (!supported.includes(currentLang)) currentLang = 'en';

    const publicPhrases = {
        zh: {
            'Platform': '平台',
            'Features': '功能',
            'Exchanges': '交易所',
            'Sign In': '登录',
            'Get Started': '开始使用',
            'Create Free Account': '免费创建账号',
            'See How It Works': '了解工作原理',
            'Open-source · Multi-exchange · AI-ready': '开源 · 多交易所 · AI 就绪',
            'Algorithmic Crypto Trading,': '算法加密货币交易，',
            'Automated.': '自动化。',
            'Connect TradingView signals to Binance, OKX, Bybit, and more. QuantPilot handles signal validation, risk management, multi-TP execution, and smart trailing stops — so you can focus on strategy.': '连接 TradingView 信号到 Binance、OKX、Bybit 等交易所。QuantPilot 处理信号验证、风控管理、多级止盈和智能移动止损 —— 让你专注策略。',
            'Exchanges': '交易所',
            'Take-Profit Levels': '止盈层级',
            'AI Providers': 'AI 提供商',
            'Position Monitor': '持仓监控',
            'THE PROCESS': '流程',
            'From Signal to Execution': '从信号到执行',
            'QuantPilot bridges the gap between your trading signals and exchange execution, with institutional-grade risk controls at every step.': 'QuantPilot 连接交易信号和交易所执行，在每一个步骤都设置了机构级风控。',
            'Signal Ingestion': '信号接收',
            'Receive signals via TradingView webhooks with IP-based rate limiting and replay protection.': '通过 TradingView webhook 接收信号，支持 IP 限速和防重放保护。',
            'AI Analysis': 'AI 分析',
            'GPT-4o, Claude, DeepSeek, or Mistral validate signal quality, assess risk, and recommend position size.': 'GPT-4o、Claude、DeepSeek 或 Mistral 验证信号质量、评估风险并推荐仓位大小。',
            'Risk Filtering': '风控过滤',
            'Daily trade limits, correlation checks, leverage caps, and pre-filters ensure only sound trades execute.': '每日交易限制、相关性检查、杠杆上限和预过滤器确保只执行合理的交易。',
            'Execution & Monitoring': '执行与监控',
            'Place multi-TP orders, smart trailing stops, and 24/7 position monitoring with ghost detection.': '多级止盈下单、智能移动止损和 24/7 持仓监控，含幽灵持仓检测。',
            'CAPABILITIES': '核心能力',
            'Built for Serious Traders': '为专业交易者打造',
            'Production-grade features designed from real trading experience, not hype.': '源自真实交易经验的生产级功能，不是空谈。',
            'Multi-Exchange': '多交易所',
            'Execute simultaneously on Binance, OKX, Bybit, Bitget, Gate.io, and Coinbase. Unified API with automatic symbol resolution and contract size handling.': '在 Binance、OKX、Bybit、Bitget、Gate.io 和 Coinbase 上同步执行。统一 API，自动解析交易对和处理合约大小。',
            'Multi-Take-Profit': '多级止盈',
            'Up to 4 TP levels with quantity split. Each level tracked independently with order verification and automatic re-placement on exchange cancellation.': '最多 4 个止盈级别，按数量分配。每个级别独立追踪，支持订单验证和交易所取消后自动重置。',
            'Smart Trailing Stop': '智能移动止损',
            '5 trailing modes: none, auto (AI-selected), moving, breakeven-on-TP1, step-trailing. Re-evaluated on limit order fills for changed market conditions.': '5 种止损模式：无、自动（AI 选择）、移动追踪、TP1 保本、阶梯追踪。限价单成交后根据市场变化重新评估。',
            'AI-Powered Decisions': 'AI 决策',
            'Optional AI analysis with confidence scoring, market regime detection, and trend strength assessment. Built-in caching for sub-second repeated signals.': '可选 AI 分析，包含置信度评分、市场状态识别和趋势强度评估。内置缓存支持亚秒级重复信号。',
            'Institutional Risk Controls': '机构级风控',
            'Pre-filter DB for daily limits, emergency stop, position correlation checks, leverage rollback on failure, and hard SL boundaries (0.1%–50% from entry).': '预过滤数据库限制每日交易次数、紧急停止、持仓相关性检查、杠杆失败回滚，以及硬止损边界（0.1%–50% 距入场价）。',
            '24/7 Position Monitor': '24/7 持仓监控',
            'Continuous reconciliation with exchange data. Ghost position detection with API-failure safeguards. Protective order verification and automatic re-placement.': '持续与交易所数据对账。幽灵持仓检测含 API 故障保护。保护性订单验证和自动重置。',
            'SIGNAL PROCESSING': '信号处理',
            'Reliable Signal Ingestion': '可靠的信号接收',
            'Secure webhooks with replay protection, IP rate limiting, and daily trade caps ensure your signals are processed exactly once without over-trading.': '安全的 webhook 防重放保护、IP 限速和每日交易上限，确保信号只处理一次，不会过度交易。',
            'HMAC signature verification for signal authenticity': 'HMAC 签名验证确保信号真实性',
            'IP-based rate limiting (60 req/min) prevents abuse': '基于 IP 的限速（60 请求/分钟）防止滥用',
            'Fingerprint-based replay protection against duplicates': '指纹防重放保护防止重复处理',
            'Per-user daily trade limits with database persistence': '每用户每日交易限制，数据库持久化',
            'Emergency stop, pause, and read-only trading modes': '紧急停止、暂停和只读交易模式',
            'RISK MANAGEMENT': '风控管理',
            'Multi-Layer Risk Protection': '多层风控保护',
            'Every trade passes through 7 risk checks before execution, and positions are continuously monitored after entry.': '每笔交易在执行前经过 7 层风控检查，入场后持仓持续监控。',
            'Pre-filter database enforces daily trade caps per user': '预过滤数据库强制执行每用户每日交易上限',
            'AI confidence and risk score gating': 'AI 置信度和风险评分门控',
            'Correlation risk prevents over-concentration': '相关性风险防止过度集中',
            'Leverage setup with automatic rollback on failure': '杠杆设置失败时自动回滚',
            'Hard SL boundaries: 0.1% minimum, 50% maximum from entry': '硬止损边界：最低 0.1%，最高 50% 距入场价',
            'Ghost position detection with exchange API failure safeguards': '幽灵持仓检测，含交易所 API 故障保护',
            'Daily trade limit check': '每日交易限制检查',
            'AI confidence ≥ threshold': 'AI 置信度 ≥ 阈值',
            'Correlation risk check': '相关性风控检查',
            'Execute on exchange': '在交易所执行',
            'Multi-Exchange Execution': '多交易所执行',
            'Unified API across major exchanges with automatic symbol resolution, contract size handling, and hedge mode support.': '跨主流交易所的统一 API，支持自动解析交易对、合约大小处理和对冲模式。',
            'Start Trading Smarter Today': '立即开始智能交易',
            'Open-source, self-hosted, and free. Deploy QuantPilot in minutes and turn your TradingView signals into automated execution.': '开源、自托管、免费。几分钟内部署 QuantPilot，将 TradingView 信号转化为自动化交易。',
            'View on GitHub': '在 GitHub 查看',
            'Register': '注册',
            'Home': '首页',
            'Back to Home': '返回首页',
            'Welcome Back': '欢迎回来',
            'Sign in to QuantPilot AI': '登录 QuantPilot AI',
            'Username': '用户名',
            'Password': '密码',
            'Enter username': '输入用户名',
            'Enter password': '输入密码',
            'Two-Factor Authentication': '双重验证',
            'Enter the 6-digit code from your authenticator app, or a recovery code.': '输入身份验证器中的 6 位验证码，或使用恢复码。',
            'Verification Code': '验证码',
            'Verify': '验证',
            'Back to login': '返回登录',
            "Don't have an account?": '还没有账号？',
            'Create one': '创建一个',
            'Join QuantPilot AI': '加入 QuantPilot AI',
            'Email': '邮箱',
            'Choose a username': '选择用户名',
            'you@example.com': 'you@example.com',
            'Create a password': '创建密码',
            'Minimum 8 characters': '至少 8 个字符',
            'Confirm Password': '确认密码',
            'Confirm password': '再次输入密码',
            'Invite Code': '邀请码',
            'Enter invite code': '输入邀请码',
            'Already have an account?': '已有账号？',
            'Passwords do not match': '两次密码不一致',
            'Creating account...': '正在创建账号...',
            'Account created! Redirecting...': '账号已创建，正在跳转...',
            'Signing in...': '正在登录...',
            'Verifying...': '正在验证...',
            'Your session expired. Please sign in again.': '登录已过期，请重新登录。',
            'Login failed': '登录失败',
            'Registration failed': '注册失败',
            'Verification failed': '验证失败',
            'Webhook payload (JSON)': 'Webhook 载荷 (JSON)',
            'Pre-filter pipeline': '预过滤管线'
        },
        ja: {
            'Platform': 'プラットフォーム',
            'Features': '機能',
            'Exchanges': '取引所',
            'Sign In': 'ログイン',
            'Get Started': '開始',
            'Create Free Account': '無料アカウント作成',
            'See How It Works': '仕組みを見る',
            'Open-source · Multi-exchange · AI-ready': 'オープンソース · マルチ取引所 · AI対応',
            'Algorithmic Crypto Trading,': 'アルゴリズム暗号通貨取引、',
            'Automated.': '自動化。',
            'Connect TradingView signals to Binance, OKX, Bybit, and more. QuantPilot handles signal validation, risk management, multi-TP execution, and smart trailing stops — so you can focus on strategy.': 'TradingViewシグナルをBinance、OKX、Bybitなどに接続。QuantPilotがシグナル検証、リスク管理、マルチTP実行、スマートトレーリングストップを処理。',
            'Take-Profit Levels': '利確レベル',
            'AI Providers': 'AIプロバイダー',
            'Position Monitor': 'ポジション監視',
            'THE PROCESS': 'プロセス',
            'From Signal to Execution': 'シグナルから実行まで',
            'QuantPilot bridges the gap between your trading signals and exchange execution, with institutional-grade risk controls at every step.': 'QuantPilotがトレードシグナルと取引所実行の橋渡しを行い、各ステップで機関グレードのリスク管理を提供。',
            'Signal Ingestion': 'シグナル受信',
            'Receive signals via TradingView webhooks with IP-based rate limiting and replay protection.': 'IP制限とリプレイ保護付きのTradingViewウェブフックでシグナルを受信。',
            'AI Analysis': 'AI分析',
            'GPT-4o, Claude, DeepSeek, or Mistral validate signal quality, assess risk, and recommend position size.': 'GPT-4o、Claude、DeepSeek、Mistralがシグナル品質を検証、リスクを評価、ポジションサイズを推奨。',
            'Risk Filtering': 'リスクフィルタリング',
            'Daily trade limits, correlation checks, leverage caps, and pre-filters ensure only sound trades execute.': '1日取引制限、相関チェック、レバレッジ上限、プレフィルターで健全な取引のみ実行。',
            'Execution & Monitoring': '実行と監視',
            'Place multi-TP orders, smart trailing stops, and 24/7 position monitoring with ghost detection.': 'マルチTP注文、スマートトレーリングストップ、ゴースト検出付き24/7ポジション監視。',
            'CAPABILITIES': 'コア機能',
            'Built for Serious Traders': '本格トレーダー向け',
            'Production-grade features designed from real trading experience, not hype.': '実際の取引経験から設計された本番グレードの機能。',
            'Multi-Exchange': 'マルチ取引所',
            'Execute simultaneously on Binance, OKX, Bybit, Bitget, Gate.io, and Coinbase. Unified API with automatic symbol resolution and contract size handling.': 'Binance、OKX、Bybit、Bitget、Gate.io、Coinbaseで同時実行。統合API、自動シンボル解決、契約サイズ処理。',
            'Multi-Take-Profit': 'マルチテイクプロフィット',
            'Up to 4 TP levels with quantity split. Each level tracked independently with order verification and automatic re-placement on exchange cancellation.': '最大4段階のTPと数量分割。各レベルを独立追跡、注文検証と取引所キャンセル時の自動再配置。',
            'Smart Trailing Stop': 'スマートトレーリングストップ',
            '5 trailing modes: none, auto (AI-selected), moving, breakeven-on-TP1, step-trailing. Re-evaluated on limit order fills for changed market conditions.': '5つのトレーリングモード：なし、自動（AI選択）、ムービング、TP1ブレークイーブン、ステップトレーリング。',
            'AI-Powered Decisions': 'AI駆動の意思決定',
            'Optional AI analysis with confidence scoring, market regime detection, and trend strength assessment. Built-in caching for sub-second repeated signals.': '信頼度スコアリング、市場レジーム検出、トレンド強度評価によるオプションAI分析。',
            'Institutional Risk Controls': '機関グレードのリスク管理',
            'Pre-filter DB for daily limits, emergency stop, position correlation checks, leverage rollback on failure, and hard SL boundaries (0.1%–50% from entry).': '日次制限のプレフィルターDB、緊急ストップ、ポジション相関チェック、レバレッジロールバック、ハードSL境界。',
            '24/7 Position Monitor': '24/7ポジション監視',
            'Continuous reconciliation with exchange data. Ghost position detection with API-failure safeguards. Protective order verification and automatic re-placement.': '取引所データとの継続的照合。API障害セーフガード付きゴーストポジション検出。',
            'SIGNAL PROCESSING': 'シグナル処理',
            'Reliable Signal Ingestion': '信頼性の高いシグナル受信',
            'Secure webhooks with replay protection, IP rate limiting, and daily trade caps ensure your signals are processed exactly once without over-trading.': 'リプレイ保護、IP制限、日次取引上限付きのセキュアウェブフック。',
            'HMAC signature verification for signal authenticity': 'HMAC署名検証でシグナルの真正性を確認',
            'IP-based rate limiting (60 req/min) prevents abuse': 'IP制限（60req/min）で乱用を防止',
            'Fingerprint-based replay protection against duplicates': 'フィンガープリントベースのリプレイ保護で重複を防止',
            'Per-user daily trade limits with database persistence': 'ユーザーごとの日次取引制限をDBで永続化',
            'Emergency stop, pause, and read-only trading modes': '緊急ストップ、一時停止、読み取り専用取引モード',
            'RISK MANAGEMENT': 'リスク管理',
            'Multi-Layer Risk Protection': '多層リスク保護',
            'Every trade passes through 7 risk checks before execution, and positions are continuously monitored after entry.': '各取引は実行前に7つのリスクチェックを通過。エントリー後もポジションを継続監視。',
            'Pre-filter database enforces daily trade caps per user': 'プレフィルターDBがユーザーごとの日次取引上限を強制',
            'AI confidence and risk score gating': 'AI信頼度とリスクスコアによるゲーティング',
            'Correlation risk prevents over-concentration': '相関リスクで過集中を防止',
            'Leverage setup with automatic rollback on failure': 'レバレッジセットアップ失敗時の自動ロールバック',
            'Hard SL boundaries: 0.1% minimum, 50% maximum from entry': 'ハードSL境界：エントリーから最低0.1%、最大50%',
            'Ghost position detection with exchange API failure safeguards': '取引所API障害セーフガード付きゴーストポジション検出',
            'Daily trade limit check': '日次取引制限チェック',
            'AI confidence ≥ threshold': 'AI信頼度 ≥ 閾値',
            'Correlation risk check': '相関リスクチェック',
            'Execute on exchange': '取引所で実行',
            'Multi-Exchange Execution': 'マルチ取引所実行',
            'Unified API across major exchanges with automatic symbol resolution, contract size handling, and hedge mode support.': '主要取引所の統合API。自動シンボル解決、契約サイズ処理、ヘッジモード対応。',
            'Start Trading Smarter Today': '今すぐスマートトレーディングを始める',
            'Open-source, self-hosted, and free. Deploy QuantPilot in minutes and turn your TradingView signals into automated execution.': 'オープンソース、セルフホスト、無料。数分でQuantPilotをデプロイし、TradingViewシグナルを自動実行に。',
            'View on GitHub': 'GitHubで見る',
            'Docs': 'ドキュメント',
            'Register': '登録',
            'Home': 'ホーム',
            'Back to Home': 'ホームへ戻る',
            'Welcome Back': 'おかえりなさい',
            'Username': 'ユーザー名',
            'Password': 'パスワード',
            'Email': 'メール',
            'Confirm Password': 'パスワード確認',
            'Verify': '確認'
        },
        ko: {
            'Platform': '플랫폼',
            'Features': '기능',
            'Exchanges': '거래소',
            'Sign In': '로그인',
            'Get Started': '시작하기',
            'Create Free Account': '무료 계정 만들기',
            'See How It Works': '작동 원리 보기',
            'Open-source · Multi-exchange · AI-ready': '오픈소스 · 멀티 거래소 · AI 대응',
            'Algorithmic Crypto Trading,': '알고리즘 암호화폐 거래,',
            'Automated.': '자동화.',
            'Connect TradingView signals to Binance, OKX, Bybit, and more. QuantPilot handles signal validation, risk management, multi-TP execution, and smart trailing stops — so you can focus on strategy.': 'TradingView 시그널을 Binance, OKX, Bybit 등에 연결. QuantPilot이 시그널 검증, 리스크 관리, 멀티 TP 실행, 스마트 트레일링 스톱을 처리합니다.',
            'Take-Profit Levels': '익절 레벨',
            'AI Providers': 'AI 제공자',
            'Position Monitor': '포지션 모니터',
            'THE PROCESS': '프로세스',
            'From Signal to Execution': '시그널에서 실행까지',
            'QuantPilot bridges the gap between your trading signals and exchange execution, with institutional-grade risk controls at every step.': 'QuantPilot이 트레이딩 시그널과 거래소 실행 사이를 연결하며, 모든 단계에서 기관급 리스크 관리를 제공합니다.',
            'Signal Ingestion': '시그널 수신',
            'Receive signals via TradingView webhooks with IP-based rate limiting and replay protection.': 'IP 기반 속도 제한과 리플레이 보호가 포함된 TradingView 웹훅으로 시그널을 수신합니다.',
            'AI Analysis': 'AI 분석',
            'GPT-4o, Claude, DeepSeek, or Mistral validate signal quality, assess risk, and recommend position size.': 'GPT-4o, Claude, DeepSeek, Mistral이 시그널 품질을 검증하고 리스크를 평가하며 포지션 크기를 추천합니다.',
            'Risk Filtering': '리스크 필터링',
            'Daily trade limits, correlation checks, leverage caps, and pre-filters ensure only sound trades execute.': '일일 거래 한도, 상관관계 확인, 레버리지 상한, 프리필터로 건전한 거래만 실행합니다.',
            'Execution & Monitoring': '실행 및 모니터링',
            'Place multi-TP orders, smart trailing stops, and 24/7 position monitoring with ghost detection.': '멀티 TP 주문, 스마트 트레일링 스톱, 고스트 감지 기능의 24/7 포지션 모니터링.',
            'CAPABILITIES': '핵심 기능',
            'Built for Serious Traders': '전문 트레이더를 위해 설계',
            'Production-grade features designed from real trading experience, not hype.': '실제 거래 경험에서 설계된 프로덕션급 기능.',
            'Multi-Exchange': '멀티 거래소',
            'Execute simultaneously on Binance, OKX, Bybit, Bitget, Gate.io, and Coinbase. Unified API with automatic symbol resolution and contract size handling.': 'Binance, OKX, Bybit, Bitget, Gate.io, Coinbase에서 동시 실행. 통합 API, 자동 심볼 해결, 계약 크기 처리.',
            'Multi-Take-Profit': '멀티 테이크프로핏',
            'Up to 4 TP levels with quantity split. Each level tracked independently with order verification and automatic re-placement on exchange cancellation.': '최대 4개 TP 레벨과 수량 분할. 각 레벨 독립 추적, 주문 검증 및 거래소 취소 시 자동 재배치.',
            'Smart Trailing Stop': '스마트 트레일링 스톱',
            '5 trailing modes: none, auto (AI-selected), moving, breakeven-on-TP1, step-trailing. Re-evaluated on limit order fills for changed market conditions.': '5가지 트레일링 모드: 없음, 자동(AI 선택), 무빙, TP1 브레이크이븐, 스텝 트레일링.',
            'AI-Powered Decisions': 'AI 기반 의사결정',
            'Optional AI analysis with confidence scoring, market regime detection, and trend strength assessment. Built-in caching for sub-second repeated signals.': '신뢰도 점수, 시장 레짐 감지, 트렌드 강도 평가를 통한 옵셔널 AI 분석. 서브초 반복 시그널용 내장 캐시.',
            'Institutional Risk Controls': '기관급 리스크 관리',
            'Pre-filter DB for daily limits, emergency stop, position correlation checks, leverage rollback on failure, and hard SL boundaries (0.1%–50% from entry).': '일일 한도 프리필터 DB, 긴급 정지, 포지션 상관관계 확인, 레버리지 롤백, 하드 SL 경계.',
            '24/7 Position Monitor': '24/7 포지션 모니터',
            'Continuous reconciliation with exchange data. Ghost position detection with API-failure safeguards. Protective order verification and automatic re-placement.': '거래소 데이터와 지속적 조정. API 장애 안전장치가 있는 고스트 포지션 감지.',
            'SIGNAL PROCESSING': '시그널 처리',
            'Reliable Signal Ingestion': '신뢰할 수 있는 시그널 수신',
            'Secure webhooks with replay protection, IP rate limiting, and daily trade caps ensure your signals are processed exactly once without over-trading.': '리플레이 보호, IP 속도 제한, 일일 거래 상한이 포함된 보안 웹훅.',
            'HMAC signature verification for signal authenticity': 'HMAC 서명 검증으로 시그널 진위 확인',
            'IP-based rate limiting (60 req/min) prevents abuse': 'IP 기반 속도 제한(60req/min)으로 남용 방지',
            'Fingerprint-based replay protection against duplicates': '지문 기반 리플레이 보호로 중복 방지',
            'Per-user daily trade limits with database persistence': '사용자별 일일 거래 한도를 DB에 영구 저장',
            'Emergency stop, pause, and read-only trading modes': '긴급 정지, 일시 정지, 읽기 전용 거래 모드',
            'RISK MANAGEMENT': '리스크 관리',
            'Multi-Layer Risk Protection': '다층 리스크 보호',
            'Every trade passes through 7 risk checks before execution, and positions are continuously monitored after entry.': '모든 거래는 실행 전 7개의 리스크 검사를 통과하며, 진입 후에도 포지션을 지속적으로 모니터링합니다.',
            'Pre-filter database enforces daily trade caps per user': '프리필터 DB가 사용자별 일일 거래 상한 강제',
            'AI confidence and risk score gating': 'AI 신뢰도 및 리스크 점수 게이팅',
            'Correlation risk prevents over-concentration': '상관관계 리스크로 과집중 방지',
            'Leverage setup with automatic rollback on failure': '레버리지 설정 실패 시 자동 롤백',
            'Hard SL boundaries: 0.1% minimum, 50% maximum from entry': '하드 SL 경계: 진입에서 최소 0.1%, 최대 50%',
            'Ghost position detection with exchange API failure safeguards': '거래소 API 장애 안전장치가 있는 고스트 포지션 감지',
            'Daily trade limit check': '일일 거래 한도 확인',
            'AI confidence ≥ threshold': 'AI 신뢰도 ≥ 임계값',
            'Correlation risk check': '상관관계 리스크 확인',
            'Execute on exchange': '거래소에서 실행',
            'Multi-Exchange Execution': '멀티 거래소 실행',
            'Unified API across major exchanges with automatic symbol resolution, contract size handling, and hedge mode support.': '주요 거래소의 통합 API. 자동 심볼 해결, 계약 크기 처리, 헤지 모드 지원.',
            'Start Trading Smarter Today': '오늘 스마트 트레이딩 시작',
            'Open-source, self-hosted, and free. Deploy QuantPilot in minutes and turn your TradingView signals into automated execution.': '오픈소스, 셀프 호스트, 무료. 몇 분 안에 QuantPilot을 배포하고 TradingView 시그널을 자동 실행으로.',
            'View on GitHub': 'GitHub에서 보기',
            'Docs': '문서',
            'Home': '홈',
            'Register': '가입',
            'Create Account': '계정 만들기',
            'Back to Home': '홈으로 돌아가기',
            'Welcome Back': '다시 오신 것을 환영합니다',
            'Username': '사용자 이름',
            'Password': '비밀번호',
            'Email': '이메일',
            'Confirm Password': '비밀번호 확인',
            'Verify': '확인'
        },
        es: {
            'Platform': 'Plataforma',
            'Features': 'Funciones',
            'Exchanges': 'Exchanges',
            'Sign In': 'Iniciar sesión',
            'Get Started': 'Empezar',
            'Create Free Account': 'Crear cuenta gratis',
            'See How It Works': 'Ver cómo funciona',
            'Open-source · Multi-exchange · AI-ready': 'Código abierto · Multi-exchange · Compatible con IA',
            'Algorithmic Crypto Trading,': 'Trading Cripto Algorítmico,',
            'Automated.': 'Automatizado.',
            'Connect TradingView signals to Binance, OKX, Bybit, and more. QuantPilot handles signal validation, risk management, multi-TP execution, and smart trailing stops — so you can focus on strategy.': 'Conecta señales de TradingView a Binance, OKX, Bybit y más. QuantPilot gestiona validación de señales, gestión de riesgos, ejecución multi-TP y trailing stops inteligentes.',
            'Take-Profit Levels': 'Niveles de Take-Profit',
            'AI Providers': 'Proveedores IA',
            'Position Monitor': 'Monitor de Posiciones',
            'THE PROCESS': 'EL PROCESO',
            'From Signal to Execution': 'De Señal a Ejecución',
            'QuantPilot bridges the gap between your trading signals and exchange execution, with institutional-grade risk controls at every step.': 'QuantPilot conecta tus señales de trading con la ejecución en exchange, con controles de riesgo institucionales en cada paso.',
            'Signal Ingestion': 'Ingestión de Señales',
            'Receive signals via TradingView webhooks with IP-based rate limiting and replay protection.': 'Recibe señales vía webhooks de TradingView con límite de tasa por IP y protección contra reproducción.',
            'AI Analysis': 'Análisis IA',
            'GPT-4o, Claude, DeepSeek, or Mistral validate signal quality, assess risk, and recommend position size.': 'GPT-4o, Claude, DeepSeek o Mistral validan la calidad de señal, evalúan riesgo y recomiendan el tamaño de posición.',
            'Risk Filtering': 'Filtrado de Riesgos',
            'Daily trade limits, correlation checks, leverage caps, and pre-filters ensure only sound trades execute.': 'Límites diarios, verificación de correlación, topes de apalancamiento y prefiltros aseguran solo operaciones sólidas.',
            'Execution & Monitoring': 'Ejecución y Monitoreo',
            'Place multi-TP orders, smart trailing stops, and 24/7 position monitoring with ghost detection.': 'Órdenes multi-TP, trailing stops inteligentes y monitoreo 24/7 con detección de posiciones fantasma.',
            'CAPABILITIES': 'CAPACIDADES',
            'Built for Serious Traders': 'Hecho para Traders Serios',
            'Production-grade features designed from real trading experience, not hype.': 'Funciones de grado producción diseñadas desde experiencia real de trading.',
            'Multi-Exchange': 'Multi-Exchange',
            'Execute simultaneously on Binance, OKX, Bybit, Bitget, Gate.io, and Coinbase. Unified API with automatic symbol resolution and contract size handling.': 'Ejecuta simultáneamente en Binance, OKX, Bybit, Bitget, Gate.io y Coinbase. API unificada con resolución automática de símbolos.',
            'Multi-Take-Profit': 'Multi-Take-Profit',
            'Up to 4 TP levels with quantity split. Each level tracked independently with order verification and automatic re-placement on exchange cancellation.': 'Hasta 4 niveles de TP con división de cantidad. Cada nivel rastreado independientemente con verificación de órdenes y reemplazo automático.',
            'Smart Trailing Stop': 'Trailing Stop Inteligente',
            '5 trailing modes: none, auto (AI-selected), moving, breakeven-on-TP1, step-trailing. Re-evaluated on limit order fills for changed market conditions.': '5 modos de trailing: ninguno, automático (IA), móvil, breakeven-en-TP1, step-trailing.',
            'AI-Powered Decisions': 'Decisiones con IA',
            'Optional AI analysis with confidence scoring, market regime detection, and trend strength assessment. Built-in caching for sub-second repeated signals.': 'Análisis IA opcional con puntuación de confianza, detección de régimen de mercado y evaluación de fuerza de tendencia.',
            'Institutional Risk Controls': 'Controles de Riesgo Institucionales',
            'Pre-filter DB for daily limits, emergency stop, position correlation checks, leverage rollback on failure, and hard SL boundaries (0.1%–50% from entry).': 'Base de datos de prefiltros para límites diarios, parada de emergencia, verificación de correlación y límites duros de SL.',
            '24/7 Position Monitor': 'Monitor de Posiciones 24/7',
            'Continuous reconciliation with exchange data. Ghost position detection with API-failure safeguards. Protective order verification and automatic re-placement.': 'Conciliación continua con datos de exchange. Detección de posiciones fantasma con salvaguardas ante fallos de API.',
            'SIGNAL PROCESSING': 'PROCESAMIENTO DE SEÑALES',
            'Reliable Signal Ingestion': 'Ingestión de Señales Confiable',
            'Secure webhooks with replay protection, IP rate limiting, and daily trade caps ensure your signals are processed exactly once without over-trading.': 'Webhooks seguros con protección contra reproducción, límite de tasa por IP y topes diarios.',
            'HMAC signature verification for signal authenticity': 'Verificación de firma HMAC para autenticidad de señales',
            'IP-based rate limiting (60 req/min) prevents abuse': 'Límite de tasa por IP (60 req/min) previene abuso',
            'Fingerprint-based replay protection against duplicates': 'Protección contra reproducción basada en huella digital',
            'Per-user daily trade limits with database persistence': 'Límites diarios por usuario con persistencia en base de datos',
            'Emergency stop, pause, and read-only trading modes': 'Parada de emergencia, pausa y modos de trading de solo lectura',
            'RISK MANAGEMENT': 'GESTIÓN DE RIESGOS',
            'Multi-Layer Risk Protection': 'Protección de Riesgo Multicapa',
            'Every trade passes through 7 risk checks before execution, and positions are continuously monitored after entry.': 'Cada operación pasa por 7 verificaciones de riesgo antes de la ejecución.',
            'Pre-filter database enforces daily trade caps per user': 'La base de datos de prefiltros impone topes diarios por usuario',
            'AI confidence and risk score gating': 'Gating por confianza IA y puntuación de riesgo',
            'Correlation risk prevents over-concentration': 'Riesgo de correlación previene sobre-concentración',
            'Leverage setup with automatic rollback on failure': 'Configuración de apalancamiento con rollback automático en fallo',
            'Hard SL boundaries: 0.1% minimum, 50% maximum from entry': 'Límites duros de SL: mínimo 0.1%, máximo 50% desde entrada',
            'Ghost position detection with exchange API failure safeguards': 'Detección de posiciones fantasma con salvaguardas ante fallos de API',
            'Daily trade limit check': 'Verificación de límite diario',
            'AI confidence ≥ threshold': 'Confianza IA ≥ umbral',
            'Correlation risk check': 'Verificación de riesgo de correlación',
            'Execute on exchange': 'Ejecutar en exchange',
            'Multi-Exchange Execution': 'Ejecución Multi-Exchange',
            'Unified API across major exchanges with automatic symbol resolution, contract size handling, and hedge mode support.': 'API unificada entre exchanges principales con resolución automática de símbolos y soporte de modo hedge.',
            'Start Trading Smarter Today': 'Empieza a Operar de Forma Más Inteligente Hoy',
            'Open-source, self-hosted, and free. Deploy QuantPilot in minutes and turn your TradingView signals into automated execution.': 'Código abierto, self-hosted y gratuito. Despliega QuantPilot en minutos y convierte tus señales en ejecución automatizada.',
            'View on GitHub': 'Ver en GitHub',
            'Docs': 'Documentación',
            'Home': 'Inicio',
            'Register': 'Registrarse',
            'Create Account': 'Crear cuenta',
            'Back to Home': 'Volver al inicio',
            'Welcome Back': 'Bienvenido de nuevo',
            'Username': 'Usuario',
            'Password': 'Contraseña',
            'Email': 'Email',
            'Confirm Password': 'Confirmar contraseña',
            'Verify': 'Verificar'
        }
    };

    function deepGet(obj, key) {
        return key.split('.').reduce((cur, part) => cur && cur[part], obj);
    }

    function translate(key, fallback) {
        return deepGet(translations, key) || fallback || key;
    }

    function translatePhrase(text) {
        if (currentLang === 'en') return text;
        const map = publicPhrases[currentLang] || {};
        const normalized = text.replace(/\s+/g, ' ').trim();
        return map[normalized] || map[text] || text;
    }

    async function load(lang) {
        if (!supported.includes(lang)) lang = 'en';
        currentLang = lang;
        localStorage.setItem('qp_lang', lang);
        try {
            const response = await fetch(`/api/i18n/public/translations/${lang}`, { credentials: 'include' });
            if (response.ok) {
                const payload = await response.json();
                translations = payload.translations || {};
            }
        } catch (err) {
            translations = {};
        }
    }

    function translateElement(el) {
        const key = el.getAttribute('data-i18n');
        const attr = el.getAttribute('data-i18n-attr');
        const fallback = el.getAttribute('data-i18n-fallback') || (attr ? el.getAttribute(attr) : el.textContent);
        const value = translate(key, fallback);
        if (attr) el.setAttribute(attr, value);
        else el.textContent = value;
    }

    function translateAttributes(el) {
        ['placeholder', 'aria-label', 'title'].forEach(attr => {
            const value = el.getAttribute(attr);
            if (!value) return;
            let store = originalAttr.get(el);
            if (!store) {
                store = {};
                originalAttr.set(el, store);
            }
            if (!store[attr]) store[attr] = value;
            el.setAttribute(attr, translatePhrase(store[attr]));
        });
    }

    function translateTextNodes() {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                const parent = node.parentElement;
                if (!parent) return NodeFilter.FILTER_REJECT;
                if (parent.closest('script,style,code,pre,[data-i18n]')) return NodeFilter.FILTER_REJECT;
                if (!node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
            }
        });
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        nodes.forEach(node => {
            if (!originalText.has(node)) originalText.set(node, node.nodeValue);
            const source = originalText.get(node);
            const match = source.match(/^(\s*)(.*?)(\s*)$/s);
            const translated = translatePhrase(match[2]);
            node.nodeValue = `${match[1]}${translated}${match[3]}`;
        });
    }

    function ensureSelector() {
        setupNavbarLangSelect();
        if (document.getElementById('public-lang-select')) return;
        const wrap = document.createElement('div');
        wrap.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:1000;background:rgba(255,255,255,.95);border:1px solid #e2e8f0;border-radius:8px;padding:6px;backdrop-filter:blur(12px);box-shadow:0 2px 8px rgba(0,0,0,.08)';
        const select = document.createElement('select');
        select.id = 'public-lang-select';
        select.style.cssText = 'background:#fff;color:#0f172a;border:1px solid #cbd5e1;border-radius:6px;padding:6px 8px;font:inherit;font-size:12px;cursor:pointer';
        supported.forEach(code => {
            const option = document.createElement('option');
            option.value = code;
            option.textContent = names[code];
            select.appendChild(option);
        });
        select.value = currentLang;
        select.addEventListener('change', async () => {
            await load(select.value);
            apply();
            const navSelect = document.getElementById('nav-lang-select');
            if (navSelect) navSelect.value = currentLang;
        });
        wrap.appendChild(select);
        document.body.appendChild(wrap);
    }

    function setupNavbarLangSelect() {
        const navSelect = document.getElementById('nav-lang-select');
        if (!navSelect) return;
        navSelect.value = currentLang;
        navSelect.addEventListener('change', async () => {
            await load(navSelect.value);
            apply();
            const bottomSelect = document.getElementById('public-lang-select');
            if (bottomSelect) bottomSelect.value = currentLang;
        });
    }

    function apply() {
        document.documentElement.lang = currentLang;
        document.querySelectorAll('[data-i18n]').forEach(translateElement);
        document.querySelectorAll('input,textarea,button,a,[title],[aria-label]').forEach(translateAttributes);
        translateTextNodes();
        const titleMap = {
            zh: 'QuantPilot — AI 驱动的算法加密货币交易平台',
            ja: 'QuantPilot — アルゴリズム暗号通貨取引プラットフォーム',
            ko: 'QuantPilot — 알고리즘 암호화폐 거래 플랫폼',
            es: 'QuantPilot — Plataforma de Trading Cripto Algorítmico'
        };
        if (titleMap[currentLang]) {
            document.title = titleMap[currentLang];
        }
        ensureSelector();
    }

    window.changePublicLanguage = async function (lang) {
        await load(lang);
        apply();
    };

    document.addEventListener('DOMContentLoaded', async () => {
        await load(currentLang);
        apply();
    });
})();
