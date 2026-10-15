/**
 * Hoodle LMS - Progressive Web App (PWA) Service Worker
 * Handles offline static caching and background web push notifications
 */

const CACHE_NAME = 'hoodle-static-v1';
const STATIC_ASSETS = [
  '/lms/static/css/style.css',
  '/lms/static/js/lms.js',
  '/lms/static/js/pdf/pdf.min.js',
  '/lms/static/js/pdf/pdf.worker.min.js',
  '/lms/static/images/hoodle_icon.png',
  '/lms/static/manifest.json'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(STATIC_ASSETS).catch(() => {});
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      );
    })
  );
  self.clients.claim();
});

// Cache First with Network Fallback for static assets
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Only handle GET requests for static assets
  if (event.request.method === 'GET' && url.pathname.includes('/static/')) {
    event.respondWith(
      caches.match(event.request).then(cached => {
        return cached || fetch(event.request).then(response => {
          if (response && response.status === 200) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
          }
          return response;
        });
      })
    );
  }
});

// Web Push Notification Event Handler
self.addEventListener('push', event => {
  let data = {
    title: 'Hoodle LMS Notification',
    body: 'You have a new update in Hoodle LMS.',
    icon: '/lms/static/images/hoodle_app_icon.png',
    badge: '/lms/static/images/hoodle_icon.png',
    data: { url: '/lms/dashboard' }
  };

  try {
    if (event.data) {
      const payload = event.data.json();
      data = Object.assign(data, payload);
    }
  } catch (e) {
    if (event.data) data.body = event.data.text();
  }

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: data.icon,
      badge: data.badge,
      data: data.data,
      vibrate: [200, 100, 200]
    })
  );
});

// Click on Web Push Notification
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/lms/dashboard';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (let client of windowClients) {
        if (client.url.includes('/lms') && 'focus' in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});
