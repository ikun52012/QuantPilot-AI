/**
 * Signal Server - Utility Functions
 * Common helper functions for the frontend.
 */

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/**
 * Escape for JavaScript single-quoted strings
 */
function escapeJsSingle(str) {
    return String(str || '')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\r?\n/g, ' ');
}

/**
 * Sanitize class token
 */
function safeClassToken(str) {
    return String(str || '').toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

/**
 * Get cookie value
 */
function getCookie(name) {
    const prefix = `${name}=`;
    return document.cookie
        .split(';')
        .map(v => v.trim())
        .find(v => v.startsWith(prefix))
        ?.slice(prefix.length) || '';
}

/**
 * Copy text to clipboard
 */
async function copyText(text, label = 'Copied') {
    try {
        await navigator.clipboard.writeText(text);
        showToast(label, 'success');
    } catch (err) {
        console.error('Copy failed:', err);
        showToast('Copy failed', 'error');
    }
}

/**
 * Format number with commas
 */
function formatNum(value, decimals = 4) {
    if (value === null || value === undefined || isNaN(value)) return '--';
    return Number(value).toLocaleString(undefined, {
        minimumFractionDigits: 0,
        maximumFractionDigits: decimals,
    });
}

/**
 * Format value with fallback
 */
function formatValue(value, fallback = '--') {
    if (value === null || value === undefined) return fallback;
    if (typeof value === 'number' && !isFinite(value)) return fallback;
    return value;
}

/**
 * Get first defined value
 */
function firstDefined(...args) {
    for (const arg of args) {
        if (arg !== null && arg !== undefined) return arg;
    }
    return null;
}

/**
 * Pick balance from object
 */
function pickBalance(total, quote) {
    if (total && typeof total === 'object') {
        return total[quote] || total.USDT || 0;
    }
    return 0;
}

/**
 * Format date/time
 */
function formatDateTime(isoString) {
    if (!isoString) return '--';
    try {
        return new Date(isoString).toLocaleString();
    } catch {
        return isoString;
    }
}

/**
 * Format time only
 */
function formatTime(isoString) {
    if (!isoString) return '--';
    try {
        return new Date(isoString).toLocaleTimeString();
    } catch {
        return isoString;
    }
}

/**
 * Debounce function
 */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Throttle function
 */
function throttle(func, limit) {
    let inThrottle;
    return function executedFunction(...args) {
        if (!inThrottle) {
            func(...args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
}

/**
 * Parse query string
 */
function parseQueryString(queryString) {
    const params = new URLSearchParams(queryString);
    const result = {};
    for (const [key, value] of params) {
        result[key] = value;
    }
    return result;
}

/**
 * Build query string
 */
function buildQueryString(params) {
    return new URLSearchParams(
        Object.entries(params).filter(([_, v]) => v !== null && v !== undefined)
    ).toString();
}

/**
 * Sleep/delay
 */
function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Check if element is in viewport
 */
function isInViewport(element) {
    const rect = element.getBoundingClientRect();
    return (
        rect.top >= 0 &&
        rect.left >= 0 &&
        rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
        rect.right <= (window.innerWidth || document.documentElement.clientWidth)
    );
}

/**
 * Generate unique ID
 */
function generateId() {
    return Math.random().toString(36).substring(2, 11);
}
