importScripts("sw-helper.js");

self.addEventListener("install", event => {
  event.waitUntil(caches.open("v1").then(cache => cache.addAll(["offline.html", "precached.png"])));
});

self.addEventListener("fetch", event => {
  event.respondWith(fetch(event.request).catch(() => caches.match("offline.html")));
});
