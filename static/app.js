/**
 * Signal Server - Dashboard Frontend Logic
 * v3.0 — Enhanced with Multi-TP, Trailing Stop, Custom AI
 */

const API = '';  // Same origin
let equityChart = null;
let dailyPnlChart = null;
let winlossChart = null;

// ─── Initialization ───
document.addEventListener('DOMContentLoaded', () => {
    setupNavigation();
    setupExchangeToggle();
    loadDashboard();
    detectWebhookUrl();
});

// ─── Toast notification system ───

/** Escape HTML special characters to prevent XSS when inserting user/server content. */
function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function showToast(message, type = 'info', title = '') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const icons = {
        success: 'ri-checkbox-circle-line',
        error: 'ri-error-warning-line',
        warning: 'ri-alert-line',
        info: 'ri-information-line',
    };
    const defaultTitles = { success: 'Success', error: 'Error', warning: 'Warning', info: 'Info' };

    const safeTitle   = escapeHtml(title || defaultTitles[type] || 'Notice');
    const safeMessage = message ? escapeHtml(message) : '';

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.setAttribute('role', 'alert');
    toast.innerHTML = `
        <i class="toast-icon ${icons[type] || icons.info}" aria-hidden="true"></i>
        <div class="toast-body">
            <div class="toast-title">${safeTitle}</div>
            ${safeMessage ? `<div class="toast-msg">${safeMessage}</div>` : ''}
        </div>
    `;
    container.appendChild(toast);

    // Auto-dismiss after 4 s
    const dismiss = () => {
        toast.classList.add('removing');
        toast.addEventListener('animationend', () => toast.remove(), { once: true });
    };
    setTimeout(dismiss, 4000);
    toast.addEventListener('click', dismiss);
}

// ─── Navigation ───
function setupNavigation() {
    document.querySelectorAll('.nav-item[data-page]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const page = item.dataset.page;
            switchPage(page);
            // Close sidebar on mobile after navigation
            closeSidebar();
        });
    });

    document.getElementById('menu-toggle')?.addEventListener('click', () => {
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebar-overlay');
        sidebar.classList.toggle('open');
        overlay.classList.toggle('visible');
    });

    document.getElementById('sidebar-overlay')?.addEventListener('click', closeSidebar);
}

function closeSidebar() {
    document.getElementById('sidebar')?.classList.remove('open');
    document.getElementById('sidebar-overlay')?.classList.remove('visible');
}

function switchPage(page) {
    document.querySelectorAll('.nav-item').forEach(n => {
        n.classList.remove('active');
        n.removeAttribute('aria-current');
    });
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));

    const navEl = document.querySelector(`[data-page="${page}"]`);
    navEl?.classList.add('active');
    navEl?.setAttribute('aria-current', 'page');
    document.getElementById(`page-${page}`)?.classList.add('active');

    const titles = {
        dashboard: 'Dashboard',
        positions: 'Positions',
        history: 'Trade History',
        analytics: 'Analytics',
        settings: 'Settings'
    };
    document.getElementById('page-title').textContent = titles[page] || page;

    // Load data for the page
    if (page === 'positions') loadPositions();
    if (page === 'history') loadHistory();
    if (page === 'analytics') loadAnalytics();
    if (page === 'settings') loadSettings();
}

// ─── Dashboard ───
async function loadDashboard() {
    try {
        const [status, stats, perf] = await Promise.all([
            fetchAPI('/api/status'),
            fetchAPI('/stats'),
            fetchAPI('/api/performance?days=30')
        ]);

        // Trading mode indicator
        if (status.live_trading) {
            const el = document.getElementById('trading-mode');
            el.innerHTML = '<span class="mode-dot live"></span><span>LIVE Trading</span>';
            el.style.background = 'var(--accent-red-bg)';
            el.style.color = 'var(--accent-red)';
        }

        // KPI Cards
        const pnl = perf.total_pnl_pct || 0;
        const pnlEl = document.getElementById('kpi-pnl');
        pnlEl.textContent = `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%`;
        pnlEl.className = `kpi-value ${pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`;

        document.getElementById('kpi-trades').textContent = perf.total_trades || 0;
        document.getElementById('kpi-winrate').textContent = `${(perf.win_rate || 0).toFixed(1)}%`;
        document.getElementById('kpi-sharpe').textContent = (perf.sharpe_ratio || 0).toFixed(2);

        // Metrics grid
        renderMetrics(perf);

        // Equity chart
        renderEquityChart(perf.equity_curve || []);

        // Recent signals
        await loadRecentSignals();

    } catch (err) {
        console.error('Dashboard load error:', err);
        showToast(err.message, 'error', 'Dashboard Load Failed');
    }
}

