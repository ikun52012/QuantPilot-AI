/**
 * QuantPilot AI - Dashboard Frontend Logic
 * v4.5 — AI command center redesign, trading controls, strategies, PWA, i18n content pages
 */

const API = '';
const USDT_PAYMENT_NETWORKS = [
    { id: 'TRC20', name: 'Tron (TRC20)' },
    { id: 'ERC20', name: 'Ethereum (ERC20)' },
    { id: 'BEP20', name: 'BSC (BEP20)' },
    { id: 'ARBITRUM', name: 'Arbitrum One' },
    { id: 'APT', name: 'Aptos (APT)' },
    { id: 'SOL', name: 'Solana (SPL)' },
];

let currentUserSettings = null;
let _strategyTemplates = [];
let _systemSocket = null;
let _priceSocket = null;
let _priceSocketTicker = '';
let _chartRealtimeState = null;
let _launchContext = null;

// ─── i18n / Multi-language Support ───
let _i18nCache = {};
let _currentLang = localStorage.getItem('qp_lang') || navigator.language.split('-')[0] || 'en';
const _supportedLangs = ['en', 'zh', 'ja', 'ko', 'es'];

async function loadTranslations(lang) {
    if (!_supportedLangs.includes(lang)) lang = 'en';
    if (_i18nCache[lang]) return _i18nCache[lang];
    try {
        const r = await fetch(`/api/i18n/translations/${lang}`, { credentials: 'include' });
        if (!r.ok) throw new Error('Failed to load translations');
        const data = await r.json();
        _i18nCache[lang] = data.translations || {};
        return _i18nCache[lang];
    } catch (e) {
        console.warn('[i18n] Translation load failed:', e);
        return {};
    }
}

function t(key, fallback) {
    const parts = key.split('.');
    let current = _i18nCache[_currentLang];
    if (!current) return fallback || key;
    for (const part of parts) {
        if (current && typeof current === 'object' && part in current) {
            current = current[part];
        } else {
            return fallback || key;
        }
    }
    return typeof current === 'string' ? current : (fallback || key);
}

function applyTranslations() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        const fallback = el.getAttribute('data-i18n-fallback') || el.textContent;
        const translation = t(key, fallback);
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
            if (el.getAttribute('data-i18n-attr') === 'placeholder') {
                el.placeholder = translation;
            } else {
                el.value = translation;
            }
        } else {
            el.textContent = translation;
        }
    });
}

async function changeLanguage(lang) {
    if (!_supportedLangs.includes(lang)) return;
    _currentLang = lang;
    localStorage.setItem('qp_lang', lang);
    await loadTranslations(lang);
    applyTranslations();
    // Update nav item texts
    updateNavTexts();
    // Update page title if on a recognized page
    const activePage = document.querySelector('.page.active');
    if (activePage) {
        const pageId = activePage.id.replace('page-', '');
        const navItem = document.querySelector(`.nav-item[data-page="${pageId}"]`);
        if (navItem) {
            const titleEl = document.getElementById('page-title');
            if (titleEl) titleEl.textContent = navItem.querySelector('span')?.textContent || pageId;
        }
    }
    await refreshAll().catch(() => {});
    showToast(t('messages.updated', 'Language updated'), 'success');
}

function updateNavTexts() {
    const navMap = {
        'nav-dashboard': 'nav.dashboard',
        'nav-user': 'nav.my_trading',
        'nav-positions': 'nav.positions',
        'nav-history': 'nav.history',
        'nav-analytics': 'nav.analytics',
        'nav-charts': 'nav.charts',
        'nav-social': 'nav.social',
        'nav-backtest': 'nav.backtest',
        'nav-strategies': 'nav.strategies',
        'nav-strategy-editor': 'nav.strategy_editor',
        'nav-subscription': 'nav.subscription',
        'nav-settings': 'nav.settings',
        'nav-admin': 'nav.admin',
    };
    Object.entries(navMap).forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (el) {
            const span = el.querySelector('span');
            if (span) {
                const fallback = span.getAttribute('data-i18n-fallback') || span.textContent;
                span.textContent = t(key, fallback);
            }
        }
    });
}

async function initI18n() {
    const langSelect = document.getElementById('lang-select');
    if (langSelect) {
        langSelect.value = _currentLang;
        document.getElementById('language-selector').style.display = '';
    }
    await loadTranslations(_currentLang);
    applyTranslations();
    updateNavTexts();
}

// ─── Auth Helper ───
// Token lives in httpOnly cookie managed by the server.
// We keep a lightweight in-memory user profile fetched via /api/auth/me.
// IMPORTANT: Sync with QP.Auth namespace to avoid state inconsistencies
let _cachedUser = null;
let _sessionRedirecting = false;

async function ensureUser() {
    // Sync with QP.Auth._cachedUser if available (qp-core.js loaded)
    if (window.QP && QP.Auth && QP.Auth._cachedUser) {
        _cachedUser = QP.Auth._cachedUser; // Sync local cache
        return _cachedUser;
    }
    // Fallback: fetch from API
    if (_cachedUser) return _cachedUser;
    try {
        const r = await fetch('/api/auth/me', { credentials: 'include', cache: 'no-store' });
        if (!r.ok) return null;
        _cachedUser = await r.json();
        // Sync back to QP.Auth if available
        if (window.QP && QP.Auth) {
            QP.Auth._cachedUser = _cachedUser;
        }
        return _cachedUser;
    } catch { return null; }
}

function getUser() {
    // Sync with QP.Auth if available
    if (window.QP && QP.Auth) {
        return QP.Auth.getUser();
    }
    return _cachedUser || {};
}

function isAdmin() {
    // Sync with QP.Auth if available
    if (window.QP && QP.Auth) {
        return QP.Auth.isAdmin();
    }
    return getUser().role === 'admin';
}

async function requireAuth() {
    const user = await ensureUser();
    if (!user) {
        redirectToLogin('expired');
        return false;
    }
    return true;
}

function redirectToLogin(reason = 'expired') {
    if (_sessionRedirecting) return;
    _sessionRedirecting = true;
    _cachedUser = null;
    // Sync with QP.Auth if available
    if (window.QP && QP.Auth) {
        QP.Auth._cachedUser = null;
        QP.Auth._sessionRedirecting = true;
    }
    const query = reason ? `?${encodeURIComponent(reason)}=1` : '';
    window.location.replace(`/login${query}`);
}

