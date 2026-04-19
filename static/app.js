/**
 * TradingView Signal Server - Dashboard Frontend Logic
 * v4.0 — Auth, Subscriptions, Crypto Payments, Admin Panel
 */

const API = '';
const USDT_PAYMENT_NETWORKS = [
    { id: 'TRC20', name: 'Tron (TRC20)' },
    { id: 'ERC20', name: 'Ethereum (ERC20)' },
    { id: 'BEP20', name: 'BSC (BEP20)' },
    { id: 'SOL', name: 'Solana (SPL)' },
];
let equityChart = null;
let dailyPnlChart = null;
let winlossChart = null;

// ─── Auth Helper ───
function getToken() { return localStorage.getItem('tvss_token'); }
function getUser() {
    try { return JSON.parse(localStorage.getItem('tvss_user') || '{}'); }
    catch { return {}; }
}
function isAdmin() { return getUser().role === 'admin'; }

function requireAuth() {
    if (!getToken()) {
        window.location.href = '/login';
        return false;
    }
    return true;
}

async function logout() {
    try { await fetch('/api/auth/logout', {method:'POST'}); } catch {}
    localStorage.removeItem('tvss_token');
    localStorage.removeItem('tvss_user');
    window.location.href = '/login';
}

// ─── Initialization ───
document.addEventListener('DOMContentLoaded', () => {
    if (!requireAuth()) return;
    setupNavigation();
    setupExchangeToggle();
    detectWebhookUrl();
    updateUserUI();
    if (isAdmin()) loadDashboard();
    else switchPage('subscription');
});

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
}

// ─── Toast ───
function escapeHtml(str) {
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function safeClassToken(str) {
    return String(str || '').toLowerCase().replace(/[^a-z0-9_-]/g, '');
}
function escapeJsSingle(str) {
    return String(str || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\r?\n/g, ' ');
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
    document.querySelectorAll('.nav-item').forEach(n => { n.classList.remove('active'); n.removeAttribute('aria-current'); });
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    const navEl = document.querySelector(`[data-page="${page}"]`);
    navEl?.classList.add('active');
    navEl?.setAttribute('aria-current','page');
    document.getElementById(`page-${page}`)?.classList.add('active');
    const titles = { dashboard:'Dashboard', positions:'Positions', history:'Trade History', analytics:'Analytics', settings:'Settings', subscription:'Subscription', admin:'Admin Panel' };
    document.getElementById('page-title').textContent = titles[page] || page;
    if (page === 'positions') loadPositions();
    if (page === 'history') loadHistory();
    if (page === 'analytics') loadAnalytics();
    if (page === 'settings') loadSettings();
    if (page === 'subscription') loadSubscription();
    if (page === 'admin') loadAdmin();
}

// ─── Dashboard ───
async function loadDashboard() {
    try {
        const [status, stats, perf] = await Promise.all([
            fetchAPI('/api/status'),
            fetchAPI('/stats'),
            fetchAPI('/api/performance?days=30')
        ]);
        if (status.live_trading) {
            const el = document.getElementById('trading-mode');
            el.innerHTML = '<span class="mode-dot live"></span><span>LIVE Trading</span>';
            el.style.background = 'var(--accent-red-bg)';
            el.style.color = 'var(--accent-red)';
        }
        const pnl = perf.total_pnl_pct || 0;
        const pnlEl = document.getElementById('kpi-pnl');
        pnlEl.textContent = `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%`;
        pnlEl.className = `kpi-value ${pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`;
        document.getElementById('kpi-trades').textContent = perf.total_trades || 0;
        document.getElementById('kpi-winrate').textContent = `${(perf.win_rate || 0).toFixed(1)}%`;
        document.getElementById('kpi-sharpe').textContent = (perf.sharpe_ratio || 0).toFixed(2);
        renderMetrics(perf);
        renderEquityChart(perf.equity_curve || []);
        await loadRecentSignals();
    } catch (err) {
        console.error('Dashboard load error:', err);
        if (err.message.includes('401')) logout();
        else showToast(err.message, 'error', 'Dashboard Load Failed');
    }
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
        const trades = await fetchAPI('/trades');
        const container = document.getElementById('recent-signals');
        if (!trades.length) { container.innerHTML = '<div class="empty-state" style="padding:40px;text-align:center;color:var(--text-muted)">No signals today</div>'; return; }
        container.innerHTML = trades.slice(-20).reverse().map(t => {
            const dir = t.direction || 'long', isLong = dir.includes('long');
            const conf = t.ai?.confidence || 0, time = t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : '--';
            return `<div class="signal-item"><div class="signal-icon ${isLong?'long':'short'}"><i class="ri-arrow-${isLong?'up':'down'}-line"></i></div><div class="signal-info"><div class="signal-ticker">${escapeHtml(t.ticker||'--')}</div><div class="signal-detail">${escapeHtml(time)} · ${escapeHtml(dir.toUpperCase())}</div></div><div class="signal-conf ${conf>=0.7?'pnl-positive':conf<0.5?'pnl-negative':''}">${(conf*100).toFixed(0)}%</div></div>`;
        }).join('');
    } catch (e) { console.error('Failed to load signals:', e); }
}

// ─── Charts ───
function renderEquityChart(curve) {
    const ctx = document.getElementById('equity-chart')?.getContext('2d');
    if (!ctx) return;
    if (equityChart) equityChart.destroy();
    const gradient = ctx.createLinearGradient(0,0,0,280);
    gradient.addColorStop(0,'rgba(59,130,246,0.3)'); gradient.addColorStop(1,'rgba(59,130,246,0.0)');
    equityChart = new Chart(ctx, { type:'line', data:{ labels:curve.map((_,i)=>`#${i+1}`), datasets:[{ label:'Cumulative P&L %', data:curve.map(c=>c.cumulative_pnl), borderColor:'#3b82f6', backgroundColor:gradient, borderWidth:2, fill:true, tension:0.4, pointRadius:0, pointHoverRadius:5 }]}, options:chartOptions('P&L %') });
}
function renderDailyPnlChart(daily) {
    const ctx = document.getElementById('daily-pnl-chart')?.getContext('2d');
    if (!ctx) return;
    if (dailyPnlChart) dailyPnlChart.destroy();
    dailyPnlChart = new Chart(ctx, { type:'bar', data:{ labels:daily.map(d=>d.date), datasets:[{ label:'Daily P&L %', data:daily.map(d=>d.pnl), backgroundColor:daily.map(d=>d.pnl>=0?'rgba(16,185,129,0.7)':'rgba(239,68,68,0.7)'), borderRadius:4 }]}, options:chartOptions('P&L %') });
}
function renderWinLossChart(perf) {
    const ctx = document.getElementById('winloss-chart')?.getContext('2d');
    if (!ctx) return;
    if (winlossChart) winlossChart.destroy();
    winlossChart = new Chart(ctx, { type:'doughnut', data:{ labels:['Wins','Losses','Breakeven'], datasets:[{ data:[perf.winning_trades||0,perf.losing_trades||0,perf.breakeven_trades||0], backgroundColor:['#10b981','#ef4444','#6b7280'], borderColor:'transparent', borderWidth:0 }]}, options:{ responsive:true, maintainAspectRatio:false, plugins:{ legend:{ position:'bottom', labels:{color:'#9ca3af',padding:16,font:{size:12}} }}, cutout:'65%' } });
}
function chartOptions(yLabel) {
    return { responsive:true, maintainAspectRatio:false, interaction:{intersect:false,mode:'index'}, plugins:{ legend:{display:false}, tooltip:{backgroundColor:'#1a1f2e',borderColor:'#2a3042',borderWidth:1,titleColor:'#e8eaed',bodyColor:'#9ca3af',cornerRadius:8,padding:12}}, scales:{ x:{grid:{color:'rgba(42,48,66,0.5)'},ticks:{color:'#6b7280',font:{size:11},maxTicksLimit:12}}, y:{grid:{color:'rgba(42,48,66,0.5)'},ticks:{color:'#6b7280',font:{size:11}},title:{display:true,text:yLabel,color:'#6b7280'}}} };
}
function setChartPeriod(evt, days) {
    document.querySelectorAll('.card-actions .btn-sm').forEach(b => b.classList.remove('active'));
    evt.target.classList.add('active');
    fetchAPI(`/api/performance?days=${days}`).then(perf => renderEquityChart(perf.equity_curve||[])).catch(e => showToast(e.message,'error'));
}

