/**
 * QuantPilot AI - API Client
 * Centralized API communication with error handling.
 */

const API_BASE = '';

class APIClient {
    constructor() {
        this.baseURL = API_BASE;
    }

    /**
     * Make an API request
     */
    async request(endpoint, options = {}) {
        const url = `${this.baseURL}${endpoint}`;
        const config = {
            credentials: 'include',
            cache: 'no-store',
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...options.headers,
            },
        };

        // Add CSRF token for non-GET requests
        if (config.method && config.method !== 'GET') {
            const csrfToken = this.getCSRFToken();
            if (csrfToken) {
                config.headers['X-CSRF-Token'] = csrfToken;
            }
        }

        try {
            const response = await fetch(url, config);

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
