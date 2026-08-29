// Caches only the app shell (HTML/CSS/JS), never the live IPO data itself —
// data always comes fresh from the backend API so the widget never shows
// silently stale numbers as if they were current.
const SHELL_CACHE = "ipo-widget-shell-v1";
const SHELL_FILES = ["./index.html", "./style.css", "./app.js", "./manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Never cache API calls — always hit the network for live data.
  if (url.pathname.startsWith("/api/")) return;
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
