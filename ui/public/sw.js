const SHELL = 'slopesdb-shell-v1'
const DATA  = 'slopesdb-data-v1'
const TILES = 'slopesdb-tiles-v1'
const MAX_TILES = 500

self.addEventListener('install', e => {
  self.skipWaiting()
  e.waitUntil(
    Promise.all([
      caches.open(DATA).then(c => c.add('/data/index.json')),
      caches.open(SHELL).then(c => c.addAll(['/icon-192.png', '/apple-touch-icon.png'])),
    ])
  )
})

self.addEventListener('activate', e => {
  const keep = [SHELL, DATA, TILES]
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => !keep.includes(k)).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url)
  if (['/icon-192.png', '/apple-touch-icon.png'].includes(url.pathname)) {
    e.respondWith(cacheFirst(SHELL, e.request))
  } else if (url.hostname === 'tile.opentopomap.org') {
    e.respondWith(tileFirst(e.request))
  } else if (url.pathname.startsWith('/_next/static/')) {
    e.respondWith(cacheFirst(SHELL, e.request))
  } else if (url.pathname.startsWith('/data/')) {
    e.respondWith(cacheFirst(DATA, e.request))
  } else if (e.request.mode === 'navigate') {
    e.respondWith(networkFirst(SHELL, e.request))
  }
})

async function cacheFirst(name, req) {
  const cache = await caches.open(name)
  const hit = await cache.match(req)
  if (hit) return hit
  const res = await fetch(req)
  if (res.ok) cache.put(req, res.clone())
  return res
}

async function networkFirst(name, req) {
  const cache = await caches.open(name)
  try {
    const res = await fetch(req)
    if (res.ok) cache.put(req, res.clone())
    return res
  } catch {
    return (await cache.match(req)) ?? new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain' } })
  }
}

async function tileFirst(req) {
  const cache = await caches.open(TILES)
  const hit = await cache.match(req)
  if (hit) {
    fetch(req).then(r => { if (r.ok) cache.put(req, r) }).catch(() => {})
    return hit
  }
  try {
    const res = await fetch(req)
    if (res.ok) {
      const keys = await cache.keys()
      if (keys.length >= MAX_TILES) await cache.delete(keys[0])
      cache.put(req, res.clone())
    }
    return res
  } catch {
    return new Response('', { status: 503 })
  }
}