async function logout() {
    try {
        const csrf = getCookie('tvss_csrf');
        await fetch('/api/auth/logout', {
            method: 'POST',
            credentials: 'include',
            headers: csrf ? { 'X-CSRF-Token': decodeURIComponent(csrf) } : {},
        });
    } catch {}
    redirectToLogin('logout');
}

function getCookie(name) {
    const prefix = `${name}=`;
    return document.cookie.split(';').map(v => v.trim()).find(v => v.startsWith(prefix))?.slice(prefix.length) || '';
}

// ─── Initialization ───
document.addEventListener('DOMContentLoaded', async () => {
    if (!await requireAuth()) return;
    await initI18n();
    setupNavigation();
    setupExchangeToggle();
    detectWebhookUrl();
    updateUserUI();
    setupRealtimeStatus();
    setupSpotlight();
    _launchContext = parseLaunchContext();
    const hashPage = _launchContext.page || (window.location.hash ? window.location.hash.slice(1) : '');
    const initialPage = document.getElementById(`page-${hashPage}`)
        ? hashPage
        : (isAdmin() ? 'dashboard' : 'user');
    switchPage(initialPage);
});

function parseLaunchContext() {
    const params = new URLSearchParams(window.location.search || '');
    const hashPage = window.location.hash ? window.location.hash.slice(1) : '';
    return {
        page: hashPage,
        title: params.get('title') || '',
        text: params.get('text') || '',
        url: params.get('url') || '',
        data: params.get('data') || '',
    };
}

function clearLaunchQuery() {
    if (!window.location.search) return;
    const nextHash = window.location.hash || '';
    history.replaceState(null, '', `${window.location.pathname}${nextHash}`);
}

function getPendingProtocolSignal() {
    try {
        return sessionStorage.getItem('qp_protocol_signal') || '';
    } catch {
        return '';
    }
}

function sanitizeTickerSymbol(value) {
    return String(value || '').toUpperCase().replace(/[^A-Z0-9./:_-]/g, '').trim();
}

function applySocialLaunchContext() {
    if (!_launchContext) return;
    const sharedTitle = (_launchContext.title || '').trim();
    const sharedText = (_launchContext.text || '').trim();
    const sharedUrl = (_launchContext.url || '').trim();
    if (!sharedTitle && !sharedText && !sharedUrl) return;

    const combinedText = [sharedTitle, sharedText].filter(Boolean).join(' ').trim();
    const tickerMatch = combinedText.toUpperCase().match(/\b([A-Z0-9]{2,20}(?:USDT|USDC|BUSD|USD|BTC|ETH|BNB)(?:\.P)?)\b/);
    const entryMatch = combinedText.match(/(?:entry|price)\s*[:=@-]?\s*(\d+(?:\.\d+)?)/i);
    const directionMatch = combinedText.match(/\b(long|short)\b/i);

    const ticker = sanitizeTickerSymbol(tickerMatch ? tickerMatch[1] : '');
    if (ticker) setFieldValue('social-ticker', ticker);
    if (directionMatch) setFieldValue('social-direction', String(directionMatch[1]).toLowerCase());
    if (entryMatch) setFieldValue('social-entry', entryMatch[1]);

    const reasonParts = [];
    if (sharedTitle) reasonParts.push(sharedTitle);
    if (sharedText) reasonParts.push(sharedText);
    if (sharedUrl) reasonParts.push(sharedUrl);
    if (reasonParts.length) {
        setFieldValue('social-reason', reasonParts.join('\n').trim());
    }

    showToast('Shared content imported into the signal form.', 'info', 'Share Target');
    _launchContext.title = '';
    _launchContext.text = '';
    _launchContext.url = '';
    clearLaunchQuery();
}

function applyProtocolLaunchContext() {
    if (!_launchContext?.data) return;
    const payload = String(_launchContext.data || '').trim();
    if (!payload) return;
    try {
        sessionStorage.setItem('qp_protocol_signal', payload);
    } catch {}
    showToast('Protocol signal payload imported into the dashboard.', 'info', 'Protocol Launch');
    _launchContext.data = '';
    clearLaunchQuery();
}

function setupSpotlight() {
    document.addEventListener('mousemove', e => {
        document.querySelectorAll('.card, .chart-card, .kpi-card, .plan-card, .option-card').forEach(card => {
            const rect = card.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            card.style.setProperty('--mouse-x', `${x}px`);
            card.style.setProperty('--mouse-y', `${y}px`);
        });
    }, { passive: true });
}

function updateUserUI() {
    const user = getUser();
    const usernameEl = document.getElementById('user-display-name');
    if (usernameEl) usernameEl.textContent = user.username || 'User';
    const roleEl = document.getElementById('user-role-badge');
    if (roleEl) {
        roleEl.textContent = user.role === 'admin' ? 'Admin' : 'User';
        roleEl.className = `role-badge ${user.role === 'admin' ? 'admin' : 'user'}`;
    }
    // Show/hide admin nav items
    document.querySelectorAll('.admin-only').forEach(el => {
        el.style.display = isAdmin() ? '' : 'none';
    });
    document.querySelectorAll('.user-only').forEach(el => {
        el.style.display = isAdmin() ? 'none' : '';
    });
    ['dashboard','positions','history','analytics','settings'].forEach(page => {
        const el = document.querySelector(`.nav-item[data-page="${page}"]`);
        if (el && !isAdmin()) el.style.display = 'none';
    });
}

function setServerStatus(label, state = 'offline') {
    const status = document.getElementById('server-status');
    if (!status) return;
    status.dataset.state = state;
    const labelEl = status.querySelector('span:last-child');
    if (labelEl) labelEl.textContent = label;
}

