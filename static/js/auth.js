/**
 * QuantPilot AI - Auth Module
 * Authentication state management.
 */

// Cached user data
let _cachedUser = null;

/**
 * Ensure user is loaded
 */
async function ensureUser() {
    if (_cachedUser) return _cachedUser;

    try {
        const r = await fetch('/api/auth/me', {
            credentials: 'include',
            cache: 'no-store',
        });
        if (!r.ok) return null;
        _cachedUser = await r.json();
        return _cachedUser;
    } catch {
        return null;
    }
}

/**
 * Get current user
 */
function getUser() {
    return _cachedUser || {};
}

/**
 * Check if user is admin
 */
function isAdmin() {
    return getUser().role === 'admin';
}

/**
 * Require authentication (redirect if not logged in)
 */
async function requireAuth() {
    const user = await ensureUser();
    if (!user) {
        window.location.replace('/login');
        return false;
    }
    return true;
}

/**
 * Logout user
 */
async function logout() {
    try {
        const csrf = getCookie('tvss_csrf');
        await fetch('/api/auth/logout', {
            method: 'POST',
            credentials: 'include',
            headers: csrf ? { 'X-CSRF-Token': decodeURIComponent(csrf) } : {},
        });
    } catch {}

    _cachedUser = null;
    window.location.replace('/login');
}

function getCookie(name) {
    const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return match ? match[2] : null;
}

/**
 * Clear cached user
 */
function clearUserCache() {
    _cachedUser = null;
}

/**
 * Update user UI elements
 */
function updateUserUI() {
    const user = getUser();

    // Update username display
    const usernameEl = document.getElementById('user-display-name');
    if (usernameEl) {
        usernameEl.textContent = user.username || 'User';
    }

    // Update role badge
    const roleEl = document.getElementById('user-role-badge');
    if (roleEl) {
        roleEl.textContent = user.role === 'admin' ? 'Admin' : 'User';
        roleEl.className = `role-badge ${user.role === 'admin' ? 'admin' : 'user'}`;
    }

    // Show/hide admin-only elements
    document.querySelectorAll('.admin-only').forEach(el => {
        el.style.display = isAdmin() ? '' : 'none';
    });

    document.querySelectorAll('.user-only').forEach(el => {
        el.style.display = isAdmin() ? 'none' : '';
    });

    // Hide admin nav items for non-admins
    ['dashboard', 'positions', 'history', 'analytics', 'settings'].forEach(page => {
        const el = document.querySelector(`.nav-item[data-page="${page}"]`);
        if (el && !isAdmin()) {
            el.style.display = 'none';
        }
    });
}