// ─── Positions ───
async function loadPositions() {
    try {
        const [positions, balance] = await Promise.all([fetchAPI('/api/positions'), fetchAPI('/balance')]);
        const tbody = document.getElementById('positions-body');
        if (!positions.length) { tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No open positions</td></tr>'; }
        else { tbody.innerHTML = positions.map(p => {
            const entry = firstDefined(p.entry_price, p.entryPrice);
            const mark = firstDefined(p.mark_price, p.markPrice);
            const liq = firstDefined(p.liquidation_price, p.liquidationPrice);
            const pnl = Number(firstDefined(p.unrealized_pnl, p.unrealizedPnl, 0));
            const pct = firstDefined(p.percentage, null);
            const pctText = pct == null ? '--' : `${Number(pct) >= 0 ? '+' : ''}${Number(pct).toFixed(2)}%`;
            return `<tr><td><strong>${escapeHtml(p.symbol||'--')}</strong></td><td><span class="badge ${p.side==='long'?'badge-long':'badge-short'}">${escapeHtml(p.side||'--')}</span></td><td>${escapeHtml(p.contracts)}</td><td>$${formatNum(entry)}</td><td>$${formatNum(mark)}</td><td>${liq?'$'+formatNum(liq):'--'}</td><td class="${pnl>=0?'pnl-positive':'pnl-negative'}">$${formatNum(pnl)}</td><td class="${pct == null || Number(pct)>=0?'pnl-positive':'pnl-negative'}">${pctText}</td><td>${escapeHtml(p.leverage||'--')}x</td></tr>`;
        }).join(''); }
        document.getElementById('bal-total').textContent = `$${formatNum(balance.total_quote ?? pickBalance(balance.total, balance.quote))}`;
        document.getElementById('bal-free').textContent = `$${formatNum(balance.free_quote ?? pickBalance(balance.free, balance.quote))}`;
        document.getElementById('bal-used').textContent = `$${formatNum(balance.used_quote ?? pickBalance(balance.used, balance.quote))}`;
    } catch (err) { showToast(err.message, 'error', 'Positions Load Failed'); }
}

// ─── History ───
async function loadHistory() {
    try {
        const days = document.getElementById('history-days')?.value || 30;
        const trades = await fetchAPI(`/api/history?days=${days}`);
        const tbody = document.getElementById('history-body');
        if (!trades.length) { tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No trades found</td></tr>'; return; }
        tbody.innerHTML = trades.reverse().map(t => {
            const dir = t.direction||'--', isLong = dir.includes('long'), conf = t.ai?.confidence||0;
            const status = t.order_status||t.status||'--', pnl = t.pnl_pct||0;
            const time = t.timestamp ? new Date(t.timestamp).toLocaleString() : '--';
            const statusClass = safeClassToken(status);
            return `<tr><td>${escapeHtml(time)}</td><td><strong>${escapeHtml(t.ticker||'--')}</strong></td><td><span class="badge ${isLong?'badge-long':'badge-short'}">${escapeHtml(dir)}</span></td><td>${t.entry_price?'$'+formatNum(t.entry_price):'--'}</td><td>${t.stop_loss?'$'+formatNum(t.stop_loss):'--'}</td><td>${t.take_profit?'$'+formatNum(t.take_profit):'--'}</td><td>${(conf*100).toFixed(0)}%</td><td><span class="badge badge-${statusClass}">${escapeHtml(status)}</span></td><td class="${pnl>=0?'pnl-positive':'pnl-negative'}">${pnl?pnl.toFixed(2)+'%':'--'}</td></tr>`;
        }).join('');
    } catch (err) { showToast(err.message, 'error', 'History Load Failed'); }
}

// ─── Analytics ───
async function loadAnalytics() {
    try {
        const [perf, daily] = await Promise.all([fetchAPI('/api/performance?days=30'), fetchAPI('/api/daily-pnl?days=30')]);
        document.getElementById('an-pf').textContent = formatValue(perf.profit_factor);
        document.getElementById('an-dd').textContent = `${(perf.max_drawdown_pct||0).toFixed(2)}%`;
        document.getElementById('an-rr').textContent = formatValue(perf.risk_reward_ratio);
        document.getElementById('an-sortino').textContent = (perf.sortino_ratio||0).toFixed(2);
        renderDailyPnlChart(daily);
        renderWinLossChart(perf);
        const metrics = [['Total P&L',`${(perf.total_pnl_pct||0).toFixed(2)}%`],['Win Rate',`${(perf.win_rate||0).toFixed(1)}%`],['Total Trades',perf.total_trades||0],['Avg Win',`${(perf.avg_win_pct||0).toFixed(2)}%`],['Avg Loss',`${(perf.avg_loss_pct||0).toFixed(2)}%`],['Sharpe',(perf.sharpe_ratio||0).toFixed(2)],['Sortino',(perf.sortino_ratio||0).toFixed(2)],['Max DD',`${(perf.max_drawdown_pct||0).toFixed(2)}%`],['Profit Factor',formatValue(perf.profit_factor)],['Best Trade',`${(perf.best_trade_pct||0).toFixed(2)}%`],['Worst Trade',`${(perf.worst_trade_pct||0).toFixed(2)}%`],['Consec. Wins',perf.max_consecutive_wins||0]];
        document.getElementById('detailed-metrics').innerHTML = metrics.map(([l,v]) => `<div class="metric-item"><span class="metric-label">${l}</span><span class="metric-value">${v}</span></div>`).join('');
        const ai = perf.ai_stats || {};
        document.getElementById('ai-stats').innerHTML = `
            <div class="ai-stat-card"><div class="stat-label">High-Conf Win Rate</div><div class="stat-value pnl-positive">${(ai.high_confidence_win_rate||0).toFixed(1)}%</div><div class="hint">${ai.high_confidence_trades||0} trades</div></div>
            <div class="ai-stat-card"><div class="stat-label">Low-Conf Win Rate</div><div class="stat-value pnl-negative">${(ai.low_confidence_win_rate||0).toFixed(1)}%</div><div class="hint">${ai.low_confidence_trades||0} trades</div></div>
            <div class="ai-stat-card"><div class="stat-label">Avg Confidence</div><div class="stat-value">${((ai.avg_confidence||0)*100).toFixed(1)}%</div></div>
            <div class="ai-stat-card"><div class="stat-label">AI Edge</div><div class="stat-value ${(ai.high_confidence_win_rate-ai.low_confidence_win_rate)>0?'pnl-positive':'pnl-negative'}">${((ai.high_confidence_win_rate||0)-(ai.low_confidence_win_rate||0)).toFixed(1)}%</div></div>`;
    } catch (err) { showToast(err.message, 'error', 'Analytics Load Failed'); }
}

// ─── Settings ───
function setupExchangeToggle() {
    const sel = document.getElementById('set-exchange');
    sel?.addEventListener('change', toggleExchangePasswordField);
    document.getElementById('set-ai-provider')?.addEventListener('change', toggleCustomAIFields);
}

function toggleExchangePasswordField() {
    const exchange = document.getElementById('set-exchange')?.value;
    const group = document.getElementById('password-group');
    if (group) group.style.display = ['okx','bitget'].includes(exchange) ? 'block' : 'none';
}

function toggleCustomAIFields() {
    const provider = document.getElementById('set-ai-provider')?.value;
    const fields = document.getElementById('custom-ai-fields');
    if (fields) fields.style.display = provider === 'custom' ? 'block' : 'none';
}

function toggleExitModeFields() {
    const mode = document.querySelector('input[name="exit-management-mode"]:checked')?.value || 'ai';
    const aiFields = document.getElementById('ai-exit-fields');
    const customFields = document.getElementById('custom-exit-fields');
    if (aiFields) aiFields.style.display = mode === 'ai' ? 'block' : 'none';
    if (customFields) customFields.style.display = mode === 'custom' ? 'block' : 'none';
}

async function loadSettings() {
    try {
        const status = await fetchAPI('/api/status');
        if (document.getElementById('set-exchange') && status.exchange) document.getElementById('set-exchange').value = status.exchange;
        toggleExchangePasswordField();
        if (document.getElementById('set-ai-provider') && status.ai_provider) document.getElementById('set-ai-provider').value = status.ai_provider;
        if (document.getElementById('set-custom-provider-enabled')) document.getElementById('set-custom-provider-enabled').checked = Boolean(status.custom_provider_enabled);
        if (document.getElementById('set-custom-provider-name')) document.getElementById('set-custom-provider-name').value = status.custom_provider_name || 'custom';
        if (document.getElementById('set-custom-provider-model')) document.getElementById('set-custom-provider-model').value = status.custom_provider_model || '';
        if (document.getElementById('set-custom-provider-url')) document.getElementById('set-custom-provider-url').value = status.custom_provider_url || '';
        setFieldValue('set-ai-temp', status.ai_temperature ?? 0.3);
        setFieldValue('set-ai-tokens', status.ai_max_tokens ?? 1000);
        setFieldValue('set-ai-prompt', status.ai_custom_system_prompt || '');
        setFieldValue('set-tg-chat', status.telegram?.chat_id || '');
        toggleCustomAIFields();
        const tp = status.take_profit || {};
        setFieldValue('set-tp-levels', tp.num_levels ?? status.tp_levels ?? 1);
        setFieldValue('set-tp1-pct', tp.tp1_pct ?? 2);
        setFieldValue('set-tp2-pct', tp.tp2_pct ?? 4);
        setFieldValue('set-tp3-pct', tp.tp3_pct ?? 6);
        setFieldValue('set-tp4-pct', tp.tp4_pct ?? 10);
        setFieldValue('set-tp1-qty', tp.tp1_qty ?? 25);
        setFieldValue('set-tp2-qty', tp.tp2_qty ?? 25);
        setFieldValue('set-tp3-qty', tp.tp3_qty ?? 25);
        setFieldValue('set-tp4-qty', tp.tp4_qty ?? 25);
        toggleTPLevels();
        const ts = status.trailing_stop || {};
        setFieldValue('set-ts-mode', ts.mode ?? status.trailing_stop_mode ?? 'none');
        setFieldValue('set-ts-trail-pct', ts.trail_pct ?? 1.0);
        setFieldValue('set-ts-activation', ts.activation_profit_pct ?? 1.0);
        setFieldValue('set-ts-step', ts.trailing_step_pct ?? 0.5);
        toggleTSFields();
        const risk = status.risk || {};
        setFieldValue('set-max-pos', risk.max_position_pct ?? 10);
        setFieldValue('set-max-trades', risk.max_daily_trades ?? 10);
        setFieldValue('set-max-loss', risk.max_daily_loss_pct ?? 5);
        setFieldValue('set-custom-sl', risk.custom_stop_loss_pct ?? 1.5);
        setFieldValue('set-ai-exit-prompt', risk.ai_exit_system_prompt || '');
        const mode = risk.exit_management_mode === 'custom' ? 'custom' : 'ai';
        const modeEl = document.getElementById(`exit-mode-${mode}`);
        if (modeEl) modeEl.checked = true;
        toggleExitModeFields();
    } catch (e) { console.error('Settings load error:', e); }
}

async function testConnection() {
    const btn = document.getElementById('btn-test-conn');
    const result = document.getElementById('conn-result');
    btn.disabled = true; btn.innerHTML = '<i class="ri-loader-4-line"></i> Testing...';
    try {
        const resp = await fetchAPI('/api/test-connection', { method:'POST', body:JSON.stringify({ exchange:document.getElementById('set-exchange').value, api_key:document.getElementById('set-api-key').value, api_secret:document.getElementById('set-api-secret').value, password:document.getElementById('set-password').value })});
        result.className = `conn-result ${resp.success?'success':'error'}`;
        result.textContent = resp.message;
    } catch (e) { result.className = 'conn-result error'; result.textContent = `Failed: ${e.message}`; }
    btn.disabled = false; btn.innerHTML = '<i class="ri-link"></i> Test Connection';
}

async function saveExchangeSettings() { await saveSettings('/api/settings/exchange', { exchange:document.getElementById('set-exchange').value, api_key:document.getElementById('set-api-key').value, api_secret:document.getElementById('set-api-secret').value, password:document.getElementById('set-password').value }, 'btn-save-exchange'); }
async function saveAISettings() { await saveSettings('/api/settings/ai', { provider:document.getElementById('set-ai-provider').value, api_key:document.getElementById('set-ai-key').value, temperature:parseFloat(document.getElementById('set-ai-temp').value)||0.3, max_tokens:parseInt(document.getElementById('set-ai-tokens').value)||1000, custom_system_prompt:document.getElementById('set-ai-prompt').value||'', custom_provider_enabled:document.getElementById('set-custom-provider-enabled')?.checked||false, custom_provider_name:document.getElementById('set-custom-provider-name')?.value||'custom', custom_provider_model:document.getElementById('set-custom-provider-model')?.value||'', custom_provider_api_url:document.getElementById('set-custom-provider-url')?.value||'' }, 'btn-save-ai'); }
async function saveTelegramSettings() { await saveSettings('/api/settings/telegram', { bot_token:document.getElementById('set-tg-token').value, chat_id:document.getElementById('set-tg-chat').value }); }
async function saveRiskSettings() {
    const mode = document.querySelector('input[name="exit-management-mode"]:checked')?.value || 'ai';
    await saveSettings('/api/settings/risk', {
        max_position_pct: parseFloat(document.getElementById('set-max-pos').value) || 10,
        max_daily_trades: parseInt(document.getElementById('set-max-trades').value) || 10,
        max_daily_loss_pct: parseFloat(document.getElementById('set-max-loss').value) || 5,
        exit_management_mode: mode,
        custom_stop_loss_pct: parseFloat(document.getElementById('set-custom-sl').value) || 1.5,
        ai_exit_system_prompt: document.getElementById('set-ai-exit-prompt').value || '',
    });
}

// ─── Take-Profit ───
function toggleTPLevels() { const num = parseInt(document.getElementById('set-tp-levels').value)||1; for(let i=1;i<=4;i++){const r=document.getElementById(`tp-row-${i}`);if(r)r.style.display=i<=num?'block':'none';} }
async function saveTPSettings() {
    const data = { num_levels:parseInt(document.getElementById('set-tp-levels').value)||1, tp1_pct:parseFloat(document.getElementById('set-tp1-pct').value)||2.0, tp2_pct:parseFloat(document.getElementById('set-tp2-pct').value)||4.0, tp3_pct:parseFloat(document.getElementById('set-tp3-pct').value)||6.0, tp4_pct:parseFloat(document.getElementById('set-tp4-pct').value)||10.0, tp1_qty:parseFloat(document.getElementById('set-tp1-qty').value)||25.0, tp2_qty:parseFloat(document.getElementById('set-tp2-qty').value)||25.0, tp3_qty:parseFloat(document.getElementById('set-tp3-qty').value)||25.0, tp4_qty:parseFloat(document.getElementById('set-tp4-qty').value)||25.0 };
    const total = [data.tp1_qty,data.tp2_qty,data.tp3_qty,data.tp4_qty].slice(0,data.num_levels).reduce((a,b)=>a+b,0);
    if (total > 100) { showToast(`Total close % is ${total}%. Must be ≤ 100%.`,'warning','Invalid TP Config'); return; }
    await saveSettings('/api/settings/take-profit', data);
    showToast(`${data.num_levels} TP levels saved.`,'success','Take-Profit Updated');
}

// ─── Trailing Stop ───
function toggleTSFields() {
    const mode = document.getElementById('set-ts-mode').value;
    const m = document.getElementById('ts-moving-fields'), p = document.getElementById('ts-profit-fields'), d = document.getElementById('ts-description'), dt = document.getElementById('ts-description-text');
    m.style.display = 'none'; p.style.display = 'none'; d.style.display = 'none';
    const descs = { none:'', moving:'The stop-loss will trail behind the price by the specified percentage.', breakeven_on_tp1:'When TP1 is reached, the stop-loss moves to the entry price (breakeven).', step_trailing:'As each TP is reached, SL moves to the previous TP price.', profit_pct_trailing:'The trailing stop activates after unrealized profit reaches the threshold.' };
    if (mode === 'moving') m.style.display = 'block';
    else if (mode === 'profit_pct_trailing') p.style.display = 'block';
    if (descs[mode]) { dt.textContent = descs[mode]; d.style.display = 'flex'; }
}
async function saveTSSettings() {
    const data = { mode:document.getElementById('set-ts-mode').value, trail_pct:parseFloat(document.getElementById('set-ts-trail-pct').value)||1.0, activation_profit_pct:parseFloat(document.getElementById('set-ts-activation').value)||1.0, trailing_step_pct:parseFloat(document.getElementById('set-ts-step').value)||0.5 };
    await saveSettings('/api/settings/trailing-stop', data);
    showToast(`Trailing stop: ${data.mode}`,'success','Trailing Stop Updated');
}

// ─── Subscription Page ───
async function loadSubscription() {
    try {
        const [plans, mySub, myPayments, me] = await Promise.all([
            fetchAPI('/api/plans'),
            fetchAPI('/api/my-subscription'),
            fetchAPI('/api/my-payments'),
            fetchAPI('/api/auth/me'),
        ]);
        const currentUser = getUser();
        localStorage.setItem('tvss_user', JSON.stringify({
            ...currentUser,
            id: me.id,
            username: me.username,
            email: me.email,
            role: me.role,
            balance_usdt: me.balance_usdt,
        }));
        updateUserUI();
        const balance = Number(me.balance_usdt || 0);
        // Current subscription status
        const statusEl = document.getElementById('sub-status');
        if (mySub && mySub.status === 'active') {
            const endDate = new Date(mySub.end_date).toLocaleDateString();
            statusEl.innerHTML = `<div class="sub-active"><i class="ri-checkbox-circle-fill"></i><div><strong>${escapeHtml(mySub.plan_name)}</strong><br><span style="color:var(--text-muted);font-size:13px">Active until ${endDate} · Balance ${formatNum(balance)} USDT</span></div></div>`;
        } else {
            statusEl.innerHTML = `<div class="sub-inactive"><i class="ri-close-circle-line"></i><span>No active subscription · Balance ${formatNum(balance)} USDT</span></div>`;
        }

        // Available plans
        const plansEl = document.getElementById('plans-grid');
        plansEl.innerHTML = plans.map(p => {
            const features = Array.isArray(p.features) ? p.features : JSON.parse(p.features_json || '[]');
            const price = Number(p.price_usdt || 0);
            const buttonText = price <= 0 ? 'Activate Free' : (balance >= price ? 'Pay With Balance' : 'Pay USDT');
            return `<div class="plan-card"><h3>${escapeHtml(p.name)}</h3><div class="plan-price">${price > 0 ? '$' + formatNum(price) : 'Free'}</div><p class="plan-desc">${escapeHtml(p.description)}</p><ul class="plan-features">${features.map(f=>`<li><i class="ri-check-line"></i>${escapeHtml(f)}</li>`).join('')}</ul><button class="btn-plan" onclick="subscribeToPlan('${escapeJsSingle(p.id)}',${price})">${buttonText}</button></div>`;
        }).join('');

        // Payment history
        const payEl = document.getElementById('payment-history');
        if (myPayments.length) {
            payEl.innerHTML = `<table class="data-table"><thead><tr><th>Date</th><th>Amount</th><th>Network</th><th>Status</th><th>TX</th></tr></thead><tbody>${myPayments.map(p => `<tr><td>${escapeHtml(new Date(p.created_at).toLocaleDateString())}</td><td>${escapeHtml(p.amount)} ${escapeHtml(p.currency)}</td><td>${escapeHtml(p.network)}</td><td><span class="badge badge-${safeClassToken(p.status)}">${escapeHtml(p.status)}</span></td><td>${p.tx_hash ? escapeHtml(p.tx_hash.slice(0,12))+'...' : '--'}</td></tr>`).join('')}</tbody></table>`;
        } else {
            payEl.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:20px">No payments yet</p>';
        }
    } catch (err) { showToast(err.message, 'error', 'Subscription Load Failed'); }
}

async function subscribeToPlan(planId, price) {
    try {
        const sub = await fetchAPI('/api/subscribe', { method:'POST', body:JSON.stringify({ plan_id:planId }) });
        if (price <= 0 || sub.status === 'active') {
            showToast(sub.paid_from_balance ? 'Subscription paid from account balance.' : 'Subscription activated.','success','Subscribed');
            loadSubscription();
            return;
        }
        // Show payment modal
        showPaymentModal(sub.id, price);
    } catch (err) { showToast(err.message,'error','Subscribe Failed'); }
}

async function redeemCardCode() {
    const input = document.getElementById('redeem-code-input');
    const code = input?.value.trim();
    if (!code) {
        showToast('Please enter a card code.','warning','Missing Code');
        return;
    }
    try {
        const result = await fetchAPI('/api/redeem-code', { method:'POST', body:JSON.stringify({ code }) });
        input.value = '';
        const pieces = [];
        if (Number(result.balance_usdt || 0) > 0) pieces.push(`${formatNum(result.balance_usdt)} USDT balance`);
        if (result.subscription) pieces.push('subscription activated');
        showToast(pieces.length ? pieces.join(' + ') : 'Code redeemed.', 'success', 'Redeemed');
        loadSubscription();
    } catch (err) {
        showToast(err.message, 'error', 'Redeem Failed');
    }
}

async function showPaymentModal(subscriptionId, amount) {
    const options = await fetchAPI('/api/payment-options');
    const modal = document.getElementById('payment-modal');
    const body = document.getElementById('payment-modal-body');

    if (!options.networks.length) {
        showToast('No payment address has been configured yet. Please contact the admin.','warning','Payment Unavailable');
        return;
    }
    let network = options.networks.length > 0 ? options.networks[0].network : 'TRC20';
    body.innerHTML = `
        <h3 style="margin-bottom:16px">Pay ${amount} USDT</h3>
        <div class="form-group"><label>Payment Network</label>
            <select id="pay-network" class="form-input" onchange="updatePaymentAddress('${subscriptionId}',${amount})">
                ${options.networks.map(n => `<option value="${n.network}">${n.name} (fee: ${n.fee})</option>`).join('')}
            </select>
        </div>
        <div id="pay-address-info" style="margin-top:16px"></div>
        <div class="form-group" style="margin-top:16px"><label>Transaction Hash (TX ID)</label><input type="text" id="pay-tx-hash" class="form-input" placeholder="Paste your TX hash after sending"></div>
        <div style="display:flex;gap:12px;margin-top:16px">
            <button class="btn btn-primary" onclick="submitPayment('${subscriptionId}')"><i class="ri-check-line"></i> Submit Payment</button>
            <button class="btn btn-secondary" onclick="closePaymentModal()">Cancel</button>
        </div>`;
    modal.style.display = 'flex';
    updatePaymentAddress(subscriptionId, amount);
}

async function updatePaymentAddress(subscriptionId, amount) {
    const network = document.getElementById('pay-network').value;
    try {
        const payment = await fetchAPI('/api/payment/create', { method:'POST', body:JSON.stringify({ subscription_id:subscriptionId, currency:'USDT', network:network }) });
        const infoEl = document.getElementById('pay-address-info');
        if (payment.status === 'activated') {
            closePaymentModal();
            showToast('Free plan activated!','success');
            loadSubscription();
            return;
        }
        infoEl.innerHTML = `<div class="payment-address-box"><label>Send to this address:</label><div class="address-display"><code>${escapeHtml(payment.address)}</code><button class="btn-copy" onclick="copyText('${escapeJsSingle(payment.address)}','Address copied!')"><i class="ri-file-copy-line"></i></button></div><p style="color:var(--text-muted);font-size:12px;margin-top:8px">Network: ${escapeHtml(payment.network_name)} · Confirmation: ${escapeHtml(payment.confirmation_time)}</p></div>`;
        // Store payment_id for submission
        document.getElementById('pay-tx-hash').dataset.paymentId = payment.id;
    } catch (err) { showToast(err.message,'error'); }
}

async function submitPayment(subscriptionId) {
    const txHash = document.getElementById('pay-tx-hash').value;
    const paymentId = document.getElementById('pay-tx-hash').dataset.paymentId;
    if (!txHash) { showToast('Please enter the TX hash','warning'); return; }
    try {
        await fetchAPI('/api/payment/submit-tx', { method:'POST', body:JSON.stringify({ payment_id:paymentId, tx_hash:txHash }) });
        showToast('Payment submitted for review!','success');
        closePaymentModal();
        loadSubscription();
    } catch (err) { showToast(err.message,'error'); }
}

function closePaymentModal() {
    document.getElementById('payment-modal').style.display = 'none';
}

// ─── Admin Panel ───
async function loadAdminLegacyUnused() {
    return loadAdmin();
    if (!isAdmin()) { showToast('Admin access required','error'); return; }
    try {
        const [users, payments] = await Promise.all([fetchAPI('/api/admin/users'), fetchAPI('/api/admin/payments')]);
        // Users table
        const usersEl = document.getElementById('admin-users');
        usersEl.innerHTML = `<table class="data-table"><thead><tr><th>Username</th><th>Email</th><th>Role</th><th>Subscription</th><th>Status</th><th>Actions</th></tr></thead><tbody>${users.map(u => `<tr><td><strong>${escapeHtml(u.username)}</strong></td><td>${escapeHtml(u.email)}</td><td><span class="role-badge ${safeClassToken(u.role)}">${escapeHtml(u.role)}</span></td><td>${u.subscription ? escapeHtml(u.subscription.plan_name) : '<span style="color:var(--text-muted)">None</span>'}</td><td>${u.is_active ? '<span class="badge badge-active">Active</span>' : '<span class="badge badge-inactive">Disabled</span>'}</td><td>${u.role !== 'admin' ? `<button class="btn-sm" onclick="toggleUser('${u.id}')">${u.is_active ? 'Disable' : 'Enable'}</button>` : ''}</td></tr>`).join('')}</tbody></table>`;

        // Pending payments
        const pendingPayments = payments.filter(p => p.status === 'submitted');
        const payEl = document.getElementById('admin-payments');
        if (pendingPayments.length) {
            payEl.innerHTML = `<table class="data-table"><thead><tr><th>User</th><th>Amount</th><th>Network</th><th>TX Hash</th><th>Date</th><th>Actions</th></tr></thead><tbody>${pendingPayments.map(p => `<tr><td>${escapeHtml(p.username||'--')}</td><td>${escapeHtml(p.amount)} ${escapeHtml(p.currency)}</td><td>${escapeHtml(p.network)}</td><td><code style="font-size:11px">${p.tx_hash?escapeHtml(p.tx_hash.slice(0,20))+'...':'--'}</code></td><td>${escapeHtml(new Date(p.created_at).toLocaleDateString())}</td><td><div style="display:flex;gap:6px"><button class="btn-sm btn-success" onclick="adminConfirmPayment('${p.id}')">✓ Confirm</button><button class="btn-sm btn-danger" onclick="adminRejectPayment('${p.id}')">✕ Reject</button></div></td></tr>`).join('')}</tbody></table>`;
        } else {
            payEl.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:20px">No pending payments</p>';
        }
    } catch (err) { showToast(err.message, 'error', 'Admin Load Failed'); }
}

async function toggleUser(userId) {
    try { await fetchAPI(`/api/admin/user/${userId}/toggle`, {method:'POST'}); loadAdmin(); showToast('User status updated','success'); }
    catch (err) { showToast(err.message,'error'); }
}
async function adminConfirmPayment(paymentId) {
    try { await fetchAPI(`/api/admin/payment/${paymentId}/confirm`, {method:'POST'}); loadAdmin(); showToast('Payment confirmed & subscription activated!','success'); }
    catch (err) { showToast(err.message,'error'); }
}
async function adminRejectPayment(paymentId) {
    try { await fetchAPI(`/api/admin/payment/${paymentId}/reject`, {method:'POST'}); loadAdmin(); showToast('Payment rejected','warning'); }
    catch (err) { showToast(err.message,'error'); }
}

// ─── Helpers ───
async function loadAdmin() {
    if (!isAdmin()) { showToast('Admin access required','error'); return; }
    try {
        const [users, payments, plans, addresses, registration, invites, redeemCodes] = await Promise.all([
            fetchAPI('/api/admin/users'),
            fetchAPI('/api/admin/payments'),
            fetchAPI('/api/admin/plans'),
            fetchAPI('/api/admin/payment-addresses'),
            fetchAPI('/api/admin/registration'),
            fetchAPI('/api/admin/invite-codes'),
            fetchAPI('/api/admin/redeem-codes'),
        ]);

        renderAdminUsers(users, plans);
        renderAdminPaymentAddresses(addresses || {});
        renderAdminRegistration(registration || {}, invites || []);
        renderAdminRedeemCodes(redeemCodes || [], plans || []);
        renderAdminPendingPayments(payments || []);
    } catch (err) { showToast(err.message, 'error', 'Admin Load Failed'); }
}

function renderAdminUsers(users, plans) {
    const usersEl = document.getElementById('admin-users');
    if (!usersEl) return;
    if (!users.length) {
        usersEl.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:20px">No users found</p>';
        return;
    }
    usersEl.innerHTML = `<div class="table-wrapper"><table class="data-table admin-users-table"><thead><tr><th>Account</th><th>Role</th><th>Status</th><th>Balance</th><th>Password</th><th>Current Subscription</th><th>Grant Subscription</th><th>Actions</th></tr></thead><tbody>${users.map(u => {
        const id = escapeHtml(u.id);
        const jsId = escapeJsSingle(u.id);
        const active = Boolean(u.is_active);
        const sub = u.subscription ? `<strong>${escapeHtml(u.subscription.plan_name || u.subscription.plan_id)}</strong><br><span class="hint">Until ${escapeHtml(formatDateTime(u.subscription.end_date))}</span>` : '<span style="color:var(--text-muted)">None</span>';
        return `<tr>
            <td><div class="admin-stack"><input id="admin-username-${id}" class="text-input table-input" value="${escapeHtml(u.username)}" autocomplete="off"><input id="admin-email-${id}" class="text-input table-input" value="${escapeHtml(u.email)}" autocomplete="off"></div></td>
            <td><select id="admin-role-${id}" class="select-input table-input"><option value="user" ${u.role === 'user' ? 'selected' : ''}>User</option><option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option></select></td>
            <td><select id="admin-active-${id}" class="select-input table-input"><option value="true" ${active ? 'selected' : ''}>Active</option><option value="false" ${!active ? 'selected' : ''}>Disabled</option></select></td>
            <td><input id="admin-balance-${id}" type="number" class="text-input table-input" value="${Number(u.balance_usdt || 0).toFixed(2)}" min="0" step="0.01"></td>
            <td><div class="admin-stack"><input id="admin-password-${id}" type="password" class="text-input table-input" placeholder="New password" autocomplete="new-password"><button class="btn btn-sm btn-primary" onclick="resetAdminPassword('${jsId}')">Reset</button></div></td>
            <td>${sub}</td>
            <td><div class="admin-stack"><select id="admin-plan-${id}" class="select-input table-input">${planOptions(plans)}</select><div class="admin-inline"><input id="admin-duration-${id}" type="number" class="text-input table-input" min="0" step="1" placeholder="Plan days"><select id="admin-substatus-${id}" class="select-input table-input"><option value="active">Active</option><option value="pending">Pending</option></select></div><button class="btn btn-sm btn-primary" onclick="grantSubscription('${jsId}')">Grant</button></div></td>
            <td><div class="admin-actions"><button class="btn btn-sm btn-success" onclick="saveAdminUser('${jsId}')">Save</button>${u.role !== 'admin' ? `<button class="btn btn-sm" onclick="toggleUser('${jsId}')">${active ? 'Disable' : 'Enable'}</button>` : ''}</div></td>
        </tr>`;
    }).join('')}</tbody></table></div>`;
}

function renderAdminPaymentAddresses(addresses) {
    const el = document.getElementById('admin-payment-addresses');
    if (!el) return;
    el.innerHTML = `<div class="settings-form admin-mini-form">${USDT_PAYMENT_NETWORKS.map(n => {
        const address = addresses[n.id]?.address || '';
        return `<div class="form-row admin-address-row">
            <div class="form-group"><label>${escapeHtml(n.name)}</label><input id="pay-address-${n.id}" class="text-input" value="${escapeHtml(address)}" placeholder="USDT receiving address"></div>
            <div class="form-group admin-button-bottom"><button class="btn btn-primary" onclick="savePaymentAddress('${n.id}')"><i class="ri-save-line"></i> Save</button></div>
        </div>`;
    }).join('')}</div>`;
}

function renderAdminRegistration(registration, invites) {
    const el = document.getElementById('admin-registration');
    if (!el) return;
    const rows = invites.length ? invites.map(c => {
        const active = c.is_active && Number(c.used_count || 0) < Number(c.max_uses || 0);
        const status = active ? 'active' : 'inactive';
        return `<tr><td><code>${escapeHtml(c.code)}</code></td><td>${escapeHtml(c.used_count || 0)} / ${escapeHtml(c.max_uses || 1)}</td><td>${escapeHtml(c.expires_at || '--')}</td><td>${escapeHtml(c.note || '')}</td><td><span class="badge badge-${status}">${status}</span></td><td><button class="btn btn-sm" onclick="copyText('${escapeJsSingle(c.code)}','Invite code copied')">Copy</button></td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty-state">No invite codes yet</td></tr>';
    el.innerHTML = `<div class="settings-form">
        <label class="checkbox-label"><input type="checkbox" id="admin-invite-required" ${registration.invite_required ? 'checked' : ''}><span>Require invite code for new registrations</span></label>
        <div class="form-row"><button class="btn btn-success" onclick="saveRegistrationSettings()"><i class="ri-save-line"></i> Save Registration Settings</button></div>
        <div class="form-row three-col admin-create-row">
            <div class="form-group"><label for="invite-max-uses">Max Uses</label><input type="number" id="invite-max-uses" class="text-input" value="1" min="1" max="1000"></div>
            <div class="form-group"><label for="invite-expires">Expires</label><input type="date" id="invite-expires" class="text-input"></div>
            <div class="form-group"><label for="invite-note">Note</label><input type="text" id="invite-note" class="text-input" placeholder="Optional"></div>
        </div>
        <div class="form-row"><button class="btn btn-primary" onclick="createInviteCode()"><i class="ri-key-2-line"></i> Generate Invite Code</button></div>
        <div class="table-wrapper mt-4"><table class="data-table"><thead><tr><th>Code</th><th>Uses</th><th>Expires</th><th>Note</th><th>Status</th><th>Copy</th></tr></thead><tbody>${rows}</tbody></table></div>
    </div>`;
}

function renderAdminRedeemCodes(codes, plans) {
    const el = document.getElementById('admin-redeem-codes');
    if (!el) return;
    const rows = codes.length ? codes.map(c => {
        const parts = [];
        if (c.plan_name) parts.push(escapeHtml(c.plan_name));
        if (Number(c.balance_usdt || 0) > 0) parts.push(`${formatNum(c.balance_usdt)} USDT`);
        const status = (!c.is_active || c.redeemed_by) ? 'inactive' : 'active';
        return `<tr><td><code>${escapeHtml(c.code)}</code></td><td>${parts.length ? parts.join(' + ') : '--'}</td><td>${escapeHtml(c.redeemed_by_username || '--')}</td><td>${escapeHtml(c.expires_at || '--')}</td><td><span class="badge badge-${status}">${status === 'active' ? 'Active' : 'Used'}</span></td><td><button class="btn btn-sm" onclick="copyText('${escapeJsSingle(c.code)}','Card code copied')">Copy</button></td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty-state">No card codes yet</td></tr>';
    el.innerHTML = `<div class="settings-form">
        <div class="form-row three-col admin-create-row">
            <div class="form-group"><label for="redeem-plan">Subscription Plan</label><select id="redeem-plan" class="select-input">${planOptions(plans, '', 'No subscription')}</select></div>
            <div class="form-group"><label for="redeem-duration">Duration Override</label><input type="number" id="redeem-duration" class="text-input" value="0" min="0" step="1"><p class="hint">0 uses the plan duration</p></div>
            <div class="form-group"><label for="redeem-balance">Balance USDT</label><input type="number" id="redeem-balance" class="text-input" value="0" min="0" step="0.01"></div>
        </div>
        <div class="form-row two-col admin-create-row">
            <div class="form-group"><label for="redeem-expires">Expires</label><input type="date" id="redeem-expires" class="text-input"></div>
            <div class="form-group"><label for="redeem-note">Note</label><input type="text" id="redeem-note" class="text-input" placeholder="Optional"></div>
        </div>
        <div class="form-row"><button class="btn btn-primary" onclick="createRedeemCode()"><i class="ri-coupon-3-line"></i> Generate Card Code</button></div>
        <div class="table-wrapper mt-4"><table class="data-table"><thead><tr><th>Code</th><th>Benefit</th><th>Redeemed By</th><th>Expires</th><th>Status</th><th>Copy</th></tr></thead><tbody>${rows}</tbody></table></div>
    </div>`;
}

function renderAdminPendingPayments(payments) {
    const pendingPayments = payments.filter(p => p.status === 'submitted');
    const payEl = document.getElementById('admin-payments');
    if (!payEl) return;
    if (pendingPayments.length) {
        payEl.innerHTML = `<div class="table-wrapper"><table class="data-table"><thead><tr><th>User</th><th>Amount</th><th>Network</th><th>TX Hash</th><th>Date</th><th>Actions</th></tr></thead><tbody>${pendingPayments.map(p => `<tr><td>${escapeHtml(p.username||'--')}</td><td>${escapeHtml(p.amount)} ${escapeHtml(p.currency)}</td><td>${escapeHtml(p.network)}</td><td><code style="font-size:11px">${p.tx_hash?escapeHtml(p.tx_hash.slice(0,20))+'...':'--'}</code></td><td>${escapeHtml(formatDateTime(p.created_at))}</td><td><div class="admin-actions"><button class="btn btn-sm btn-success" onclick="adminConfirmPayment('${escapeJsSingle(p.id)}')">Confirm</button><button class="btn btn-sm btn-danger" onclick="adminRejectPayment('${escapeJsSingle(p.id)}')">Reject</button></div></td></tr>`).join('')}</tbody></table></div>`;
    } else {
        payEl.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:20px">No pending payments</p>';
    }
}

function planOptions(plans, selected = '', emptyLabel = 'Select plan...') {
    return `<option value="">${escapeHtml(emptyLabel)}</option>${plans.map(p => `<option value="${escapeHtml(p.id)}" ${p.id === selected ? 'selected' : ''}>${escapeHtml(p.name)} (${formatNum(p.price_usdt)} USDT)</option>`).join('')}`;
}

async function saveAdminUser(userId) {
    const data = {
        username: document.getElementById(`admin-username-${userId}`)?.value || '',
        email: document.getElementById(`admin-email-${userId}`)?.value || '',
        role: document.getElementById(`admin-role-${userId}`)?.value || 'user',
        is_active: document.getElementById(`admin-active-${userId}`)?.value === 'true',
        balance_usdt: parseFloat(document.getElementById(`admin-balance-${userId}`)?.value) || 0,
    };
    try {
        const result = await fetchAPI(`/api/admin/user/${encodeURIComponent(userId)}`, { method:'PUT', body:JSON.stringify(data) });
        const current = getUser();
        if (current.id === userId && result.user) {
            localStorage.setItem('tvss_user', JSON.stringify({ ...current, ...result.user }));
            updateUserUI();
        }
        showToast('User account updated.','success','Saved');
        loadAdmin();
    } catch (err) { showToast(err.message,'error','Save Failed'); }
}

async function resetAdminPassword(userId) {
    const input = document.getElementById(`admin-password-${userId}`);
    const password = input?.value || '';
    if (password.length < 6) {
        showToast('Password must be at least 6 characters.','warning','Invalid Password');
        return;
    }
    try {
        await fetchAPI(`/api/admin/user/${encodeURIComponent(userId)}/password`, { method:'POST', body:JSON.stringify({ password }) });
        input.value = '';
        showToast('Password updated.','success','Saved');
    } catch (err) {
        showToast(err.message,'error','Password Reset Failed');
    }
}

async function grantSubscription(userId) {
    const planId = document.getElementById(`admin-plan-${userId}`)?.value;
    if (!planId) {
        showToast('Choose a subscription plan first.','warning','Missing Plan');
        return;
    }
    const data = {
        plan_id: planId,
        duration_days: parseInt(document.getElementById(`admin-duration-${userId}`)?.value) || 0,
        status: document.getElementById(`admin-substatus-${userId}`)?.value || 'active',
    };
    try {
        await fetchAPI(`/api/admin/user/${encodeURIComponent(userId)}/subscription`, { method:'POST', body:JSON.stringify(data) });
        showToast('Subscription updated.','success','Saved');
        loadAdmin();
    } catch (err) { showToast(err.message,'error','Grant Failed'); }
}

async function savePaymentAddress(network) {
    const address = document.getElementById(`pay-address-${network}`)?.value.trim();
    if (!address) {
        showToast('Payment address cannot be empty.','warning','Missing Address');
        return;
    }
    try {
        await fetchAPI('/api/admin/payment-addresses', { method:'POST', body:JSON.stringify({ network, address }) });
        showToast(`${network} address saved.`, 'success', 'Saved');
        loadAdmin();
    } catch (err) { showToast(err.message,'error','Save Failed'); }
}

async function saveRegistrationSettings() {
    const inviteRequired = document.getElementById('admin-invite-required')?.checked || false;
    try {
        await fetchAPI('/api/admin/registration', { method:'POST', body:JSON.stringify({ invite_required: inviteRequired }) });
        showToast('Registration settings saved.','success','Saved');
        loadAdmin();
    } catch (err) { showToast(err.message,'error','Save Failed'); }
}

async function createInviteCode() {
    const data = {
        max_uses: parseInt(document.getElementById('invite-max-uses')?.value) || 1,
        expires_at: document.getElementById('invite-expires')?.value || '',
        note: document.getElementById('invite-note')?.value || '',
    };
    try {
        const created = await fetchAPI('/api/admin/invite-codes', { method:'POST', body:JSON.stringify(data) });
        showToast(created.code, 'success', 'Invite Code Generated');
        loadAdmin();
    } catch (err) { showToast(err.message,'error','Generate Failed'); }
}

async function createRedeemCode() {
    const data = {
        plan_id: document.getElementById('redeem-plan')?.value || '',
        duration_days: parseInt(document.getElementById('redeem-duration')?.value) || 0,
        balance_usdt: parseFloat(document.getElementById('redeem-balance')?.value) || 0,
        expires_at: document.getElementById('redeem-expires')?.value || '',
        note: document.getElementById('redeem-note')?.value || '',
    };
    if (!data.plan_id && data.balance_usdt <= 0) {
        showToast('Choose a plan or enter a balance amount.','warning','Missing Benefit');
        return;
    }
    try {
        const created = await fetchAPI('/api/admin/redeem-codes', { method:'POST', body:JSON.stringify(data) });
        showToast(created.code, 'success', 'Card Code Generated');
        loadAdmin();
    } catch (err) { showToast(err.message,'error','Generate Failed'); }
}

async function saveSettings(endpoint, data, btnId) {
    const btn = btnId ? document.getElementById(btnId) : null;
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ri-loader-4-line"></i> Saving...'; }
    try {
        await fetchAPI(endpoint, { method:'POST', body:JSON.stringify(data) });
        showToast('Settings saved.','success','Saved');
        if (btn) { btn.innerHTML = '<i class="ri-check-line"></i> Saved!'; }
        setTimeout(() => { if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ri-save-line"></i> Save'; } }, 2000);
    } catch (e) {
        showToast(e.message,'error','Save Failed');
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ri-save-line"></i> Save'; }
    }
}

async function testTelegram() {
    try { await fetchAPI('/api/test-telegram',{method:'POST'}); showToast('Check your Telegram.','success','Test Sent'); }
    catch (e) { showToast(e.message,'error','Test Failed'); }
}

function detectWebhookUrl() {
    const url = `${window.location.origin}/webhook`;
    const el = document.getElementById('webhook-url');
    if (el) el.textContent = url;
}

function copyWebhookUrl(evt) {
    const url = document.getElementById('webhook-url')?.textContent;
    if (url) {
        navigator.clipboard.writeText(url).then(() => showToast(url,'success','Webhook URL copied'));
        const btn = evt?.target?.closest('.btn');
        if (btn) { btn.innerHTML = '<i class="ri-check-line"></i>'; setTimeout(() => { btn.innerHTML = '<i class="ri-file-copy-line"></i>'; }, 1500); }
    }
}

async function fetchAPI(path, options = {}) {
    const token = getToken();
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const resp = await fetch(`${API}${path}`, { headers, ...options });
    if (resp.status === 401) { logout(); throw new Error('Session expired'); }
    if (!resp.ok) {
        const data = await resp.json().catch(()=>({}));
        throw new Error(data.detail || `API error: ${resp.status}`);
    }
    return resp.json();
}

function firstDefined(...values) { return values.find(v => v !== undefined && v !== null); }
function setFieldValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value ?? '';
}
function pickBalance(section, quote = 'USDT') {
    if (!section || typeof section !== 'object') return 0;
    return firstDefined(section[quote], section.USDT, section.USD, section.USDC, 0);
}
function formatNum(n) {
    if (n == null || n === '') return '--';
    const value = Number(n);
    if (!Number.isFinite(value)) return '--';
    return value.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
}
function formatValue(v) { if (v==='∞'||v===Infinity) return '∞'; if (typeof v==='number') return v.toFixed(2); return v||'--'; }
function formatDateTime(value) {
    if (!value) return '--';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
}
async function refreshAll() {
    const p = document.querySelector('.page.active')?.id?.replace('page-','');
    if (p==='dashboard') await loadDashboard(); else if (p==='positions') await loadPositions();
    else if (p==='history') await loadHistory(); else if (p==='analytics') await loadAnalytics();
    else if (p==='subscription') await loadSubscription(); else if (p==='admin') await loadAdmin();
}
