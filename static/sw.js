// QuantPilot AI Service Worker for PWA
// Keep this in sync with versioned asset URLs in the HTML entrypoints.
const CACHE_VERSION = '20260504-2';
const CACHE_NAME = `quantpilot-${CACHE_VERSION}`;
const STATIC_CACHE = `quantpilot-static-${CACHE_VERSION}`;
const NETWORK_FIRST_PATHS = new Set([
  '/static/style.css',
  '/static/app.js',
  '/static/js/qp-core.js',
  '/static/js/charts.js',
  '/static/js/i18n.js',
  '/static/manifest.json',
]);

const STATIC_ASSETS = [
  '/static/style.css?v=20260504-2',
  '/static/app.js?v=20260504-2',
  '/static/js/qp-core.js?v=20260504-2',
  '/static/js/charts.js?v=20260504-2',
  '/static/js/i18n.js?v=20260504-2',
  '/static/manifest.json?v=20260504-2',
  '/static/icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    Promise.all([
      caches.open(STATIC_CACHE).then((cache) => cache.addAll(STATIC_ASSETS)),
    ]).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.filter((name) => !name.includes(CACHE_VERSION)).map((name) => caches.delete(name))
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  if (url.pathname.startsWith('/ws/') || url.pathname.startsWith('/websocket')) {
    return;
  }

  if (event.request.method !== 'GET') {
    event.respondWith(fetch(event.request));
    return;
  }

  if (shouldUseNetworkFirst(url)) {
    event.respondWith(handleNetworkFirstRequest(event.request));
    return;
  }

  if (!shouldCacheRequest(event.request, url)) {
    event.respondWith(fetch(event.request));
    return;
  }

  event.respondWith(handleStaticRequest(event.request));
});

function shouldCacheRequest(request, url) {
  if (request.method !== 'GET') return false;
  if (request.mode === 'navigate') return false;
  if (url.origin !== self.location.origin) return false;
  if (url.pathname.startsWith('/api/')) return false;

  return STATIC_ASSETS.includes(url.pathname) || url.pathname.startsWith('/static/');
}

function shouldUseNetworkFirst(url) {
  return url.origin === self.location.origin && NETWORK_FIRST_PATHS.has(url.pathname);
}

async function handleNetworkFirstRequest(request) {
  try {
    const networkResponse = await fetch(request);
    if (networkResponse && networkResponse.ok) {
      const responseForCache = networkResponse.clone();
      eventWaitUntilSafe(
        caches.open(STATIC_CACHE).then((cache) => cache.put(request, responseForCache))
      );
    }
    return networkResponse;
  } catch (_) {
    return caches.match(request) || fetch(request);
  }
}

async function handleStaticRequest(request) {
  const cachedResponse = await caches.match(request);

  if (cachedResponse) {
    fetch(request).then((networkResponse) => {
      if (networkResponse && networkResponse.ok) {
        const responseForCache = networkResponse.clone();
        caches.open(STATIC_CACHE).then((cache) => cache.put(request, responseForCache));
      }
    }).catch(() => {});

    return cachedResponse;
  }

  return fetch(request).then((response) => {
    if (response.ok) {
      const responseForCache = response.clone();
      eventWaitUntilSafe(
        caches.open(STATIC_CACHE).then((cache) => cache.put(request, responseForCache))
      );
    }
    return response;
  }).catch(() => {
    return caches.match(request) || caches.match('/');
  });
}

function eventWaitUntilSafe(promise) {
  promise.catch(() => {});
}

self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-trades') {
    event.waitUntil(syncTrades());
  }
});

async function syncTrades() {
  const pendingTrades = await getPendingTrades();

  for (const trade of pendingTrades) {
    try {
      await fetch('/api/user/trades/sync', {
        method: 'POST',
        body: JSON.stringify(trade),
        headers: {
          'Content-Type': 'application/json',
          'X-PWA-Sync': '1'
        }
      });
      await removePendingTrade(trade.id);
    } catch (error) {
      console.error('Failed to sync trade:', trade.id);
    }
  }
}

self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {};

  const options = {
    body: data.body || 'New trading signal received',
    icon: '/static/icon.svg',
    badge: '/static/icon.svg',
    vibrate: [100, 50, 100],
    data: {
      url: data.url || '/dashboard',
      timestamp: Date.now()
    },
    actions: [
      {action: 'view', title: 'View', icon: '/static/icon.svg'},
      {action: 'dismiss', title: 'Dismiss', icon: '/static/icon.svg'}
    ],
    tag: data.tag || 'trading-notification',
    renotify: true
  };

  event.waitUntil(
    self.registration.showNotification(data.title || 'QuantPilot AI', options)
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  if (event.action === 'dismiss') {
    return;
  }

  const url = event.notification.data.url || '/dashboard';

  event.waitUntil(
    clients.matchAll({type: 'window'}).then((clientList) => {
      for (const client of clientList) {
        if (client.url === url && 'focus' in client) {
          return client.focus();
        }
      }

      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});

self.addEventListener('message', (event) => {
  if (event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }

  if (event.data.type === 'CACHE_TRADE') {
    event.waitUntil(cacheTrade(event.data.trade));
  }

  if (event.data.type === 'GET_PENDING_TRADES') {
    event.waitUntil(
      getPendingTrades().then((trades) => {
        event.source.postMessage({type: 'PENDING_TRADES', trades: trades});
      })
    );
  }
});

async function cacheTrade(trade) {
  const db = await openIndexedDB();
  const tx = db.transaction(['pending_trades'], 'readwrite');
  const store = tx.objectStore('pending_trades');
  store.add({...trade, timestamp: Date.now()});
}

async function getPendingTrades() {
  const db = await openIndexedDB();
  const tx = db.transaction(['pending_trades'], 'readonly');
  const store = tx.objectStore('pending_trades');
  return store.getAll();
}

async function removePendingTrade(tradeId) {
  const db = await openIndexedDB();
  const tx = db.transaction(['pending_trades'], 'readwrite');
  const store = tx.objectStore('pending_trades');
  store.delete(tradeId);
}

function openIndexedDB() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('QuantPilotDB', 1);

    request.onerror = () => reject(request.error);
    request.onsuccess = () => resolve(request.result);

    request.onupgradeneeded = (event) => {
      const db = event.target.result;

      if (!db.objectStoreNames.contains('pending_trades')) {
        db.createObjectStore('pending_trades', {keyPath: 'id'});
      }

      if (!db.objectStoreNames.contains('offline_settings')) {
        db.createObjectStore('offline_settings', {keyPath: 'key'});
      }
    };
  });
}

console.log(`[ServiceWorker] QuantPilot AI ${CACHE_VERSION} loaded`);
