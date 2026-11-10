const CACHE_NAME = "tesla-control-v1";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  // 充電状態は常に最新を取得する必要があるため、オフライン時のフォールバック以外はキャッシュしない
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
