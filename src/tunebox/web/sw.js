/*
 * Service worker — the "installable to the home screen" half of PLAN.md Phase 4.
 *
 * It caches the app *shell* and nothing else, and that boundary is the whole design:
 *
 * * The shell (markup, CSS, script, manifest, icons) is bytes from this repository. Serving it from
 *   a cache means the icon opens instantly on a phone and, when the house network is down, shows
 *   tunebox saying "cannot reach tunebox" rather than the browser's dinosaur.
 *
 * * **API responses are never cached, and the worker does not even look at them.** A cached
 *   now-playing screen is worse than no screen: it would show a track that stopped ten minutes ago
 *   with a pause button that does nothing, and the user's only clue would be that reality disagreed.
 *   Requests outside the shell list are not intercepted at all, so there is no code path in which a
 *   stale `/player` or `/jobs` can be served — a stricter guarantee than a cache policy, because
 *   there is no cache to get it wrong.
 *
 * * The token is in a header the page attaches per request, never in a cached response, so nothing
 *   here can leak it into storage that outlives "forget token".
 *
 * The cache name carries the tunebox version, substituted when this file is served. That is what
 * makes an upgrade take effect: a new version is a new cache, and `activate` deletes every older
 * one rather than leaving a phone on last month's JavaScript.
 */

const VERSION = "__TUNEBOX_VERSION__";
const CACHE = `tunebox-shell-${VERSION}`;

const SHELL = [
  "/",
  `/ui/app.css?v=${VERSION}`,
  `/ui/app.js?v=${VERSION}`,
  "/ui/icon.svg",
  "/ui/icon-180.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      // Individually rather than cache.addAll: addAll is atomic, so one 404 (an icon renamed in a
      // later version, say) would leave the worker with no cache at all and no clue why.
      await Promise.all(
        SHELL.map((url) => cache.add(new Request(url, { cache: "reload" })).catch(() => {}))
      );
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names.filter((name) => name.startsWith("tunebox-shell-") && name !== CACHE).map((name) => caches.delete(name))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (!isShell(url)) return; // API traffic: not ours to touch. See the header comment.

  event.respondWith(
    (async () => {
      // Network first, so a running host always wins and an upgraded shell is picked up on the next
      // load rather than the one after. The cache is the fallback, which is the only thing it is for.
      try {
        const response = await fetch(request);
        if (response && response.ok) {
          const cache = await caches.open(CACHE);
          cache.put(request, response.clone());
        }
        return response;
      } catch (error) {
        const cached = await caches.match(request, { ignoreSearch: true });
        if (cached) return cached;
        throw error;
      }
    })()
  );
});

function isShell(url) {
  if (url.pathname === "/" || url.pathname === "/manifest.webmanifest") return true;
  return url.pathname.startsWith("/ui/");
}