function wsUrl(path) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}${path}`;
}

function closeSocket(socket) {
    if (!socket) return;
    try {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
    } catch {}
}

function setupRealtimeStatus() {
    setServerStatus(navigator.onLine ? 'Realtime standby' : 'Offline', navigator.onLine ? 'connecting' : 'offline');
    window.addEventListener('online', () => {
        setServerStatus('Realtime standby', 'connecting');
        connectSystemSocket();
    });
    window.addEventListener('offline', () => {
        setServerStatus('Offline', 'offline');
        closeSocket(_systemSocket);
        _systemSocket = null;
    });
    connectSystemSocket();
}

function connectSystemSocket() {
    if (!navigator.onLine || _systemSocket) return;
    setServerStatus('Connecting realtime...', 'connecting');
    const socket = new WebSocket(wsUrl('/ws/positions'));
    _systemSocket = socket;

    socket.onopen = () => {
        setServerStatus('Realtime connected', 'online');
        try {
            socket.send(JSON.stringify({ type: 'subscribe', channels: ['positions'] }));
        } catch {}
    };

    socket.onmessage = (event) => {
        let message = null;
        try {
            message = JSON.parse(event.data);
        } catch {
            return;
        }
        if (message?.type === 'connected' || message?.type === 'subscribed' || message?.type === 'pong') {
            setServerStatus('Realtime connected', 'online');
            return;
        }
        if (['position_update', 'position_closed', 'trade_executed'].includes(message?.type)) {
            setServerStatus('Realtime live', 'online');
            const page = document.querySelector('.page.active')?.id?.replace('page-', '');
            if (page === 'positions') { loadPositions(); loadPendingOrders(); }
            if (page === 'dashboard') loadRecentSignals();
        }
    };

    socket.onerror = () => {
        setServerStatus('Realtime unavailable', 'offline');
    };

    socket.onclose = () => {
        if (_systemSocket === socket) _systemSocket = null;
        if (!navigator.onLine) {
            setServerStatus('Offline', 'offline');
            return;
        }
        setServerStatus('Realtime retrying...', 'connecting');
        setTimeout(() => {
            if (!_systemSocket && navigator.onLine) connectSystemSocket();
        }, 5000);
    };
}

function teardownPriceSocket() {
    closeSocket(_priceSocket);
    _priceSocket = null;
    _priceSocketTicker = '';
}

function connectPriceSocket(ticker) {
    if (!ticker || !navigator.onLine) return;
    if (_priceSocket && _priceSocketTicker === ticker) return;
    teardownPriceSocket();
    _priceSocketTicker = ticker;
    const socket = new WebSocket(wsUrl('/ws/prices'));
    _priceSocket = socket;

    socket.onopen = () => {
        try {
            socket.send(JSON.stringify({ type: 'subscribe_tickers', tickers: [ticker] }));
        } catch {}
    };

    socket.onmessage = (event) => {
        let message = null;
        try {
            message = JSON.parse(event.data);
        } catch {
            return;
        }
        if (message?.type !== 'price_update' || String(message.ticker || '').toUpperCase() !== _priceSocketTicker) return;

        _chartRealtimeState = message;
        setText('chart-price', message.price ? `$${formatNum(message.price)}` : '--');
        const changeValue = Number(message.change_1h_pct || 0);
        const changeEl = document.getElementById('chart-change');
        if (changeEl) {
            changeEl.textContent = `${changeValue >= 0 ? '+' : ''}${changeValue.toFixed(2)}%`;
            changeEl.className = `kpi-value ${changeValue >= 0 ? 'pnl-positive' : 'pnl-negative'}`;
        }
        setText('chart-rsi', formatValue(message.rsi_1h));
        setText('chart-volume', formatCompact(message.volume_24h));
    };

    socket.onclose = () => {
        if (_priceSocket === socket) _priceSocket = null;
        const activePage = document.querySelector('.page.active')?.id?.replace('page-', '');
        if (activePage === 'charts' && navigator.onLine && _priceSocketTicker === ticker) {
            setTimeout(() => {
                if (!_priceSocket && _priceSocketTicker === ticker) connectPriceSocket(ticker);
            }, 5000);
        }
    };
}

// ─── Toast ───
function escapeHtml(str) {
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function safeClassToken(str) {
    return String(str || '').toLowerCase().replace(/[^a-z0-9_-]/g, '');
}
function escapeJsSingle(str) {
    return String(str || '')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\r?\n/g, ' ');
}
function getCookie(name) {
    const prefix = `${name}=`;
    return document.cookie.split(';').map(v => v.trim()).find(v => v.startsWith(prefix))?.slice(prefix.length) || '';
}
function copyText(text, label = 'Copied') {
    navigator.clipboard.writeText(text).then(() => showToast(label, 'success'));
}
function showToast(message, type = 'info', title = '') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const icons = { success:'ri-checkbox-circle-line', error:'ri-error-warning-line', warning:'ri-alert-line', info:'ri-information-line' };
    const defaultTitles = { success:'Success', error:'Error', warning:'Warning', info:'Info' };
    const safeTitle = escapeHtml(title || defaultTitles[type] || 'Notice');
    const safeMessage = message ? escapeHtml(message) : '';
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.setAttribute('role','alert');
    toast.innerHTML = `<i class="toast-icon ${icons[type]||icons.info}"></i><div class="toast-body"><div class="toast-title">${safeTitle}</div>${safeMessage?`<div class="toast-msg">${safeMessage}</div>`:''}</div>`;
    container.appendChild(toast);
    const dismiss = () => { toast.classList.add('removing'); toast.addEventListener('animationend', () => toast.remove(), {once:true}); };
    setTimeout(dismiss, 4000);
    toast.addEventListener('click', dismiss);
}

// ─── Navigation ───
function setupNavigation() {
    document.querySelectorAll('.nav-item[data-page]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            switchPage(item.dataset.page);
            closeSidebar();
        });
    });
    document.getElementById('menu-toggle')?.addEventListener('click', () => {
        document.getElementById('sidebar')?.classList.toggle('open');
        document.getElementById('sidebar-overlay')?.classList.toggle('visible');
    });
    document.getElementById('sidebar-overlay')?.addEventListener('click', closeSidebar);
}

function closeSidebar() {
    document.getElementById('sidebar')?.classList.remove('open');
    document.getElementById('sidebar-overlay')?.classList.remove('visible');
}

function switchPage(page) {
    // Block non-admin from admin-only pages
    if (!isAdmin() && (page === 'backtest' || page === 'admin' || page === 'settings' || page === 'dashboard' || page === 'positions' || page === 'history' || page === 'analytics')) {
        page = 'user';
    }
    document.querySelectorAll('.nav-item').forEach(n => { n.classList.remove('active'); n.removeAttribute('aria-current'); });
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    const navEl = document.querySelector(`[data-page="${page}"]`);
    navEl?.classList.add('active');
    navEl?.setAttribute('aria-current','page');
    document.getElementById(`page-${page}`)?.classList.add('active');
    if (page !== 'charts') teardownPriceSocket();
    const titles = {
        dashboard: t('nav.dashboard', 'Dashboard'),
        user: t('nav.my_trading', 'My Trading'),
        positions: t('nav.positions', 'Positions'),
        history: t('nav.history', 'Trade History'),
        analytics: t('nav.analytics', 'Analytics'),
        charts: t('nav.charts', 'Charts'),
        social: t('nav.social', 'Signals'),
        settings: t('nav.settings', 'Settings'),
        subscription: t('nav.subscription', 'Subscription'),
        admin: t('nav.admin', 'Admin Panel'),
        backtest: t('nav.backtest', 'Backtest'),
        strategies: t('nav.strategies', 'Strategies'),
        'strategy-editor': t('nav.strategy_editor', 'Editor'),
    };
    document.getElementById('page-title').textContent = titles[page] || page;
    if (window.location.hash !== `#${page}`) {
        history.replaceState(null, '', `${window.location.pathname}#${page}`);
    }
    if (page === 'dashboard') applyProtocolLaunchContext();
    if (page === 'dashboard') loadDashboard();
    if (page === 'positions') { loadPositions(); loadPendingOrders(); }
    if (page === 'history') loadHistory();
    if (page === 'analytics') loadAnalytics();
    if (page === 'charts') loadChartPage();
    if (page === 'social') applySocialLaunchContext();
    if (page === 'social') loadSocialPage();
    if (page === 'settings') loadSettings();
    if (page === 'user') loadUserPortal();
    if (page === 'subscription') loadSubscription();
    if (page === 'admin') loadAdmin();
    if (page === 'strategies') {
        loadStrategiesOverview();
        loadDCAList();
        loadGridList();
        loadStrategyHistory();
    }
    if (page === 'strategy-editor') loadStrategyEditorPage();
    // Re-apply translations after page switch so content pages get translated
    applyTranslations();
}

