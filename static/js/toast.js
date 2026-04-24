/**
 * QuantPilot AI - Toast Notifications
 * Toast notification system.
 */

// Toast icons
const TOAST_ICONS = {
    success: 'ri-checkbox-circle-line',
    error: 'ri-error-warning-line',
    warning: 'ri-alert-line',
    info: 'ri-information-line',
};

// Default titles
const TOAST_TITLES = {
    success: 'Success',
    error: 'Error',
    warning: 'Warning',
    info: 'Info',
};

/**
 * Show a toast notification
 */
function showToast(message, type = 'info', title = '') {
    const container = document.getElementById('toast-container');
    if (!container) {
        console.warn('Toast container not found');
        return;
    }

    const safeTitle = escapeHtml(title || TOAST_TITLES[type] || 'Notice');
    const safeMessage = message ? escapeHtml(message) : '';
    const icon = TOAST_ICONS[type] || TOAST_ICONS.info;

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

    // Auto dismiss after 4 seconds
    const dismiss = () => {
        toast.classList.add('removing');
        toast.addEventListener('animationend', () => toast.remove(), { once: true });
    };

    setTimeout(dismiss, 4000);
    toast.addEventListener('click', dismiss);
}

/**
 * Show success toast
 */
function showSuccess(message, title = 'Success') {
    showToast(message, 'success', title);
}

/**
 * Show error toast
 */
function showError(message, title = 'Error') {
    showToast(message, 'error', title);
}

/**
 * Show warning toast
 */
function showWarning(message, title = 'Warning') {
    showToast(message, 'warning', title);
}

/**
 * Show info toast
 */
function showInfo(message, title = 'Info') {
    showToast(message, 'info', title);
}

/**
 * Clear all toasts
 */
function clearAllToasts() {
    const container = document.getElementById('toast-container');
    if (container) {
        container.innerHTML = '';
    }
}
