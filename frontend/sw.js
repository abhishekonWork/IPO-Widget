// Caches only the app shell (HTML/CSS/JS), never the live IPO data itself --
// data always comes fresh from the backend API so the widget never shows
// silently stale numbers as if they were current.
//
// IMPORTANT: bump SHELL_CACHE's version number (v1 -> v2 -> v3...) every
// time app.js/style.css/index.html change. The "activate" handler below
// deletes any OLD-named cache automatically, which is what lets a phone
// pick up a new deploy without the person needing to manually clear
// their browser cache.
const SHELL_CACHE = "ipo-widget-shell-v3";
const SHELL_FILES = ["./index.html", "./style.css", "./app.js", "./manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting(); // activate this new service worker immediately, don't wait for old tabs to close
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((name) => name !== SHELL_CACHE) // delete every cache that isn't THIS version
          .map((name) => caches.delete(name))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Never cache API calls -- always hit the network for live data.
  if (url.pathname.startsWith("/api/")) return;

  // Network-first for the app shell itself: try to get the latest file
  // from the server, only falling back to the cached copy if offline.
  // This means a fresh deploy shows up on next visit without the person
  // needing to clear anything -- the OLD cache-first approach is what
  // caused the "I updated GitHub but the site looks the same" issue.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const responseClone = response.clone();
        caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, responseClone));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