// ─── Dashboard ───
async function loadDashboard() {
    try {
        const [status, perf, strategyOverview] = await Promise.all([
            fetchAPI('/api/status'),
            fetchAPI('/api/performance?days=30'),
            fetchAPI('/api/strategies/overview').catch(() => null),
        ]);
        if (status.live_trading) {
            const el = document.getElementById('trading-mode');
            el.innerHTML = `<span class="mode-dot live"></span><span>${status.exchange_sandbox_mode ? 'Sandbox Trading' : 'LIVE Trading'}</span>`;
            el.style.background = 'var(--accent-red-bg)';
            el.style.color = 'var(--accent-red)';
        }
        setText('dash-api-health', 'API Online');
        setText('dash-risk-mode', status.live_trading ? 'Live Risk Guard' : 'Paper Risk Guard');
        setText('dash-webhook-state', status.webhook_configured === false ? 'Webhook Needs Setup' : 'Webhook Ready');
        const pnl = perf.total_pnl_pct || 0;
        const pnlEl = document.getElementById('kpi-pnl');
        pnlEl.textContent = `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%`;
        pnlEl.className = `kpi-value ${pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`;
        document.getElementById('kpi-trades').textContent = perf.total_trades || 0;
        document.getElementById('kpi-winrate').textContent = `${(perf.win_rate || 0).toFixed(1)}%`;
        document.getElementById('kpi-sharpe').textContent = (perf.sharpe_ratio || 0).toFixed(2);
        renderDashboardBrief(perf, strategyOverview, status);
        renderMetrics(perf);
        renderEquityChart(perf.equity_curve || []);
        await loadRecentSignals();
        checkSystemHealth();
    } catch (err) {
console.error('Dashboard load error:', err);
    }
}

async function checkSystemHealth() {
    try {
        const health = await fetchAPI('/health/quick');
        const chipEl = document.getElementById('system-health-chip');
        const healthTextEl = document.getElementById('dash-system-health');

        if (health.status === 'healthy') {
            if (chipEl) chipEl.className = 'status-chip';
            if (healthTextEl) healthTextEl.textContent = 'System Healthy';
        } else if (health.status === 'degraded') {
            if (chipEl) chipEl.className = 'status-chip warning';
            if (healthTextEl) healthTextEl.textContent = 'System Degraded';
        } else {
            if (chipEl) chipEl.className = 'status-chip error';
            if (healthTextEl) healthTextEl.textContent = 'System Issues';
        }
    } catch (err) {
        const chipEl = document.getElementById('system-health-chip');
        const healthTextEl = document.getElementById('dash-system-health');
        if (chipEl) chipEl.className = 'status-chip error';
        if (healthTextEl) healthTextEl.textContent = 'Health Check Failed';
    }
}

function renderDashboardBrief(perf = {}, overview = {}, status = {}) {
    const pnl = Number(perf.total_pnl_pct || 0);
    const win = Number(perf.win_rate || 0);
    const confidence = Math.max(0, Math.min(99, Math.round((win * 0.55) + (pnl > 0 ? 25 : 12) + Math.min(Number(perf.sharpe_ratio || 0) * 8, 16))));
    setText('ai-confidence', confidence ? `${confidence}%` : '--');
    setText('ai-brief-title', pnl >= 0 ? 'Constructive Market Posture' : 'Defensive Market Posture');
    setText(
        'ai-brief-text',
        pnl >= 0
            ? `Portfolio trend is positive over the selected window. Keep execution gated by risk controls and review any low-confidence signal before automation.`
            : `Performance is under pressure. Prefer paper mode, smaller sizing, and manual review until drawdown and win-rate stabilize.`
    );
    setText('dash-dca-active', overview?.dca?.active_count ?? '--');
    setText('dash-grid-active', overview?.grid?.active_count ?? '--');
    const botPnl = Number((overview?.dca?.total_pnl || 0) + (overview?.grid?.total_pnl || 0));
    setText('dash-bot-pnl', Number.isFinite(botPnl) ? `$${formatNum(botPnl)}` : '--');

    const queue = document.getElementById('dashboard-action-queue');
    if (!queue) return;
    const items = [
        {
            icon: status.live_trading ? 'ri-alarm-warning-line' : 'ri-shield-check-line',
            title: status.live_trading ? 'Live trading enabled' : 'Paper trading active',
            text: status.live_trading ? 'Confirm exchange keys, leverage, and emergency stop readiness.' : 'Execution is isolated from real funds.',
        },
        {
            icon: 'ri-robot-2-line',
            title: `${overview?.dca?.active_count || 0} DCA / ${overview?.grid?.active_count || 0} Grid bots`,
            text: 'Automation health is tracked from persisted strategy state.',
        },
        {
            icon: pnl >= 0 ? 'ri-line-chart-line' : 'ri-arrow-down-circle-line',
            title: pnl >= 0 ? 'Performance stable' : 'Performance needs review',
            text: `Win rate ${win.toFixed(1)}%, Sharpe ${(Number(perf.sharpe_ratio || 0)).toFixed(2)}.`,
        },
    ];
    const protocolSignal = getPendingProtocolSignal();
    if (protocolSignal) {
        items.unshift({
            icon: 'ri-radar-line',
            title: 'Protocol signal imported',
            text: protocolSignal.length > 120 ? `${protocolSignal.slice(0, 117)}...` : protocolSignal,
        });
    }
    queue.innerHTML = items.map(item => `<div class="queue-item"><i class="${item.icon}"></i><div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.text)}</span></div></div>`).join('');
}

