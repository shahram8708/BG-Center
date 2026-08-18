/* BG Command Centre service worker.
 *
 * Offline strategy is differentiated by page category:
 *   - Fully static pages (About / AI Disclaimer / Privacy / Terms): cache-first.
 *   - Read-only informational pages the user has visited (Dashboard, BG Status
 *     Hub, Bank Tracker, BG Detail & Timeline, Notifications, Audit Log):
 *     network-first with cache fallback. The app shows an honest "cached data"
 *     banner when serving from cache while offline.
 *   - Workflow-changing pages/actions: never cached for offline submission.
 *     The app disables those submits offline.
 * Static assets use stale-while-revalidate.
 */
const VERSION = "bgcc-v9";
const STATIC_CACHE = `${VERSION}-static`;
const PAGE_CACHE = `${VERSION}-pages`;

const PRECACHE_STATIC = [
  "/static/manifest.json",
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon.svg"
];

// Fully static, never-changing pages - cache aggressively (cache-first).
const STATIC_PAGES = [
  "/about",
  "/legal/disclaimer",
  "/legal/privacy",
  "/legal/terms"
];

// Read-only informational pages - network-first, cache fallback.
const READ_ONLY_PAGE_PREFIXES = [
  "/dashboard",
  "/bg-status",
  "/bg-bank-tracker",
  "/bg/",
  "/notifications",
  "/admin/audit-log"
];

function isStaticPage(path) {
  return STATIC_PAGES.some((p) => path === p || path === p + "/");
}

function isReadOnlyPage(path) {
  return READ_ONLY_PAGE_PREFIXES.some((p) => path === p || path.startsWith(p));
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(PRECACHE_STATIC))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key.indexOf(VERSION) !== 0).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

async function cacheFirst(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const responseForCache = response.clone();
      cache.put(request, responseForCache);
    }
    return response;
  } catch (e) {
    return new Response("", { status: 504, statusText: "Offline" });
  }
}

async function networkFirstWithPageCache(request) {
  const cache = await caches.open(PAGE_CACHE);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const responseForCache = response.clone();
      cache.put(request, responseForCache);
    }
    return response;
  } catch (e) {
    const cached = await cache.match(request);
    if (cached) return cached;
    return new Response("", { status: 504, statusText: "Offline" });
  }
}

async function networkOnlyWithOfflineFallback(request) {
  try {
    const response = await fetch(request);
    if (response && response.ok) return response;
    // For failed navigations with nothing cached, fall back to /offline.
    if (request.mode === "navigate") {
      const offline = await caches.match("/offline");
      if (offline) return offline;
    }
    return response;
  } catch (e) {
    if (request.mode === "navigate") {
      const offline = await caches.match("/offline");
      if (offline) return offline;
    }
    return new Response("", { status: 504, statusText: "Offline" });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);

  const fetchPromise = (async () => {
    try {
      const response = await fetch(request);
      if (response && response.ok) {
        const responseForCache = response.clone();
        cache.put(request, responseForCache);
      }
      return response;
    } catch (e) {
      return cached || new Response("", { status: 504, statusText: "Offline" });
    }
  })();

  return cached || fetchPromise;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;

  // Static assets: stale-while-revalidate.
  if (path.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }

  // Fully static pages: cache-first.
  if (isStaticPage(path)) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // Read-only informational pages: network-first, cache fallback.
  if (isReadOnlyPage(path) && request.mode === "navigate") {
    event.respondWith(networkFirstWithPageCache(request));
    return;
  }

  // Everything else (including workflow pages): network-only with offline
  // fallback for navigations. Workflow actions are never cached.
  if (request.mode === "navigate") {
    event.respondWith(networkOnlyWithOfflineFallback(request));
    return;
  }
});

/* ---- Push notifications ---- */
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) { /* ignore */ }
  const options = {
    body: data.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    data: { url: data.url || "/" }
  };
  event.waitUntil(self.registration.showNotification(data.title || "BG Command Centre", options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data && event.notification.data.url ? event.notification.data.url : "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if (client.url === url && "focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