function renderMetrics(perf) {
    const grid = document.getElementById('metrics-grid');
    const items = [
        ['Profit Factor', formatValue(perf.profit_factor)],
        ['Risk/Reward', formatValue(perf.risk_reward_ratio)],
        ['Max Drawdown', `${(perf.max_drawdown_pct || 0).toFixed(2)}%`],
        ['Sortino Ratio', (perf.sortino_ratio || 0).toFixed(2)],
        ['Best Trade', `${(perf.best_trade_pct || 0).toFixed(2)}%`],
        ['Worst Trade', `${(perf.worst_trade_pct || 0).toFixed(2)}%`],
        ['Consec. Wins', perf.max_consecutive_wins || 0],
        ['Consec. Losses', perf.max_consecutive_losses || 0],
    ];

    grid.innerHTML = items.map(([label, value]) => `
        <div class="metric-item">
            <span class="metric-label">${label}</span>
            <span class="metric-value">${value}</span>
        </div>
    `).join('');
}

async function loadRecentSignals() {
    try {
        const trades = await fetchAPI('/trades');
        const container = document.getElementById('recent-signals');

        if (!trades.length) {
            container.innerHTML = '<div class="empty-state" style="padding:40px;text-align:center;color:var(--text-muted)">No signals today</div>';
            return;
        }

        container.innerHTML = trades.slice(-20).reverse().map(t => {
            const dir = t.direction || 'long';
            const isLong = dir.includes('long');
            const conf = t.ai?.confidence || 0;
            const time = t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : '--';

            return `
                <div class="signal-item">
                    <div class="signal-icon ${isLong ? 'long' : 'short'}">
                        <i class="ri-arrow-${isLong ? 'up' : 'down'}-line"></i>
                    </div>
                    <div class="signal-info">
                        <div class="signal-ticker">${t.ticker || '--'}</div>
                        <div class="signal-detail">${time} · ${dir.toUpperCase()}</div>
                    </div>
                    <div class="signal-conf ${conf >= 0.7 ? 'pnl-positive' : conf < 0.5 ? 'pnl-negative' : ''}">${(conf * 100).toFixed(0)}%</div>
                </div>
            `;
        }).join('');
    } catch (e) {
        console.error('Failed to load signals:', e);
        showToast(e.message, 'error', 'Signals Load Failed');
    }
}

// ─── Charts ───
function renderEquityChart(curve) {
    const ctx = document.getElementById('equity-chart')?.getContext('2d');
    if (!ctx) return;

    if (equityChart) equityChart.destroy();

    const labels = curve.map((_, i) => `#${i + 1}`);
    const data = curve.map(c => c.cumulative_pnl);

    const gradient = ctx.createLinearGradient(0, 0, 0, 280);
    gradient.addColorStop(0, 'rgba(59, 130, 246, 0.3)');
    gradient.addColorStop(1, 'rgba(59, 130, 246, 0.0)');

    equityChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: 'Cumulative P&L %',
                data,
                borderColor: '#3b82f6',
                backgroundColor: gradient,
                borderWidth: 2,
                fill: true,
                tension: 0.4,
                pointRadius: 0,
                pointHoverRadius: 5,
            }]
        },
        options: chartOptions('P&L %')
    });
}

function renderDailyPnlChart(daily) {
    const ctx = document.getElementById('daily-pnl-chart')?.getContext('2d');
    if (!ctx) return;

    if (dailyPnlChart) dailyPnlChart.destroy();

    dailyPnlChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: daily.map(d => d.date),
            datasets: [{
                label: 'Daily P&L %',
                data: daily.map(d => d.pnl),
                backgroundColor: daily.map(d => d.pnl >= 0
                    ? 'rgba(16, 185, 129, 0.7)'
                    : 'rgba(239, 68, 68, 0.7)'
                ),
                borderRadius: 4,
            }]
        },
        options: chartOptions('P&L %')
    });
}