function renderMetrics(perf) {
    const grid = document.getElementById('metrics-grid');
    const items = [
        ['Profit Factor', formatValue(perf.profit_factor)], ['Risk/Reward', formatValue(perf.risk_reward_ratio)],
        ['Max Drawdown', `${(perf.max_drawdown_pct||0).toFixed(2)}%`], ['Sortino Ratio', (perf.sortino_ratio||0).toFixed(2)],
        ['Best Trade', `${(perf.best_trade_pct||0).toFixed(2)}%`], ['Worst Trade', `${(perf.worst_trade_pct||0).toFixed(2)}%`],
        ['Consec. Wins', perf.max_consecutive_wins||0], ['Consec. Losses', perf.max_consecutive_losses||0],
    ];
    grid.innerHTML = items.map(([l,v]) => `<div class="metric-item"><span class="metric-label">${l}</span><span class="metric-value">${v}</span></div>`).join('');
}

async function loadRecentSignals() {
    try {
        const trades = await fetchAPI('/api/trades');
        const container = document.getElementById('recent-signals');
        if (!trades.length) { container.innerHTML = '<div class="empty-state" style="padding:40px;text-align:center;color:var(--text-muted)">No signals today</div>'; return; }
        container.innerHTML = trades.slice(-20).reverse().map(t => {
            const dir = t.direction || 'long', isLong = dir.includes('long');
            const conf = t.ai?.confidence || 0, time = t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : '--';
            return `<div class="signal-item"><div class="signal-icon ${isLong?'long':'short'}"><i class="ri-arrow-${isLong?'up':'down'}-line"></i></div><div class="signal-info"><div class="signal-ticker">${escapeHtml(t.ticker||'--')}</div><div class="signal-detail">${escapeHtml(time)} · ${escapeHtml(dir.toUpperCase())}</div></div><div class="signal-conf ${conf>=0.7?'pnl-positive':conf<0.5?'pnl-negative':''}">${(conf*100).toFixed(0)}%</div></div>`;
        }).join('');
    } catch (e) { console.error('Failed to load signals:', e); }
}

// ─── Market Charts ───
async function loadChartPage() {
    const ticker = (document.getElementById('chart-ticker')?.value || 'BTCUSDT').trim().toUpperCase();
    const timeframe = document.getElementById('chart-timeframe')?.value || '1h';
    const days = parseInt(document.getElementById('chart-days')?.value) || 30;
    try {
        const [ohlcv, realtime, indicators, positions, signals] = await Promise.all([
            fetchAPI(`/api/chart/ohlcv/${encodeURIComponent(ticker)}?timeframe=${encodeURIComponent(timeframe)}&days=${days}`),
            fetchAPI(`/api/chart/realtime/${encodeURIComponent(ticker)}`).catch(() => null),
            fetchAPI(`/api/chart/indicators/${encodeURIComponent(ticker)}?timeframe=${encodeURIComponent(timeframe)}`).catch(() => null),
            fetchAPI(`/api/chart/positions/${encodeURIComponent(ticker)}`).catch(() => ({ markers: [] })),
            fetchAPI(`/api/chart/signals/${encodeURIComponent(ticker)}?days=${days}`).catch(() => ({ markers: [] })),
        ]);
        renderMarketChart(ohlcv.data || []);
        const lastBar = ohlcv.data?.length ? ohlcv.data[ohlcv.data.length - 1] : null;
        const live = _chartRealtimeState && _chartRealtimeState.ticker === ticker ? _chartRealtimeState : realtime;
        const price = live?.price ?? lastBar?.close;
        setText('chart-price', price ? `$${formatNum(price)}` : '--');
        const change = Number(live?.change_24h_pct ?? live?.change_1h_pct ?? 0);
        const changeEl = document.getElementById('chart-change');
        if (changeEl) {
            changeEl.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
            changeEl.className = `kpi-value ${change >= 0 ? 'pnl-positive' : 'pnl-negative'}`;
        }
        setText('chart-rsi', formatValue(live?.rsi_1h ?? indicators?.indicators?.rsi_1h));
        setText('chart-volume', formatCompact(live?.volume_24h ?? indicators?.indicators?.volume_24h ?? realtime?.volume_24h));
        renderMarkerList('chart-positions', positions.markers || [], t('pages.charts.no_positions', 'No open position markers'));
        renderMarkerList('chart-signals', signals.markers || [], t('pages.charts.no_signals', 'No executed signal markers'));
        connectPriceSocket(ticker);
    } catch (err) {
        showToast(err.message, 'error', 'Chart Load Failed');
    }
}

function renderMarketChart(data) {
    const ctx = document.getElementById('market-chart')?.getContext('2d');
    if (!ctx) return;
    if (marketChart) marketChart.destroy();
    const labels = data.map(bar => new Date((bar.time || 0) * 1000).toLocaleString());
    const close = data.map(bar => Number(bar.close || 0));
    const high = data.map(bar => Number(bar.high || 0));
    const low = data.map(bar => Number(bar.low || 0));
    marketChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [
                { label: 'Close', data: close, borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,.12)', borderWidth: 2, pointRadius: 0, tension: .25, fill: true },
                { label: 'High', data: high, borderColor: 'rgba(59,130,246,.55)', borderWidth: 1, pointRadius: 0, tension: .2 },
                { label: 'Low', data: low, borderColor: 'rgba(239,68,68,.55)', borderWidth: 1, pointRadius: 0, tension: .2 },
            ],
        },
        options: chartOptions('Price'),
    });
}

