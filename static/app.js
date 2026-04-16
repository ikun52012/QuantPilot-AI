/**
 * Signal Server - Dashboard Frontend Logic
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

// ─── Navigation ───
function setupNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const page = item.dataset.page;
            switchPage(page);
        });
    });

    document.getElementById('menu-toggle')?.addEventListener('click', () => {
        document.getElementById('sidebar').classList.toggle('open');
    });
}

function switchPage(page) {
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelector(`[data-page="${page}"]`)?.classList.add('active');
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
            fetchAPI('/'),
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

function setChartPeriod(days) {
    document.querySelectorAll('.card-actions .btn-sm').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
    fetchAPI(`/api/performance?days=${days}`).then(perf => {
        renderEquityChart(perf.equity_curve || []);
    });
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
    }
}

// ─── Settings ───
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
        const status = await fetchAPI('/');
        // Set current values (we don't expose keys for security)
        // Just display current provider info
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
    await saveSettings('/api/settings/ai', {
        provider: document.getElementById('set-ai-provider').value,
        api_key: document.getElementById('set-ai-key').value,
    }, 'btn-save-ai');
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

async function testTelegram() {
    try {
        await fetchAPI('/api/test-telegram', { method: 'POST' });
        alert('Test message sent! Check your Telegram.');
    } catch (e) {
        alert(`Failed: ${e.message}`);
    }
}

async function saveSettings(endpoint, data, btnId) {
    const btn = btnId ? document.getElementById(btnId) : null;
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ri-loader-4-line"></i> Saving...'; }

    try {
        await fetchAPI(endpoint, { method: 'POST', body: JSON.stringify(data) });
        if (btn) { btn.innerHTML = '<i class="ri-check-line"></i> Saved!'; }
        setTimeout(() => {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ri-save-line"></i> Save'; }
        }, 2000);
    } catch (e) {
        alert(`Save failed: ${e.message}`);
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ri-save-line"></i> Save'; }
    }
}

function detectWebhookUrl() {
    const url = `${window.location.origin}/webhook`;
    const el = document.getElementById('webhook-url');
    if (el) el.textContent = url;
}

function copyWebhookUrl() {
    const url = document.getElementById('webhook-url')?.textContent;
    if (url) {
        navigator.clipboard.writeText(url);
        // Brief feedback
        const btn = event.target.closest('.btn');
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