function renderWinLossChart(perf) {
    const ctx = document.getElementById('winloss-chart')?.getContext('2d');
    if (!ctx) return;

    if (winlossChart) winlossChart.destroy();

    winlossChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['Wins', 'Losses', 'Breakeven'],
            datasets: [{
                data: [
                    perf.winning_trades || 0,
                    perf.losing_trades || 0,
                    perf.breakeven_trades || 0
                ],
                backgroundColor: ['#10b981', '#ef4444', '#6b7280'],
                borderColor: 'transparent',
                borderWidth: 0,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: { color: '#9ca3af', padding: 16, font: { size: 12 } }
                }
            },
            cutout: '65%',
        }
    });
}

function chartOptions(yLabel) {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: 'index' },
        plugins: {
            legend: { display: false },
            tooltip: {
                backgroundColor: '#1a1f2e',
                borderColor: '#2a3042',
                borderWidth: 1,
                titleColor: '#e8eaed',
                bodyColor: '#9ca3af',
                cornerRadius: 8,
                padding: 12,
            }
        },
        scales: {
            x: {
                grid: { color: 'rgba(42, 48, 66, 0.5)' },
                ticks: { color: '#6b7280', font: { size: 11 }, maxTicksLimit: 12 }
            },
            y: {
                grid: { color: 'rgba(42, 48, 66, 0.5)' },
                ticks: { color: '#6b7280', font: { size: 11 } },
                title: { display: true, text: yLabel, color: '#6b7280' }
            }
        }
    };
}

function setChartPeriod(evt, days) {
    document.querySelectorAll('.card-actions .btn-sm').forEach(b => b.classList.remove('active'));
    evt.target.classList.add('active');
    fetchAPI(`/api/performance?days=${days}`).then(perf => {
        renderEquityChart(perf.equity_curve || []);
    }).catch(err => showToast(err.message, 'error'));
}

// ─── Positions ───
async function loadPositions() {
    try {
        const [positions, balance] = await Promise.all([
            fetchAPI('/api/positions'),
            fetchAPI('/balance')
        ]);

        // Positions table
        const tbody = document.getElementById('positions-body');
        if (!positions.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No open positions</td></tr>';
        } else {
            tbody.innerHTML = positions.map(p => `
                <tr>
                    <td><strong>${p.symbol}</strong></td>
                    <td><span class="badge ${p.side === 'long' ? 'badge-long' : 'badge-short'}">${p.side}</span></td>
                    <td>${p.contracts}</td>
                    <td>$${formatNum(p.entry_price)}</td>
                    <td>$${formatNum(p.mark_price)}</td>
                    <td>${p.liquidation_price ? '$' + formatNum(p.liquidation_price) : '--'}</td>
                    <td class="${p.unrealized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">
                        $${formatNum(p.unrealized_pnl)}
                    </td>
                    <td class="${p.percentage >= 0 ? 'pnl-positive' : 'pnl-negative'}">
                        ${p.percentage >= 0 ? '+' : ''}${p.percentage.toFixed(2)}%
                    </td>
                    <td>${p.leverage}x</td>
                </tr>
            `).join('');
        }

        // Balance
        document.getElementById('bal-total').textContent = `$${formatNum(balance.total || 0)}`;
        document.getElementById('bal-free').textContent = `$${formatNum(balance.free || 0)}`;
        document.getElementById('bal-used').textContent = `$${formatNum(balance.used || 0)}`;

    } catch (err) {
        console.error('Positions load error:', err);
        showToast(err.message, 'error', 'Positions Load Failed');
    }
}