function renderMarkerList(id, markers, emptyText) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!markers.length) {
        el.innerHTML = `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
        return;
    }
    el.innerHTML = `<div class="table-wrapper"><table class="data-table"><thead><tr><th>${escapeHtml(t('common.time', 'Time'))}</th><th>${escapeHtml(t('pages.charts.marker', 'Marker'))}</th></tr></thead><tbody>${markers.map(m => `<tr><td>${escapeHtml(formatDateTime(new Date((m.time || 0) * 1000).toISOString()))}</td><td>${escapeHtml(m.text || m.position || '--')}</td></tr>`).join('')}</tbody></table></div>`;
}

function formatCompact(value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n) || n === 0) return '--';
    if (Math.abs(n) >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
    if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
    if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(2)}K`;
    return n.toFixed(2);
}

// ─── Social Signals ───
async function loadSocialPage() {
    try {
        const [stats, feed, subs, leaderboard] = await Promise.all([
            fetchAPI('/api/social/stats'),
            fetchAPI('/api/social/list?limit=50'),
            fetchAPI('/api/social/subscriptions'),
            fetchAPI('/api/social/leaderboard?limit=10'),
        ]);
        setText('social-total-signals', stats.total_signals || 0);
        setText('social-total-subs', stats.total_subscriptions || 0);
        setText('social-active-users', stats.active_users || 0);
        setText('social-top-ticker', stats.top_tickers?.[0]?.ticker || '--');
        renderSocialFeed(feed.signals || []);
        renderSocialSubscriptions(subs.subscriptions || []);
        renderSocialLeaderboard(leaderboard.leaderboard || []);
    } catch (err) {
        showToast(err.message, 'error', 'Signals Load Failed');
    }
}

async function shareSocialSignal() {
    const payload = {
        ticker: (document.getElementById('social-ticker')?.value || 'BTCUSDT').trim().toUpperCase(),
        direction: document.getElementById('social-direction')?.value || 'long',
        entry_price: Number(document.getElementById('social-entry')?.value || 0),
        stop_loss: optionalNumber('social-sl'),
        take_profit: optionalNumber('social-tp'),
        confidence: Number(document.getElementById('social-confidence')?.value || 0),
        reason: document.getElementById('social-reason')?.value || '',
        strategy_name: 'manual-share',
    };
    if (!payload.entry_price || payload.entry_price <= 0) {
        showToast('Entry price must be greater than zero.', 'warning', 'Invalid Signal');
        return;
    }
    try {
        await fetchAPI('/api/social/share', { method: 'POST', body: JSON.stringify(payload) });
        showToast('Signal shared.', 'success', 'Shared');
        await loadSocialPage();
    } catch (err) {
        showToast(err.message, 'error', 'Share Failed');
    }
}

function optionalNumber(id) {
    const value = Number(document.getElementById(id)?.value || 0);
    return value > 0 ? value : null;
}

function renderSocialFeed(signals) {
    const el = document.getElementById('social-feed');
    if (!el) return;
    if (!signals.length) {
        el.innerHTML = `<div class="empty-state">${escapeHtml(t('pages.social.no_signals', 'No shared signals yet'))}</div>`;
        return;
    }
    el.innerHTML = `<div class="table-wrapper"><table class="data-table"><thead><tr><th>${escapeHtml(t('common.ticker', 'Ticker'))}</th><th>${escapeHtml(t('common.direction', 'Direction'))}</th><th>${escapeHtml(t('trading.entry', 'Entry'))}</th><th>${escapeHtml(t('common.confidence', 'Confidence'))}</th><th>${escapeHtml(t('pages.social.provider', 'Provider'))}</th><th>${escapeHtml(t('common.actions', 'Actions'))}</th></tr></thead><tbody>${signals.map(s => `<tr><td><strong>${escapeHtml(s.ticker)}</strong></td><td><span class="badge badge-${safeClassToken(s.direction)}">${escapeHtml(t(`trading.${s.direction}`, s.direction))}</span></td><td>$${formatNum(s.entry_price)}</td><td>${Math.round(Number(s.confidence || 0) * 100)}%</td><td>${escapeHtml(s.username || '--')}</td><td><div class="admin-actions"><button class="btn btn-sm btn-primary" onclick="subscribeSocialSignal('${escapeJsSingle(s.signal_id)}')">${escapeHtml(t('actions.subscribe', 'Subscribe'))}</button><button class="btn btn-sm btn-secondary" onclick="followSignalUser('${escapeJsSingle(s.username || '')}')">${escapeHtml(t('actions.follow', 'Follow'))}</button></div></td></tr>`).join('')}</tbody></table></div>`;
}

function renderSocialSubscriptions(subs) {
    const el = document.getElementById('social-subscriptions');
    if (!el) return;
    if (!subs.length) {
        el.innerHTML = `<div class="empty-state">${escapeHtml(t('pages.social.no_subscriptions', 'No signal subscriptions'))}</div>`;
        return;
    }
    el.innerHTML = `<div class="table-wrapper"><table class="data-table"><thead><tr><th>${escapeHtml(t('pages.social.signal', 'Signal'))}</th><th>${escapeHtml(t('pages.social.auto_execute', 'Auto Execute'))}</th><th>${escapeHtml(t('pages.social.max_position', 'Max Position'))}</th><th>${escapeHtml(t('pages.social.subscribed', 'Subscribed'))}</th><th>${escapeHtml(t('common.actions', 'Actions'))}</th></tr></thead><tbody>${subs.map(s => `<tr><td><code>${escapeHtml(s.signal_id)}</code></td><td>${s.auto_execute ? t('common.yes', 'Yes') : t('common.no', 'No')}</td><td>${formatNum(s.max_position_pct)}%</td><td>${escapeHtml(formatDateTime(s.subscribed_at))}</td><td><button class="btn btn-sm btn-danger" onclick="unsubscribeSocialSignal('${escapeJsSingle(s.signal_id)}')">${escapeHtml(t('actions.unsubscribe', 'Unsubscribe'))}</button></td></tr>`).join('')}</tbody></table></div>`;
}

function renderSocialLeaderboard(rows) {
    const el = document.getElementById('social-leaderboard');
    if (!el) return;
    if (!rows.length) {
        el.innerHTML = `<div class="empty-state">${escapeHtml(t('pages.social.no_leaderboard', 'No leaderboard data'))}</div>`;
        return;
    }
    el.innerHTML = rows.map((r, idx) => `<div class="metric-item"><span class="metric-label">#${idx + 1} ${escapeHtml(r.username || '--')} · ${escapeHtml(r.ticker || '--')}</span><span class="metric-value">${formatNum(r.success_rate || 0)}%</span></div>`).join('');
}

