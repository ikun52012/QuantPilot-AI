// QuantPilot AI Service Worker for PWA
const CACHE_NAME = 'quantpilot-v4.5';
const STATIC_CACHE = 'quantpilot-static-v4.5';
const API_CACHE = 'quantpilot-api-v4.5';

const STATIC_ASSETS = [
  '/',
  '/dashboard',
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.json',
  '/static/icon.svg',
];

const API_ENDPOINTS = [
  '/health',
  '/api/user/settings',
  '/api/positions',
  '/api/trades',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    Promise.all([
      caches.open(STATIC_CACHE).then((cache) => cache.addAll(STATIC_ASSETS)),
      caches.open(API_CACHE)
    ]).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.filter((name) => !name.includes('v4.5')).map((name) => caches.delete(name))
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  if (url.pathname.startsWith('/ws/') || url.pathname.startsWith('/websocket')) {
    return;
  }

  if (url.pathname.startsWith('/api/')) {
    event.respondWith(handleApiRequest(event.request));
    return;
  }

  if (event.request.method === 'GET') {
    event.respondWith(handleStaticRequest(event.request));
    return;
  }

  event.respondWith(fetch(event.request));
});

async function handleStaticRequest(request) {
  const cachedResponse = await caches.match(request);

  if (cachedResponse) {
    const networkResponse = fetch(request).then((response) => {
      if (response.ok) {
        caches.open(STATIC_CACHE).then((cache) => cache.put(request, response.clone()));
      }
      return response;
    }).catch(() => cachedResponse);

    return Promise.race([networkResponse, cachedResponse]);
  }

  return fetch(request).then((response) => {
    if (response.ok) {
      caches.open(STATIC_CACHE).then((cache) => cache.put(request, response.clone()));
    }
    return response;
  }).catch(() => {
    return caches.match(request) || caches.match('/');
  });
}

async function handleApiRequest(request) {
  const url = new URL(request.url);

  if (request.method !== 'GET') {
    return fetch(request);
  }

  if (url.pathname.includes('/realtime') || url.pathname.includes('/live')) {
    return fetch(request);
  }

  const cachedResponse = await caches.match(request);

  if (cachedResponse) {
    const cacheTime = cachedResponse.headers.get('sw-cache-time');
    const now = Date.now();
    const maxAge = 30 * 1000;

    if (cacheTime && now - parseInt(cacheTime) < maxAge) {
      return cachedResponse;
    }
  }

  return fetch(request).then((response) => {
    if (response.ok) {
      const headers = new Headers(response.headers);
      headers.set('sw-cache-time', Date.now().toString());

      const cachedResponse = response.clone();
      const modifiedResponse = new Response(cachedResponse.body, {
        status: cachedResponse.status,
        statusText: cachedResponse.statusText,
        headers: headers
      });

      caches.open(API_CACHE).then((cache) => cache.put(request, modifiedResponse));
    }
    return response;
  }).catch(() => {
    return cachedResponse || new Response(JSON.stringify({error: 'Network unavailable', cached: true}), {
      status: 503,
      headers: {'Content-Type': 'application/json'}
    });
  });
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
        headers: {'Content-Type': 'application/json'}
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

console.log('[ServiceWorker] QuantPilot AI v4.5 loaded');