// ─── History ───
async function loadHistory() {
    try {
        const days = document.getElementById('history-days')?.value || 30;
        const trades = await fetchAPI(`/api/history?days=${days}`);
        const tbody = document.getElementById('history-body');

        if (!trades.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No trades found</td></tr>';
            return;
        }

        tbody.innerHTML = trades.reverse().map(t => {
            const dir = t.direction || '--';
            const isLong = dir.includes('long');
            const conf = t.ai?.confidence || 0;
            const status = t.order_status || t.status || '--';
            const pnl = t.pnl_pct || 0;
            const time = t.timestamp ? new Date(t.timestamp).toLocaleString() : '--';

            return `
                <tr>
                    <td>${time}</td>
                    <td><strong>${t.ticker || '--'}</strong></td>
                    <td><span class="badge ${isLong ? 'badge-long' : 'badge-short'}">${dir}</span></td>
                    <td>${t.entry_price ? '$' + formatNum(t.entry_price) : '--'}</td>
                    <td>${t.stop_loss ? '$' + formatNum(t.stop_loss) : '--'}</td>
                    <td>${t.take_profit ? '$' + formatNum(t.take_profit) : '--'}</td>
                    <td>${(conf * 100).toFixed(0)}%</td>
                    <td><span class="badge badge-${status}">${status}</span></td>
                    <td class="${pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">${pnl ? pnl.toFixed(2) + '%' : '--'}</td>
                </tr>
            `;
        }).join('');
    } catch (err) {
        console.error('History load error:', err);
        showToast(err.message, 'error', 'History Load Failed');
    }
}

// ─── Analytics ───
async function loadAnalytics() {
    try {
        const [perf, daily] = await Promise.all([
            fetchAPI('/api/performance?days=30'),
            fetchAPI('/api/daily-pnl?days=30')
        ]);

        // KPIs
        document.getElementById('an-pf').textContent = formatValue(perf.profit_factor);
        document.getElementById('an-dd').textContent = `${(perf.max_drawdown_pct || 0).toFixed(2)}%`;
        document.getElementById('an-rr').textContent = formatValue(perf.risk_reward_ratio);
        document.getElementById('an-sortino').textContent = (perf.sortino_ratio || 0).toFixed(2);

        // Charts
        renderDailyPnlChart(daily);
        renderWinLossChart(perf);

        // Detailed metrics
        const metricsEl = document.getElementById('detailed-metrics');
        const metrics = [
            ['Total P&L', `${(perf.total_pnl_pct || 0).toFixed(2)}%`],
            ['Win Rate', `${(perf.win_rate || 0).toFixed(1)}%`],
            ['Total Trades', perf.total_trades || 0],
            ['Avg Win', `${(perf.avg_win_pct || 0).toFixed(2)}%`],
            ['Avg Loss', `${(perf.avg_loss_pct || 0).toFixed(2)}%`],
            ['Expectancy', `${(perf.expectancy_pct || 0).toFixed(4)}%`],
            ['Sharpe Ratio', (perf.sharpe_ratio || 0).toFixed(2)],
            ['Sortino Ratio', (perf.sortino_ratio || 0).toFixed(2)],
            ['Calmar Ratio', (perf.calmar_ratio || 0).toFixed(2)],
            ['Max Drawdown', `${(perf.max_drawdown_pct || 0).toFixed(2)}%`],
            ['DD Duration', `${perf.max_drawdown_duration_trades || 0} trades`],
            ['Profit Factor', formatValue(perf.profit_factor)],
            ['Best Trade', `${(perf.best_trade_pct || 0).toFixed(2)}%`],
            ['Worst Trade', `${(perf.worst_trade_pct || 0).toFixed(2)}%`],
            ['Gross Profit', `${(perf.gross_profit_pct || 0).toFixed(2)}%`],
            ['Gross Loss', `${(perf.gross_loss_pct || 0).toFixed(2)}%`],
            ['Consec. Wins', perf.max_consecutive_wins || 0],
            ['Consec. Losses', perf.max_consecutive_losses || 0],
        ];

        metricsEl.innerHTML = metrics.map(([label, value]) => `
            <div class="metric-item">
                <span class="metric-label">${label}</span>
                <span class="metric-value">${value}</span>
            </div>
        `).join('');

        // AI stats
        const aiEl = document.getElementById('ai-stats');
        const ai = perf.ai_stats || {};
        aiEl.innerHTML = `
            <div class="ai-stat-card">
                <div class="stat-label">High-Confidence Win Rate</div>
                <div class="stat-value pnl-positive">${(ai.high_confidence_win_rate || 0).toFixed(1)}%</div>
                <div class="hint">${ai.high_confidence_trades || 0} trades (conf ≥ 70%)</div>
            </div>
            <div class="ai-stat-card">
                <div class="stat-label">Low-Confidence Win Rate</div>
                <div class="stat-value pnl-negative">${(ai.low_confidence_win_rate || 0).toFixed(1)}%</div>
                <div class="hint">${ai.low_confidence_trades || 0} trades (conf < 50%)</div>
            </div>
            <div class="ai-stat-card">
                <div class="stat-label">Avg AI Confidence</div>
                <div class="stat-value">${((ai.avg_confidence || 0) * 100).toFixed(1)}%</div>
                <div class="hint">Across all trades</div>
            </div>
            <div class="ai-stat-card">
                <div class="stat-label">AI Edge</div>
                <div class="stat-value ${(ai.high_confidence_win_rate - ai.low_confidence_win_rate) > 0 ? 'pnl-positive' : 'pnl-negative'}">
                    ${((ai.high_confidence_win_rate || 0) - (ai.low_confidence_win_rate || 0)).toFixed(1)}%
                </div>
                <div class="hint">High vs Low confidence gap</div>
            </div>
        `;

    } catch (err) {
        console.error('Analytics load error:', err);
        showToast(err.message, 'error', 'Analytics Load Failed');
    }
}

