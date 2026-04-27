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
            'Features': '功能',
            'AI Models': 'AI 模型',
            'Pipeline': '流程',
            'Exchanges': '交易所',
            'Sign In': '登录',
            'Get Started': '开始使用',
            'Start Trading Free': '免费开始交易',
            'Explore Features': '查看功能',
            'AI-Powered': 'AI 驱动',
            'Crypto Trading': '加密货币交易',
            'v4.4 — AI Risk Review · Multi-TP · Exchange Automation': 'v4.4 — AI 风险审核 · 多级止盈 · 交易所自动化',
            'Receive TradingView signals, filter weak setups with 15 fast checks, review them with OpenAI, Claude, DeepSeek, OpenRouter, or a custom provider, then execute across 6+ exchanges.': '接收 TradingView 信号，通过 15 项快速检查过滤弱信号，再使用 OpenAI、Claude、DeepSeek、OpenRouter 或自定义模型审核，并在 6+ 交易所执行。',
            'AI Providers': 'AI 提供商',
            'Filter Checks': '过滤检查',
            'TP Levels': '止盈层级',
            'Trailing Modes': '移动止损模式',
            'Everything You Need for': '智能交易所需的一切',
            'Smart Trading': '智能交易',
            'A complete trading pipeline from signal to execution, with strict risk controls and practical operations tooling.': '从信号到执行的完整交易流程，包含严格风控和实用运维工具。',
            'CORE': '核心',
            'COMPATIBLE': '兼容',
            'SAFE': '安全',
            'SECURE': '安全',
            'OPS': '运维',
            'Provider-Aware AI Review': '按提供商适配的 AI 审核',
            'Route analysis through OpenAI, Anthropic, DeepSeek, OpenRouter, or an OpenAI-compatible custom endpoint with configurable prompts and token limits.': '可通过 OpenAI、Anthropic、DeepSeek、OpenRouter 或 OpenAI 兼容自定义端点执行分析，并配置提示词与 token 限制。',
            'OpenRouter Support': 'OpenRouter 支持',
            "Use OpenRouter's OpenAI-compatible endpoint from the same AI settings surface while keeping direct provider integrations available.": '可在同一个 AI 设置界面使用 OpenRouter 的 OpenAI 兼容端点，同时保留直接提供商集成。',
            'Custom AI Analysis': '自定义 AI 分析',
            'Choose from OpenAI GPT-4o, Anthropic Claude, DeepSeek, or your own custom endpoint. Customize temperature, tokens, and system prompt.': '可选择 OpenAI GPT-4o、Anthropic Claude、DeepSeek 或自定义端点，并配置温度、token 和系统提示词。',
            'Realistic Paper Trading': '真实模拟交易',
            'Paper mode records simulated orders locally first, so strategies can be tested without sending live exchange orders.': '模拟模式会先在本地记录模拟订单，便于测试策略而不向真实交易所发送订单。',
            '15-Layer Pre-Filter': '15 层预过滤',
            'RSI extremes, funding rate guard, orderbook imbalance, market hours, consecutive loss protection, EMA alignment, and adaptive thresholds.': '覆盖 RSI 极值、资金费率保护、订单簿失衡、市场时段、连续亏损保护、EMA 对齐和自适应阈值。',
            '5 Trailing Stop Modes': '5 种移动止损模式',
            'Moving trailing, breakeven on TP1, step-down trailing, profit-% activated, or static stop-loss. Advanced exit management for any strategy.': '支持移动追踪、TP1 后保本、阶梯追踪、盈利百分比触发或固定止损，适配不同策略的高级出场管理。',
            'Per-User Webhooks': '用户独立 Webhook',
            'Each user can receive an isolated webhook secret, encrypted exchange settings, and subscription-aware live trading permissions.': '每个用户都有独立 webhook 密钥、加密交易所设置和按订阅控制的实盘权限。',
            'Operational Controls': '运营控制',
            'Admin diagnostics cover payment review, webhook events, position monitor state, backups, storage health, and audit history.': '管理诊断覆盖付款审核、webhook 事件、持仓监控状态、备份、存储健康和审计历史。',
            'Live Analytics': '实时分析',
            'Equity curves, win/loss distribution, Sharpe ratio, Sortino, profit factor, and AI confidence performance are available in the dashboard.': '仪表盘提供权益曲线、胜负分布、夏普、索提诺、利润因子和 AI 置信度表现。',
            'AI Provider Layer': 'AI 提供商层',
            'Choose a direct provider or an OpenAI-compatible route, then keep risk behavior consistent through one execution pipeline.': '可选择直接提供商或 OpenAI 兼容路由，并通过统一执行流程保持风控行为一致。',
            'Supported Models': '支持的模型',
            'Risk Decision Layer': '风控决策层',
            'Every executable trade has to pass fast rule checks, AI confidence thresholds, and validated exit planning before an order is created.': '每笔可执行交易在创建订单前都必须通过快速规则检查、AI 置信度阈值和出场计划验证。',
            'Daily limits, volatility, spread, volume, RSI, funding, and trend checks run before AI spend.': '在消耗 AI 调用前先检查日限制、波动率、价差、成交量、RSI、资金费率和趋势。',
            'Low-confidence or direction-conflicting analysis is rejected before execution.': '低置信度或方向冲突的分析会在执行前被拒绝。',
            'Opening trades require a valid stop loss and at least one take-profit target.': '开仓交易必须具备有效止损和至少一个止盈目标。',
            'Signal': '信号',
            'Processing Pipeline': '处理流程',
            'Every signal flows through a practical pipeline with fast checks, AI review, and protective exit validation.': '每个信号都会经过快速检查、AI 审核和保护性出场验证。',
            'Multi Take-Profit &': '多级止盈与',
            'Trailing Stop Strategies': '移动止损策略',
            'Maximize profits and protect gains with sophisticated exit management.': '通过更精细的出场管理扩大收益并保护利润。',
            'Take-Profit Levels': '止盈层级',
            'Trailing Stop Modes': '移动止损模式',
            'Supported Exchanges': '支持的交易所',
            "Seamlessly connect to the world's leading cryptocurrency exchanges via CCXT.": '通过 CCXT 无缝连接主流加密货币交易所。',
            'Ready to Start': '准备开始',
            'Trading Smarter': '更聪明地交易',
            'Create an account, subscribe to a plan, and start routing AI-reviewed trading signals to your exchange.': '创建账号、订阅套餐，并开始将 AI 审核后的交易信号路由到交易所。',
            'Create Account': '创建账号',
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
            'Verification failed': '验证失败'
        },
        ja: {
            'Features': '機能',
            'AI Models': 'AIモデル',
            'Pipeline': 'パイプライン',
            'Exchanges': '取引所',
            'Sign In': 'ログイン',
            'Get Started': '開始',
            'Home': 'ホーム',
            'Register': '登録',
            'Create Account': 'アカウント作成',
            'Back to Home': 'ホームへ戻る',
            'Welcome Back': 'おかえりなさい',
            'Username': 'ユーザー名',
            'Password': 'パスワード',
            'Email': 'メール',
            'Confirm Password': 'パスワード確認',
            'Verify': '確認'
        },
        ko: {
            'Features': '기능',
            'AI Models': 'AI 모델',
            'Pipeline': '파이프라인',
            'Exchanges': '거래소',
            'Sign In': '로그인',
            'Get Started': '시작하기',
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
            'Features': 'Funciones',
            'AI Models': 'Modelos IA',
            'Pipeline': 'Flujo',
            'Exchanges': 'Exchanges',
            'Sign In': 'Iniciar sesión',
            'Get Started': 'Empezar',
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
        if (document.getElementById('public-lang-select')) return;
        const wrap = document.createElement('div');
        wrap.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:1000;background:rgba(7,10,16,.9);border:1px solid rgba(148,163,184,.2);border-radius:8px;padding:6px;backdrop-filter:blur(12px)';
        const select = document.createElement('select');
        select.id = 'public-lang-select';
        select.style.cssText = 'background:#0b111c;color:#eef4fb;border:1px solid rgba(148,163,184,.24);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px';
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
        });
        wrap.appendChild(select);
        document.body.appendChild(wrap);
    }

    function apply() {
        document.documentElement.lang = currentLang;
        document.querySelectorAll('[data-i18n]').forEach(translateElement);
        document.querySelectorAll('input,textarea,button,a,[title],[aria-label]').forEach(translateAttributes);
        translateTextNodes();
        if (currentLang === 'zh') {
            document.title = document.title
                .replace('AI-Powered Crypto Trading Platform', 'AI 驱动的加密货币交易平台')
                .replace('Login - QuantPilot AI', '登录 - QuantPilot AI')
                .replace('Register — QuantPilot AI', '注册 — QuantPilot AI');
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