async function subscribeSocialSignal(signalId) {
    try {
        await fetchAPI(`/api/social/subscribe/${encodeURIComponent(signalId)}`, {
            method: 'POST',
            body: JSON.stringify({ signal_id: signalId, auto_execute: false, max_position_pct: 10 }),
        });
        showToast('Signal subscribed.', 'success', 'Subscribed');
        await loadSocialPage();
    } catch (err) {
        showToast(err.message, 'error', 'Subscribe Failed');
    }
}

async function unsubscribeSocialSignal(signalId) {
    try {
        await fetchAPI(`/api/social/unsubscribe/${encodeURIComponent(signalId)}`, { method: 'DELETE' });
        showToast('Signal subscription removed.', 'warning', 'Unsubscribed');
        await loadSocialPage();
    } catch (err) {
        showToast(err.message, 'error', 'Unsubscribe Failed');
    }
}

async function followSignalUser(username) {
    if (!username) return;
    try {
        await fetchAPI(`/api/social/follow/${encodeURIComponent(username)}`, { method: 'POST' });
        showToast(`Following ${username}.`, 'success', 'Following');
    } catch (err) {
        showToast(err.message, 'error', 'Follow Failed');
    }
}

// ─── Strategy Editor ───
async function loadStrategyEditorPage() {
    try {
        const [templates, strategies] = await Promise.all([
            fetchAPI('/api/strategy-editor/templates'),
            fetchAPI('/api/strategy-editor/list'),
        ]);
        renderStrategyTemplates(templates.templates || []);
        renderEditorStrategies(strategies.strategies || []);
    } catch (err) {
        showToast(err.message, 'error', 'Editor Load Failed');
    }
}

function renderStrategyTemplates(templates) {
    const el = document.getElementById('strategy-template-list');
    if (!el) return;
    _strategyTemplates = templates;
    if (!templates.length) {
        el.innerHTML = `<div class="empty-state">${escapeHtml(t('pages.editor.no_templates', 'No templates available'))}</div>`;
        return;
    }
    el.innerHTML = templates.map(item => `<div class="template-card"><div><strong>${escapeHtml(item.name)}</strong><span class="hint">${escapeHtml(item.category)} · ${escapeHtml(item.description)}</span></div><button class="btn btn-sm btn-secondary" onclick="useStrategyTemplateById('${escapeJsSingle(item.id || item.name)}')">${escapeHtml(t('actions.use', 'Use'))}</button></div>`).join('');
}

function useStrategyTemplateById(templateId) {
    const template = _strategyTemplates.find(t => (t.id || t.name) === templateId);
    if (template) useStrategyTemplate(template);
}

function useStrategyTemplate(template) {
    const config = template.config || {};
    setFieldValue('editor-strategy-id', '');
    setFieldValue('editor-name', template.name || 'Custom Strategy');
    setFieldValue('editor-entry-json', JSON.stringify(config.entry_conditions || [], null, 2));
    setFieldValue('editor-exit-json', JSON.stringify(config.exit_conditions || [], null, 2));
    setFieldValue('editor-risk-json', JSON.stringify(config.risk_management || {}, null, 2));
    setFieldValue('editor-tp-json', JSON.stringify(config.tp_levels || [], null, 2));
    setFieldValue('editor-trailing-json', JSON.stringify(config.trailing_stop || {}, null, 2));
}

function resetStrategyEditorForm() {
    setFieldValue('editor-strategy-id', '');
    setFieldValue('editor-name', 'My Strategy');
    setFieldValue('editor-ticker', 'BTCUSDT');
    setFieldValue('editor-direction', 'long');
    setFieldValue('editor-entry-json', '[]');
    setFieldValue('editor-exit-json', '[]');
    setFieldValue('editor-risk-json', '{}');
    setFieldValue('editor-tp-json', '[]');
    setFieldValue('editor-trailing-json', '{}');
}

async function saveEditorStrategy() {
    let payload;
    try {
        payload = {
            strategy_id: document.getElementById('editor-strategy-id')?.value || '',
            name: document.getElementById('editor-name')?.value || 'My Strategy',
            ticker: (document.getElementById('editor-ticker')?.value || 'BTCUSDT').trim().toUpperCase(),
            direction: document.getElementById('editor-direction')?.value || 'long',
            entry_conditions: JSON.parse(document.getElementById('editor-entry-json')?.value || '[]'),
            exit_conditions: JSON.parse(document.getElementById('editor-exit-json')?.value || '[]'),
            risk_management: JSON.parse(document.getElementById('editor-risk-json')?.value || '{}'),
            tp_levels: JSON.parse(document.getElementById('editor-tp-json')?.value || '[]'),
            trailing_stop: JSON.parse(document.getElementById('editor-trailing-json')?.value || '{}'),
        };
    } catch (err) {
        showToast('One of the JSON fields is invalid.', 'warning', 'Invalid JSON');
        return;
    }
    try {
        const existingId = payload.strategy_id;
        const endpoint = existingId ? `/api/strategy-editor/${encodeURIComponent(existingId)}` : '/api/strategy-editor/create';
        const method = existingId ? 'PUT' : 'POST';
        const result = await fetchAPI(endpoint, { method, body: JSON.stringify(payload) });
        setFieldValue('editor-strategy-id', result.strategy_id || existingId);
        showToast('Strategy saved.', 'success', 'Saved');
        await loadStrategyEditorPage();
    } catch (err) {
        showToast(err.message, 'error', 'Save Failed');
    }
}