// ─── Custom AI Provider Functions ───

function toggleCustomAIFields() {
    const provider = document.getElementById('set-ai-provider').value;
    const customFields = document.getElementById('custom-ai-fields');
    
    if (provider === 'custom') {
        customFields.style.display = 'block';
        // Set default values for custom provider
        document.getElementById('set-custom-provider-name').value = 'custom';
        document.getElementById('set-custom-provider-enabled').checked = true;
    } else {
        customFields.style.display = 'none';
    }
}

// ─── AI Settings ───
function setupExchangeToggle() {
    const exchangeSelect = document.getElementById('set-exchange');
    exchangeSelect?.addEventListener('change', () => {
        const val = exchangeSelect.value;
        const passGroup = document.getElementById('password-group');
        passGroup.style.display = ['okx', 'bitget'].includes(val) ? 'block' : 'none';
    });
}

async function loadSettings() {
    try {
        const status = await fetchAPI('/api/status');
        // Display current exchange & AI provider (keys are not sent for security)
        const exchangeEl = document.getElementById('set-exchange');
        if (exchangeEl && status.exchange) exchangeEl.value = status.exchange;
        const aiEl = document.getElementById('set-ai-provider');
        if (aiEl && status.ai_provider) {
            // Check if current provider is a custom provider
            if (status.custom_provider_enabled && status.ai_provider === 'custom') {
                aiEl.value = 'custom';
                // Show custom provider fields
                document.getElementById('custom-ai-fields').style.display = 'block';
                // Set custom provider values
                document.getElementById('set-custom-provider-name').value = status.custom_provider_name || 'custom';
                document.getElementById('set-custom-provider-model').value = status.custom_provider_model || '';
                document.getElementById('set-custom-provider-url').value = status.custom_provider_url || '';
                document.getElementById('set-custom-provider-enabled').checked = true;
            } else {
                aiEl.value = status.ai_provider;
            }
        }

        // Set TP levels from status
        if (status.tp_levels) {
            const tpEl = document.getElementById('set-tp-levels');
            if (tpEl) {
                tpEl.value = status.tp_levels;
                toggleTPLevels();
            }
        }

        // Set trailing stop mode from status
        if (status.trailing_stop_mode) {
            const tsEl = document.getElementById('set-ts-mode');
            if (tsEl) {
                tsEl.value = status.trailing_stop_mode;
                toggleTSFields();
            }
        }
    } catch (e) {
        console.error('Settings load error:', e);
    }
}

async function testConnection() {
    const btn = document.getElementById('btn-test-conn');
    const result = document.getElementById('conn-result');
    btn.disabled = true;
    btn.innerHTML = '<i class="ri-loader-4-line"></i> Testing...';

    try {
        const resp = await fetchAPI('/api/test-connection', {
            method: 'POST',
            body: JSON.stringify({
                exchange: document.getElementById('set-exchange').value,
                api_key: document.getElementById('set-api-key').value,
                api_secret: document.getElementById('set-api-secret').value,
                password: document.getElementById('set-password').value,
            })
        });

        result.className = `conn-result ${resp.success ? 'success' : 'error'}`;
        result.textContent = resp.message;
    } catch (e) {
        result.className = 'conn-result error';
        result.textContent = `Connection failed: ${e.message}`;
        showToast(e.message, 'error', 'Connection Failed');
    }

    btn.disabled = false;
    btn.innerHTML = '<i class="ri-link"></i> Test Connection';
}

