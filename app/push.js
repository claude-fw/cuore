// CUORE · notifiche Web Push di QUESTO dispositivo (Mac, iPhone, iPad), per la pagina Impostazioni.
// Script classico (nessuna dipendenza): <script src="push.js"></script> espone `window.CUOREPush`.
//   await CUOREPush.stato()          -> {supportato, motivo, permesso, installata, iscritto, dispositivo, nome, server}
//   await CUOREPush.iscrivi("iPhone")-> {ok, ...stato} | {ok:false, errore, messaggio}   (chiamare da un tocco/clic)
//   await CUOREPush.annulla()        -> {ok, ...stato} | {ok:false, errore, messaggio}
//   await CUOREPush.imposta(true)    -> interruttore generale del push (tutti i dispositivi), spento per difetto
//   await CUOREPush.prova()          -> una notifica di prova a tutti i dispositivi iscritti (solo con push attivo)
// Funziona solo con CUORE online (Worker): sul ponte del Mac `stato()` risponde {supportato:false, motivo:"solo_online"}.
// iPhone/iPad: solo con l'app aggiunta alla schermata Home (iOS/iPadOS 16.4 o successivo), mai da Safari normale.
(function () {
  "use strict";
  const KEY = "cuore.push.dispositivo";
  const NAME = "cuore.push.nome";
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* senza memoria: un id per sessione */ } },
  };
  let volatile = null;
  function deviceId() {
    let id = store.get(KEY) || volatile;
    if (!id) {
      const b = new Uint8Array(16);
      crypto.getRandomValues(b);
      id = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
      store.set(KEY, id);
      volatile = id;
    }
    return id;
  }
  const fail = (errore, messaggio) => ({ ok: false, errore, messaggio });
  const standalone = () =>
    (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;
  const ios = () => /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  function supporto() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      if (ios() && !standalone())
        return "aggiungi_alla_home";      // Safari su iPhone/iPad: il push esiste solo nell'app dalla Home
      return "browser_senza_push";
    }
    if (ios() && !standalone()) return "aggiungi_alla_home";
    return null;
  }

  async function api(path, body) {
    const init = body === undefined ? { cache: "no-store", credentials: "same-origin" } : {
      method: "POST", cache: "no-store", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    const r = await fetch(path, init);
    let doc = null;
    try { doc = await r.json(); } catch (e) { /* pagina di login di Access o ponte del Mac */ }
    return { status: r.status, doc };
  }

  function keyBytes(b64) {
    const s = atob((b64 + "===".slice((b64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(s, (c) => c.charCodeAt(0));
  }
  const sameKey = (sub, key) => {
    const k = sub && sub.options && sub.options.applicationServerKey;
    if (!k) return true;               // non tutti i browser la espongono: si tiene l'iscrizione
    const a = new Uint8Array(k), b = keyBytes(key);
    return a.length === b.length && a.every((x, i) => x === b[i]);
  };
  async function registration() {
    const reg = (await navigator.serviceWorker.getRegistration()) || (await navigator.serviceWorker.register("sw.js"));
    return navigator.serviceWorker.ready.then(() => reg);
  }

  async function stato() {
    const motivo = supporto();
    const base = {
      supportato: motivo === null, motivo, installata: standalone(),
      permesso: "Notification" in window ? Notification.permission : "non_disponibile",
      dispositivo: deviceId(), nome: store.get(NAME), iscritto: false, server: null,
    };
    let s;
    try { s = await api("api/notifiche"); } catch (e) { return Object.assign(base, { supportato: false, motivo: "rete" }); }
    if (s.status === 404 || s.status === 503 || !s.doc || s.doc.schema !== "cuore.notifiche/1")
      return Object.assign(base, { supportato: false, motivo: "solo_online" });
    base.server = s.doc;
    if (motivo !== null) return base;
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = reg && (await reg.pushManager.getSubscription());
    base.iscritto = !!sub && s.doc.dispositivi.some((d) => d.dispositivo === base.dispositivo);
    if (!s.doc.configurate) base.motivo = "push_non_configurato";
    return base;
  }

  async function iscrivi(nome) {
    nome = String(nome || "").trim().slice(0, 40);
    if (!nome) return fail("nome_mancante", "Dai un nome al dispositivo (es. iPhone).");
    // Safari (Mac, iPhone, iPad) mostra la richiesta solo se parte subito dal clic: prima di ogni attesa di rete.
    const chiesto = supporto() === null && Notification.permission === "default" ? Notification.requestPermission() : null;
    const st = await stato();
    if (!st.supportato) return fail(st.motivo, st.motivo === "aggiungi_alla_home"
      ? "Su iPhone e iPad apri CUORE dall'icona sulla schermata Home (Condividi → Aggiungi a Home), poi riprova."
      : st.motivo === "solo_online" ? "Le notifiche push funzionano solo con CUORE online." : "Questo browser non ha le notifiche push.");
    if (!st.server.configurate) return fail("push_non_configurato", "Le chiavi delle notifiche non sono ancora impostate.");
    const perm = chiesto ? await chiesto : Notification.permission;
    if (perm !== "granted") return fail("permesso_negato", perm === "denied"
      ? "Le notifiche sono bloccate per CUORE: sbloccale nelle Impostazioni di sistema → Notifiche, poi riprova."
      : "Permesso per le notifiche non concesso.");
    const key = st.server.chiave_pubblica;
    const reg = await registration();
    let sub = await reg.pushManager.getSubscription();
    if (sub && !sameKey(sub, key)) { await sub.unsubscribe(); sub = null; }
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
    const r = await api("api/notifiche/iscrizione", { dispositivo: st.dispositivo, nome, endpoint: sub.endpoint });
    if (r.status !== 200) return fail((r.doc && r.doc.errore) || "errore", (r.doc && r.doc.messaggio) || "Iscrizione non riuscita.");
    store.set(NAME, nome);
    return Object.assign({ ok: true }, await stato());
  }

  async function annulla() {
    const reg = "serviceWorker" in navigator ? await navigator.serviceWorker.getRegistration() : null;
    const sub = reg && reg.pushManager && (await reg.pushManager.getSubscription());
    if (sub) await sub.unsubscribe();
    const r = await api("api/notifiche/annulla", { dispositivo: deviceId() });
    if (r.status !== 200) return fail((r.doc && r.doc.errore) || "errore", "Annullamento non riuscito sul server.");
    return Object.assign({ ok: true }, await stato());
  }

  async function imposta(attive) {
    const r = await api("api/notifiche/imposta", { attive: attive === true });
    return r.status === 200 ? r.doc : fail((r.doc && r.doc.errore) || "errore", "Impostazione non salvata.");
  }

  async function prova() {
    const r = await api("api/notifiche/prova", {});
    return r.status === 200 ? r.doc : fail((r.doc && r.doc.errore) || "errore",
      r.status === 409 ? "Accendi prima le notifiche." : "Prova non riuscita.");
  }

  window.CUOREPush = { stato, iscrivi, annulla, imposta, prova };
})();