function renderEditorStrategies(strategies) {
    const el = document.getElementById('editor-strategy-list');
    if (!el) return;
    if (!strategies.length) {
        el.innerHTML = `<div class="empty-state">${escapeHtml(t('pages.editor.no_saved_strategies', 'No saved custom strategies'))}</div>`;
        return;
    }
    el.innerHTML = `<div class="table-wrapper"><table class="data-table"><thead><tr><th>${escapeHtml(t('common.name', 'Name'))}</th><th>${escapeHtml(t('common.ticker', 'Ticker'))}</th><th>${escapeHtml(t('common.direction', 'Direction'))}</th><th>${escapeHtml(t('common.status', 'Status'))}</th><th>${escapeHtml(t('messages.updated', 'Updated'))}</th><th>${escapeHtml(t('common.actions', 'Actions'))}</th></tr></thead><tbody>${strategies.map(s => `<tr><td><strong>${escapeHtml(s.name || '--')}</strong></td><td>${escapeHtml(s.ticker || '--')}</td><td><span class="badge badge-${safeClassToken(s.direction || 'long')}">${escapeHtml(t(`trading.${s.direction || 'long'}`, s.direction || '--'))}</span></td><td><span class="badge badge-${s.is_active ? 'active' : 'pending'}">${s.is_active ? t('common.active', 'active') : t('common.draft', 'draft')}</span></td><td>${escapeHtml(formatDateTime(s.updated_at))}</td><td><div class="admin-actions"><button class="btn btn-sm btn-secondary" onclick="editStrategyDraft('${escapeJsSingle(s.strategy_id)}')">${escapeHtml(t('actions.edit', 'Edit'))}</button><button class="btn btn-sm btn-warning" onclick="exportStrategy('${escapeJsSingle(s.strategy_id)}')"><i class="ri-download-line"></i> Export</button><button class="btn btn-sm btn-primary" onclick="toggleEditorStrategy('${escapeJsSingle(s.strategy_id)}', ${s.is_active ? 'false' : 'true'})">${escapeHtml(s.is_active ? t('actions.deactivate', 'Deactivate') : t('actions.activate', 'Activate'))}</button><button class="btn btn-sm btn-danger" onclick="deleteEditorStrategy('${escapeJsSingle(s.strategy_id)}')">${escapeHtml(t('actions.delete', 'Delete'))}</button></div></td></tr>`).join('')}</tbody></table></div><div style="padding:12px;text-align:right"><button class="btn btn-secondary" onclick="exportAllStrategies()"><i class="ri-download-line"></i> Export All</button></div>`;
}

async function editStrategyDraft(strategyId) {
    try {
        const s = await fetchAPI(`/api/strategy-editor/${encodeURIComponent(strategyId)}`);
        setFieldValue('editor-strategy-id', s.strategy_id || strategyId);
        setFieldValue('editor-name', s.name || '');
        setFieldValue('editor-ticker', s.ticker || 'BTCUSDT');
        setFieldValue('editor-direction', s.direction || 'long');
        setFieldValue('editor-entry-json', JSON.stringify(s.entry_conditions || [], null, 2));
        setFieldValue('editor-exit-json', JSON.stringify(s.exit_conditions || [], null, 2));
        setFieldValue('editor-risk-json', JSON.stringify(s.risk_management || {}, null, 2));
        setFieldValue('editor-tp-json', JSON.stringify(s.tp_levels || [], null, 2));
        setFieldValue('editor-trailing-json', JSON.stringify(s.trailing_stop || {}, null, 2));
    } catch (err) {
        showToast(err.message, 'error', 'Load Failed');
    }
}

async function toggleEditorStrategy(strategyId, activate) {
    try {
        await fetchAPI(`/api/strategy-editor/${encodeURIComponent(strategyId)}/${activate ? 'activate' : 'deactivate'}`, { method: 'POST' });
        showToast(activate ? 'Strategy activated.' : 'Strategy deactivated.', 'success', 'Updated');
        await loadStrategyEditorPage();
    } catch (err) {
        showToast(err.message, 'error', 'Update Failed');
    }
}

async function deleteEditorStrategy(strategyId) {
    if (!confirm('Delete this strategy?')) return;
    try {
        await fetchAPI(`/api/strategy-editor/${encodeURIComponent(strategyId)}`, { method: 'DELETE' });
        showToast('Strategy deleted.', 'success', 'Deleted');
        await loadStrategyEditorPage();
    } catch (err) {
        showToast(err.message, 'error', 'Delete Failed');
    }
}

async function exportStrategy(strategyId) {
    try {
        const s = await fetchAPI(`/api/strategy-editor/export/${encodeURIComponent(strategyId)}`);
        const dataStr = JSON.stringify(s, null, 2);
        const blob = new Blob([dataStr], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `strategy_${s.name || strategyId}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast('Strategy exported.', 'success', 'Export Complete');
    } catch (err) {
        showToast(err.message, 'error', 'Export Failed');
    }
}

async function exportAllStrategies() {
    try {
        const result = await fetchAPI('/api/strategy-editor/list');
        const strategies = result.strategies || [];
        if (!strategies.length) {
            showToast('No strategies to export.', 'warning', 'Empty');
            return;
        }
        const exportData = {
            exported_at: new Date().toISOString(),
            strategies: strategies,
        };
        const dataStr = JSON.stringify(exportData, null, 2);
        const blob = new Blob([dataStr], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `all_strategies_${new Date().toISOString().slice(0, 10)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast(`${strategies.length} strategies exported.`, 'success', 'Export Complete');
    } catch (err) {
        showToast(err.message, 'error', 'Export Failed');
    }
}

function importStrategyFromJSON() {
    const input = document.getElementById('strategy-import-file');
    if (input) input.click();
}

function triggerStrategyImport() {
    const input = document.getElementById('saved-strategy-import-file');
    if (input) input.click();
}

async function handleStrategyImportFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
        const text = await file.text();
        const data = JSON.parse(text);
        setFieldValue('editor-strategy-id', data.strategy_id || '');
        setFieldValue('editor-name', data.name || file.name.replace('.json', ''));
        setFieldValue('editor-ticker', data.ticker || 'BTCUSDT');
        setFieldValue('editor-direction', data.direction || 'long');
        setFieldValue('editor-entry-json', JSON.stringify(data.entry_conditions || [], null, 2));
        setFieldValue('editor-exit-json', JSON.stringify(data.exit_conditions || [], null, 2));
        setFieldValue('editor-risk-json', JSON.stringify(data.risk_management || {}, null, 2));
        setFieldValue('editor-tp-json', JSON.stringify(data.tp_levels || [], null, 2));
        setFieldValue('editor-trailing-json', JSON.stringify(data.trailing_stop || {}, null, 2));
        showToast('Strategy imported to form.', 'success', 'Import Complete');
    } catch (err) {
        showToast('Invalid JSON file.', 'error', 'Import Failed');
    }
    event.target.value = '';
}

async function handleSavedStrategyImportFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
        const text = await file.text();
        const data = JSON.parse(text);
        const strategies = data.strategies || [data];
        let imported = 0;
        for (const s of strategies) {
            try {
                await fetchAPI('/api/strategy-editor/import', {
                    method: 'POST',
                    body: JSON.stringify(s),
                });
                imported++;
            } catch (err) {
                console.warn('Failed to import strategy:', s.name, err);
            }
        }
        showToast(`${imported} strategies imported.`, 'success', 'Import Complete');
        await loadStrategyEditorPage();
    } catch (err) {
        showToast('Invalid JSON file.', 'error', 'Import Failed');
    }
    event.target.value = '';
}