async function saveExchangeSettings() {
    await saveSettings('/api/settings/exchange', {
        exchange: document.getElementById('set-exchange').value,
        api_key: document.getElementById('set-api-key').value,
        api_secret: document.getElementById('set-api-secret').value,
        password: document.getElementById('set-password').value,
    }, 'btn-save-exchange');
}

async function saveAISettings() {
    const provider = document.getElementById('set-ai-provider').value;
    const data = {
        provider: provider,
        api_key: document.getElementById('set-ai-key').value,
        temperature: parseFloat(document.getElementById('set-ai-temp').value) || 0.3,
        max_tokens: parseInt(document.getElementById('set-ai-tokens').value) || 1000,
        custom_system_prompt: document.getElementById('set-ai-prompt').value || '',
    };
    
    // Add custom provider fields if provider is 'custom'
    if (provider === 'custom') {
        data.custom_provider_enabled = document.getElementById('set-custom-provider-enabled').checked;
        data.custom_provider_name = document.getElementById('set-custom-provider-name').value || 'custom';
        data.custom_provider_model = document.getElementById('set-custom-provider-model').value || '';
        data.custom_provider_api_url = document.getElementById('set-custom-provider-url').value || '';
    }
    
    await saveSettings('/api/settings/ai', data, 'btn-save-ai');
}

async function saveTelegramSettings() {
    await saveSettings('/api/settings/telegram', {
        bot_token: document.getElementById('set-tg-token').value,
        chat_id: document.getElementById('set-tg-chat').value,
    });
}

async function saveRiskSettings() {
    await saveSettings('/api/settings/risk', {
        max_position_pct: parseFloat(document.getElementById('set-max-pos').value),
        max_daily_trades: parseInt(document.getElementById('set-max-trades').value),
        max_daily_loss_pct: parseFloat(document.getElementById('set-max-loss').value),
    });
}

// ─── Take-Profit Settings ───

function toggleTPLevels() {
    const num = parseInt(document.getElementById('set-tp-levels').value) || 1;
    for (let i = 1; i <= 4; i++) {
        const row = document.getElementById(`tp-row-${i}`);
        if (row) {
            row.style.display = i <= num ? 'block' : 'none';
        }
    }
}

async function saveTPSettings() {
    const data = {
        num_levels: parseInt(document.getElementById('set-tp-levels').value) || 1,
        tp1_pct: parseFloat(document.getElementById('set-tp1-pct').value) || 2.0,
        tp2_pct: parseFloat(document.getElementById('set-tp2-pct').value) || 4.0,
        tp3_pct: parseFloat(document.getElementById('set-tp3-pct').value) || 6.0,
        tp4_pct: parseFloat(document.getElementById('set-tp4-pct').value) || 10.0,
        tp1_qty: parseFloat(document.getElementById('set-tp1-qty').value) || 25.0,
        tp2_qty: parseFloat(document.getElementById('set-tp2-qty').value) || 25.0,
        tp3_qty: parseFloat(document.getElementById('set-tp3-qty').value) || 25.0,
        tp4_qty: parseFloat(document.getElementById('set-tp4-qty').value) || 25.0,
    };

    // Validate total qty doesn't exceed 100%
    const totalQty = [data.tp1_qty, data.tp2_qty, data.tp3_qty, data.tp4_qty]
        .slice(0, data.num_levels)
        .reduce((a, b) => a + b, 0);

    if (totalQty > 100) {
        showToast(`Total close % is ${totalQty}%. Must be ≤ 100%.`, 'warning', 'Invalid TP Config');
        return;
    }

    await saveSettings('/api/settings/take-profit', data);
    showToast(`${data.num_levels} TP levels saved.`, 'success', 'Take-Profit Updated');
}

// ─── Trailing Stop Settings ───

