// Service worker minimale di CUORE: cache-izza SOLO gli asset statici della UI (per un uso offline di base).
// Le risposte del ponte locale (/api/...) non vengono mai cache-izzate: i dati restano sempre letti dal ponte.
const CACHE = "cuore-static-v22";
const STATIC_ASSETS = ["/", "/index.html", "/manifest.webmanifest", "/sw.js", "/push.js"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(STATIC_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Mai la stessa origine API del ponte: dati sempre in rete, nessuna cache.
  if (req.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) {
    return;
  }

  // Pagina (navigazione): prima la rete, la copia solo senza rete. Cosi' la grafica e' sempre l'ultima e, ad accesso
  // scaduto, la rete porta alla pagina di login di Access invece di mostrare una copia vecchia senza dati.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok && url.pathname === "/") {
            const copy = res.clone();
            caches.open(CACHE).then((cache) => cache.put("/", copy));
          }
          return res;
        })
        .catch(() => caches.match("/").then((c) => c || Response.error()))
    );
    return;
  }

  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((cache) => cache.put(req, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});

// Notifiche Web Push di CUORE online: il segnale arriva SENZA contenuto (nessun testo presso Apple/Google/Mozilla);
// i testi (riservati per difetto) si leggono qui da /api/notifiche/recenti con l'accesso del titolare. Una notifica
// per elemento, con `tag` = id: un secondo segnale sostituisce la stessa notifica invece di ripeterla.
// Se la lettura non riesce (accesso scaduto, rete) si mostra un avviso generico: iOS chiede sempre una notifica.
self.addEventListener("push", (event) => {
  event.waitUntil((async () => {
    let items = [];
    try {
      const r = await fetch("/api/notifiche/recenti", { cache: "no-store", credentials: "same-origin" });
      if (r.ok) items = ((await r.json()).notifiche || []).slice(0, 5);
    } catch (e) { /* avviso generico */ }
    if (!items.length) items = [{ id: "cuore-generico", titolo: "CUORE", testo: "Hai un promemoria: apri CUORE." }];
    await Promise.all(items.map((n) => self.registration.showNotification(n.titolo || "CUORE",
      { body: n.testo || "", tag: String(n.id), icon: "/icons/icon-192.png", badge: "/icons/icon-192.png", data: { url: "/" } })));
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = all.find((c) => new URL(c.url).origin === self.location.origin);
    if (open) return open.focus();
    return self.clients.openWindow("/");
  })());
});
