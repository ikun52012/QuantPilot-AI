/**
 * QuantPilot AI - Core Module Namespace
 * Provides unified namespace to avoid module conflicts.
 */
(function() {
    'use strict';

    const QP = window.QP || {};

    // ─── Security Utilities ───
    QP.escapeHtml = function(str) {
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    };

    QP.escapeJsSingle = function(str) {
        return String(str || '')
            .replace(/\\/g, '\\\\')
            .replace(/'/g, "\\'")
            .replace(/"/g, '&quot;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/\r?\n/g, ' ');
    };

    QP.safeClassToken = function(str) {
        return String(str || '').toLowerCase().replace(/[^a-z0-9_-]/g, '');
    };

    QP.getCookie = function(name) {
        const prefix = `${name}=`;
        return document.cookie
            .split(';')
            .map(v => v.trim())
            .find(v => v.startsWith(prefix))
            ?.slice(prefix.length) || '';
    };

    // ─── Copy Utility ───
    QP.copyText = async function(text, label = 'Copied') {
        try {
            await navigator.clipboard.writeText(text);
            QP.Toast.success(label);
        } catch (err) {
            console.error('Copy failed:', err);
            QP.Toast.error('Copy failed');
        }
    };

    // ─── Format Utilities ───
    QP.formatNum = function(value, decimals = 4) {
        if (value === null || value === undefined || isNaN(value)) return '--';
        return Number(value).toLocaleString(undefined, {
            minimumFractionDigits: 0,
            maximumFractionDigits: decimals,
        });
    };

    QP.formatValue = function(value, fallback = '--') {
        if (value === null || value === undefined) return fallback;
        if (typeof value === 'number' && !isFinite(value)) return fallback;
        return value;
    };

    QP.firstDefined = function(...args) {
        for (const arg of args) {
            if (arg !== null && arg !== undefined) return arg;
        }
        return null;
    };

    QP.pickBalance = function(total, quote) {
        if (total && typeof total === 'object') {
            return total[quote] || total.USDT || 0;
        }
        return 0;
    };

    QP.formatDateTime = function(isoString) {
        if (!isoString) return '--';
        try {
            return new Date(isoString).toLocaleString();
        } catch {
            return isoString;
        }
    };

    QP.formatTime = function(isoString) {
        if (!isoString) return '--';
        try {
            return new Date(isoString).toLocaleTimeString();
        } catch {
            return isoString;
        }
    };

    // ─── DOM Helpers ───
    QP.setText = function(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };

    QP.setFieldValue = function(id, value) {
        const el = document.getElementById(id);
        if (el) el.value = value;
    };

    QP.setSecretPlaceholder = function(id, configured, placeholder) {
        const el = document.getElementById(id);
        if (el) {
            el.placeholder = configured ? '●●●●●●●● (configured)' : placeholder;
            if (configured) el.value = '';
        }
    };

    // ─── Debounce/Throttle ───
    QP.debounce = function(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    };

    QP.throttle = function(func, limit) {
        let inThrottle;
        return function executedFunction(...args) {
            if (!inThrottle) {
                func(...args);
                inThrottle = true;
                setTimeout(() => inThrottle = false, limit);
            }
        };
    };

    // ─── URL Utilities ───
    QP.parseQueryString = function(queryString) {
        const params = new URLSearchParams(queryString);
        const result = {};
        for (const [key, value] of params) {
            result[key] = value;
        }
        return result;
    };

    QP.buildQueryString = function(params) {
        return new URLSearchParams(
            Object.entries(params).filter(([_, v]) => v !== null && v !== undefined)
        ).toString();
    };

    // ─── Async Helpers ───
    QP.sleep = function(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    };

    // ─── Viewport Helpers ───
    QP.isInViewport = function(element) {
        const rect = element.getBoundingClientRect();
        return (
            rect.top >= 0 &&
            rect.left >= 0 &&
            rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
            rect.right <= (window.innerWidth || document.documentElement.clientWidth)
        );
    };

    // ─── ID Generation ───
    QP.generateId = function() {
        return Math.random().toString(36).substring(2, 11);
    };

    // ─── Toast Module ───
    QP.Toast = {
        icons: {
            success: 'ri-checkbox-circle-line',
            error: 'ri-error-warning-line',
            warning: 'ri-alert-line',
            info: 'ri-information-line',
        },
        titles: {
            success: 'Success',
            error: 'Error',
            warning: 'Warning',
            info: 'Info',
        },

        show: function(message, type = 'info', title = '') {
            const container = document.getElementById('toast-container');
            if (!container) {
                console.warn('Toast container not found');
                return;
            }

            const safeTitle = QP.escapeHtml(title || QP.Toast.titles[type] || 'Notice');
            const safeMessage = message ? QP.escapeHtml(message) : '';
            const icon = QP.Toast.icons[type] || QP.Toast.icons.info;

            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            toast.setAttribute('role', 'alert');
            toast.innerHTML = `
                <i class="toast-icon ${icon}"></i>
                <div class="toast-body">
                    <div class="toast-title">${safeTitle}</div>
                    ${safeMessage ? `<div class="toast-msg">${safeMessage}</div>` : ''}
                </div>
            `;

            container.appendChild(toast);

            const dismiss = () => {
                toast.classList.add('removing');
                toast.addEventListener('animationend', () => toast.remove(), { once: true });
            };

            setTimeout(dismiss, 4000);
            toast.addEventListener('click', dismiss);
        },

        success: function(message, title = 'Success') {
            QP.Toast.show(message, 'success', title);
        },

        error: function(message, title = 'Error') {
            QP.Toast.show(message, 'error', title);
        },

        warning: function(message, title = 'Warning') {
            QP.Toast.show(message, 'warning', title);
        },

        info: function(message, title = 'Info') {
            QP.Toast.show(message, 'info', title);
        },

        clearAll: function() {
            const container = document.getElementById('toast-container');
            if (container) {
                container.innerHTML = '';
            }
        }
    };

    // ─── Auth Module ───
    QP.Auth = {
        _cachedUser: null,
        _sessionRedirecting: false,

        ensureUser: async function() {
            if (QP.Auth._cachedUser) return QP.Auth._cachedUser;
            try {
                const r = await fetch('/api/auth/me', {
                    credentials: 'include',
                    cache: 'no-store',
                });
                if (!r.ok) return null;
                QP.Auth._cachedUser = await r.json();
                return QP.Auth._cachedUser;
            } catch {
                return null;
            }
        },

        getUser: function() {
            return QP.Auth._cachedUser || {};
        },

        isAdmin: function() {
            return QP.Auth.getUser().role === 'admin';
        },

        requireAuth: async function() {
            const user = await QP.Auth.ensureUser();
            if (!user) {
                QP.Auth.redirectToLogin('expired');
                return false;
            }
            return true;
        },

        redirectToLogin: function(reason = 'expired') {
            if (QP.Auth._sessionRedirecting) return;
            QP.Auth._sessionRedirecting = true;
            QP.Auth._cachedUser = null;
            const query = reason ? `?${encodeURIComponent(reason)}=1` : '';
            window.location.replace(`/login${query}`);
        },

        logout: async function() {
            try {
                const csrf = QP.getCookie('tvss_csrf');
                await fetch('/api/auth/logout', {
                    method: 'POST',
                    credentials: 'include',
                    headers: csrf ? { 'X-CSRF-Token': decodeURIComponent(csrf) } : {},
                });
            } catch {}
            QP.Auth.redirectToLogin('logout');
        },

        clearUserCache: function() {
            QP.Auth._cachedUser = null;
        },

        updateUserUI: function() {
            const user = QP.Auth.getUser();
            const usernameEl = document.getElementById('user-display-name');
            if (usernameEl) usernameEl.textContent = user.username || 'User';
            const roleEl = document.getElementById('user-role-badge');
            if (roleEl) {
                roleEl.textContent = user.role === 'admin' ? 'Admin' : 'User';
                roleEl.className = `role-badge ${user.role === 'admin' ? 'admin' : 'user'}`;
            }
            document.querySelectorAll('.admin-only').forEach(el => {
                el.style.display = QP.Auth.isAdmin() ? '' : 'none';
            });
            document.querySelectorAll('.user-only').forEach(el => {
                el.style.display = QP.Auth.isAdmin() ? 'none' : '';
            });
            ['dashboard', 'positions', 'history', 'analytics', 'settings'].forEach(page => {
                const el = document.querySelector(`.nav-item[data-page="${page}"]`);
                if (el && !QP.Auth.isAdmin()) el.style.display = 'none';
            });
        }
    };

    // ─── API Client Module ───
    QP.API = {
        BASE: '',

        request: async function(endpoint, options = {}) {
            const url = `${QP.API.BASE}${endpoint}`;
            const config = {
                credentials: 'include',
                cache: 'no-store',
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers,
                },
            };

            if (config.method && config.method !== 'GET') {
                const csrfToken = QP.getCookie('tvss_csrf');
                if (csrfToken) {
                    config.headers['X-CSRF-Token'] = decodeURIComponent(csrfToken);
                }
            }

            try {
                const response = await fetch(url, config);

                if (response.status === 401) {
                    QP.Auth.redirectToLogin('expired');
                    throw new Error('Unauthorized');
                }

                if (!response.ok) {
                    const error = await response.json().catch(() => ({ detail: 'Request failed' }));
                    throw new Error(error.detail || `HTTP ${response.status}`);
                }

                return await response.json();
            } catch (error) {
                console.error(`API Error [${endpoint}]:`, error);
                throw error;
            }
        },

        get: function(endpoint) {
            return QP.API.request(endpoint, { method: 'GET' });
        },

        post: function(endpoint, data) {
            return QP.API.request(endpoint, {
                method: 'POST',
                body: JSON.stringify(data),
            });
        },

        put: function(endpoint, data) {
            return QP.API.request(endpoint, {
                method: 'PUT',
                body: JSON.stringify(data),
            });
        },

        delete: function(endpoint) {
            return QP.API.request(endpoint, { method: 'DELETE' });
        }
    };

    // Convenience function for app.js compatibility
    window.fetchAPI = function(endpoint, options = {}) {
        return QP.API.request(endpoint, options);
    };

    // Export global namespace
    window.QP = QP;

    // Also export individual functions for backward compatibility
    window.escapeHtml = QP.escapeHtml;
    window.escapeJsSingle = QP.escapeJsSingle;
    window.safeClassToken = QP.safeClassToken;
    window.getCookie = QP.getCookie;
    window.copyText = QP.copyText;
    window.formatNum = QP.formatNum;
    window.formatValue = QP.formatValue;
    window.firstDefined = QP.firstDefined;
    window.pickBalance = QP.pickBalance;
    window.formatDateTime = QP.formatDateTime;
    window.formatTime = QP.formatTime;
    window.setText = QP.setText;
    window.setFieldValue = QP.setFieldValue;
    window.setSecretPlaceholder = QP.setSecretPlaceholder;
    window.debounce = QP.debounce;
    window.throttle = QP.throttle;
    window.parseQueryString = QP.parseQueryString;
    window.buildQueryString = QP.buildQueryString;
    window.sleep = QP.sleep;
    window.isInViewport = QP.isInViewport;
    window.generateId = QP.generateId;
    window.showToast = QP.Toast.show;
    window.showSuccess = QP.Toast.success;
    window.showError = QP.Toast.error;
    window.showWarning = QP.Toast.warning;
    window.showInfo = QP.Toast.info;
    window.clearAllToasts = QP.Toast.clearAll;

})();