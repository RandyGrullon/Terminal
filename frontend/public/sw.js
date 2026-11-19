// NEXUS PWA: makes the app installable and shows the last loaded page when there is no connection.
// It never caches the API, the live socket or the hashed assets (the browser already caches those).
const CACHE = 'nexus-shell-v1'
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.add('/')).catch(() => undefined))
  self.skipWaiting()
})
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))))
  self.clients.claim()
})
self.addEventListener('fetch', (e) => {
  const req = e.request
  if (req.method !== 'GET' || req.mode !== 'navigate') return
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) caches.open(CACHE).then((c) => c.put('/', res.clone())).catch(() => undefined)
        return res
      })
      .catch(() => caches.match('/').then((r) => r || Response.error())),
  )
})
