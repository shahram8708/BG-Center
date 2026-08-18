/* BG Command Centre service worker.
 *
 * Production-ready offline-first caching and Web Push notification handler.
 * - Static assets & fallback shell: Cache-first with background revalidation.
 * - Dynamic HTML / GET pages: Network-first with cache fallback, so online
 *   refreshes always display fresh data while retaining immediate offline availability.
 * - Offline navigation fallback: Renders the dedicated /offline page when neither
 *   network nor cache is available.
 * - Safe versioned cache invalidation and immediate claim without breaking active sessions.
 * - Full Web Push notification and background click management.
 */

const VERSION = "bgcc-v10";
const STATIC_CACHE = `${VERSION}-static`;
const PAGE_CACHE = `${VERSION}-pages`;

const PRECACHE_STATIC = [
  "/offline",
  "/static/manifest.json",
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/vendor/css/bootstrap.min.css",
  "/static/vendor/css/bootstrap-icons.min.css",
  "/static/vendor/js/bootstrap.bundle.min.js",
  "/static/vendor/css/fonts/bootstrap-icons.woff2",
  "/static/vendor/css/fonts/bootstrap-icons.woff"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(async (cache) => {
      const precachePromises = PRECACHE_STATIC.map(async (url) => {
        try {
          const response = await fetch(url, { cache: "reload" });
          if (response && response.ok) {
            await cache.put(url, response);
          }
        } catch (err) {
          // Gracefully continue if an optional font or asset is unreachable
        }
      });
      await Promise.all(precachePromises);
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== STATIC_CACHE && key !== PAGE_CACHE)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

async function cacheFirstStatic(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cachedResponse = await cache.match(request);

  const fetchPromise = fetch(request)
    .then((networkResponse) => {
      if (networkResponse && networkResponse.ok) {
        cache.put(request, networkResponse.clone());
      }
      return networkResponse;
    })
    .catch(() => cachedResponse);

  return cachedResponse || fetchPromise;
}

async function networkFirst(request) {
  const cache = await caches.open(PAGE_CACHE);
  try {
    const networkResponse = await fetch(request);
    if (networkResponse && networkResponse.ok) {
      cache.put(request, networkResponse.clone());
    }
    return networkResponse;
  } catch (err) {
    const cachedResponse = await cache.match(request);
    if (cachedResponse) {
      return cachedResponse;
    }
    if (request.mode === "navigate" || (request.headers.get("accept") && request.headers.get("accept").includes("text/html"))) {
      const offlineFallback = await caches.match("/offline");
      if (offlineFallback) {
        return offlineFallback;
      }
    }
    return new Response("You are currently offline and this resource is not cached.", {
      status: 503,
      statusText: "Offline",
      headers: { "Content-Type": "text/plain" }
    });
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  const path = url.pathname;

  // Don't intercept server-sent assistant events or logout
  if (path.startsWith("/assistant/events") || path.startsWith("/auth/sign-out")) {
    return;
  }

  // Precached & static assets (CSS, JS, images, fonts, manifest)
  if (
    path.startsWith("/static/") ||
    path === "/manifest.json" ||
    path === "/sw.js"
  ) {
    event.respondWith(cacheFirstStatic(request));
    return;
  }

  // Dynamic pages & GET data: Network-First with Cache Fallback
  event.respondWith(networkFirst(request));
});

/* ---- Web Push Notifications ---- */
self.addEventListener("push", (event) => {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      try {
        data = { body: event.data.text() };
      } catch (e2) {
        data = {};
      }
    }
  }

  const title = data.title || "BG Command Centre";
  const options = {
    body: data.body || "You have a new update.",
    icon: data.icon || "/static/icons/icon-192.png",
    badge: data.badge || "/static/icons/icon-192.png",
    tag: data.tag || ("bgcc-notification-" + (data.id || Date.now())),
    data: {
      url: data.url || "/notifications/",
      id: data.id || null
    },
    requireInteraction: false
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

/* ---- Notification Click Handling ---- */
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const rawUrl = (event.notification.data && event.notification.data.url) ? event.notification.data.url : "/notifications/";
  const targetUrl = new URL(rawUrl, self.location.origin).href;

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if (client.url === targetUrl && "focus" in client) {
          return client.focus();
        }
      }
      for (const client of windowClients) {
        if ("focus" in client && "navigate" in client) {
          return client.focus().then(() => client.navigate(targetUrl));
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});
