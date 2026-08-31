// 四维策略面板 Service Worker（F5 PWA 离线壳）
const CACHE = "four-dim-v82";
const ASSETS = [
  "./",
  "./four_dim_live.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png"
];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});
// 网络优先，失败回退缓存（API 走网络，静态资源可离线壳）
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // API 不缓存
  if (url.pathname.endsWith("four_dim_live.html") || url.pathname === "/") {
    // HTML 文件强制走网络，避免缓存导致界面不更新
    e.respondWith(
      fetch(e.request, { cache: 'no-cache' }).catch(() => caches.match(e.request))
    );
    return;
  }
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request).then((r) => r || caches.match("./")))
  );
});
