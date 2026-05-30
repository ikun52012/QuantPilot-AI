/**
 * QuantPilot AI - API Client
 * Centralized API communication with error handling.
 */

const API_BASE = (() => {
    const meta = document.querySelector('meta[name="api-base-url"]');
    if (meta && meta.content) return meta.content;
    try { return window.QUANTPILOT_API_BASE || ''; } catch (_) { return ''; }
})();

class APIClient {
    constructor() {
        this.baseURL = API_BASE;
    }

    /**
     * Make an API request
     */
    async request(endpoint, options = {}) {
        const url = `${this.baseURL}${endpoint}`;
        const timeout = options.timeout || 15000; // default 15s timeout
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout);

        const config = {
            credentials: 'include',
            cache: 'no-store',
            ...options,
            signal: controller.signal,
            headers: {
                'Content-Type': 'application/json',
                ...options.headers,
            },
        };
        delete config.timeout;

        // Add CSRF token for non-GET requests
        if (config.method && config.method !== 'GET') {
            const csrfToken = this.getCSRFToken();
            if (csrfToken) {
                config.headers['X-CSRF-Token'] = csrfToken;
            }
        }

        try {
            const response = await fetch(url, config);
            clearTimeout(timer);

            if (response.status === 401) {
                // Redirect to login on auth error
                window.location.replace('/login');
                throw new Error('Unauthorized');
            }

            if (!response.ok) {
                const error = await response.json().catch(() => ({ detail: 'Request failed' }));
                throw new Error(error.detail || `HTTP ${response.status}`);
            }

            return await response.json();
        } catch (error) {
            clearTimeout(timer);
            if (error.name === 'AbortError') {
                throw new Error(`Request timeout after ${timeout}ms: ${endpoint}`);
            }
            console.error(`API Error [${endpoint}]:`, error);
            throw error;
        }
    }

    /**
     * GET request
     */
    async get(endpoint) {
        return this.request(endpoint, { method: 'GET' });
    }

    /**
     * POST request
     */
    async post(endpoint, data) {
        return this.request(endpoint, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /**
     * PUT request
     */
    async put(endpoint, data) {
        return this.request(endpoint, {
            method: 'PUT',
            body: JSON.stringify(data),
        });
    }

    /**
     * DELETE request
     */
    async delete(endpoint) {
        return this.request(endpoint, { method: 'DELETE' });
    }

    /**
     * Get CSRF token from cookie
     */
    getCSRFToken() {
        const name = 'tvss_csrf=';
        const cookies = document.cookie.split(';');
        for (let cookie of cookies) {
            cookie = cookie.trim();
            if (cookie.startsWith(name)) {
                return cookie.substring(name.length);
            }
        }
        return '';
    }
}

// Create singleton instance
const api = new APIClient();

// Export convenience functions
async function fetchAPI(endpoint, options = {}) {
    return api.request(endpoint, options);
}