function toggleTSFields() {
    const mode = document.getElementById('set-ts-mode').value;
    const movingFields = document.getElementById('ts-moving-fields');
    const profitFields = document.getElementById('ts-profit-fields');
    const descEl = document.getElementById('ts-description');
    const descText = document.getElementById('ts-description-text');

    // Hide all
    movingFields.style.display = 'none';
    profitFields.style.display = 'none';
    descEl.style.display = 'none';

    const descriptions = {
        none: '',
        moving: 'The stop-loss will trail behind the price by the specified percentage. As price moves in your favour, the SL moves up (for longs) or down (for shorts).',
        breakeven_on_tp1: 'When TP1 is reached, the stop-loss will automatically move to the entry price (breakeven). This protects your capital after the first profit target is hit.',
        step_trailing: 'As each TP level is reached, the SL moves to the previous TP price. e.g., When TP2 is hit → SL moves to TP1. When TP3 is hit → SL moves to TP2.',
        profit_pct_trailing: 'The trailing stop activates only after unrealized profit reaches the activation threshold. Once activated, it trails by the step percentage.',
    };

    if (mode === 'moving') {
        movingFields.style.display = 'block';
    } else if (mode === 'profit_pct_trailing') {
        profitFields.style.display = 'block';
    }

    if (descriptions[mode]) {
        descText.textContent = descriptions[mode];
        descEl.style.display = 'flex';
    }
}

async function saveTSSettings() {
    const data = {
        mode: document.getElementById('set-ts-mode').value,
        trail_pct: parseFloat(document.getElementById('set-ts-trail-pct').value) || 1.0,
        activation_profit_pct: parseFloat(document.getElementById('set-ts-activation').value) || 1.0,
        trailing_step_pct: parseFloat(document.getElementById('set-ts-step').value) || 0.5,
    };

    await saveSettings('/api/settings/trailing-stop', data);
    showToast(`Trailing stop mode: ${data.mode}`, 'success', 'Trailing Stop Updated');
}

// ─── Telegram ───

async function testTelegram() {
    try {
        await fetchAPI('/api/test-telegram', { method: 'POST' });
        showToast('Check your Telegram for the test message.', 'success', 'Test Sent');
    } catch (e) {
        showToast(e.message, 'error', 'Test Failed');
    }
}

async function saveSettings(endpoint, data, btnId) {
    const btn = btnId ? document.getElementById(btnId) : null;
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ri-loader-4-line"></i> Saving...'; }

    try {
        await fetchAPI(endpoint, { method: 'POST', body: JSON.stringify(data) });
        showToast('Settings saved successfully.', 'success', 'Saved');
        if (btn) { btn.innerHTML = '<i class="ri-check-line"></i> Saved!'; }
        setTimeout(() => {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ri-save-line"></i> Save'; }
        }, 2000);
    } catch (e) {
        showToast(e.message, 'error', 'Save Failed');
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ri-save-line"></i> Save'; }
    }
}

function detectWebhookUrl() {
    const url = `${window.location.origin}/webhook`;
    const el = document.getElementById('webhook-url');
    if (el) el.textContent = url;
}

function copyWebhookUrl(evt) {
    const url = document.getElementById('webhook-url')?.textContent;
    if (url) {
        navigator.clipboard.writeText(url).then(() => {
            showToast(url, 'success', 'Webhook URL copied');
        }).catch(() => showToast('Could not access clipboard.', 'warning'));
        // Brief visual feedback on the button
        const btn = evt?.target?.closest('.btn');
        if (btn) {
            btn.innerHTML = '<i class="ri-check-line"></i>';
            setTimeout(() => { btn.innerHTML = '<i class="ri-file-copy-line"></i>'; }, 1500);
        }
    }
}

// ─── Helpers ───
async function fetchAPI(path, options = {}) {
    const resp = await fetch(`${API}${path}`, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    if (!resp.ok) throw new Error(`API error: ${resp.status}`);
    return resp.json();
}

function formatNum(n) {
    if (n === null || n === undefined) return '--';
    return Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatValue(v) {
    if (v === '∞' || v === Infinity) return '∞';
    if (typeof v === 'number') return v.toFixed(2);
    return v || '--';
}

async function refreshAll() {
    const activePage = document.querySelector('.page.active')?.id?.replace('page-', '');
    if (activePage === 'dashboard') await loadDashboard();
    else if (activePage === 'positions') await loadPositions();
    else if (activePage === 'history') await loadHistory();
    else if (activePage === 'analytics') await loadAnalytics();
}
