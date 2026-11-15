"""Ponte locale di CUORE: serve l'app e i feed di SOLA LETTURA di CUORE School e CUORE Money, solo su 127.0.0.1.

    python3 app/bridge/serve.py [--porta 8765]

- Ascolta solo su 127.0.0.1: non raggiungibile da altri dispositivi (nessuna esposizione, nessun iPhone per ora).
- Solo GET: `/` (app/index.html) e `/api/school` (feed `cuore.school.feed/1`, tenuto SOLO in memoria: ricalcolato
  quando cambiano i file letti o il giorno, e comunque dopo 15 minuti; il calcolo del motore richiede decine di secondi).
  Ogni altro metodo o percorso: 405/404. Host diverso da 127.0.0.1/localhost: 403 (contro il DNS rebinding).
- `/api/money` (feed `cuore.money.feed/1`, cuore_money.py): database Finance aperto immutable + readOnly, contratto
  validato, stessa cache in memoria (stato dei file del database + giorno, 15 minuti). Senza `money_fn` (es. nei test)
  l'endpoint risponde 503 e non legge nulla.
- `/api/events` (registro `cuore.events/1`, cuore_events.py): vista calcolata in sola lettura da log, calendario e stato
  del job e dalle esecuzioni del Registry (copia in memoria); nessuna cache, calcolo di pochi millisecondi. Senza
  `events_fn` (es. nei test) risponde 503 e non legge nulla. Documento validato prima dell'invio: un registro non
  conforme o non calcolabile risponde 503 con il codice (`contratto_non_valido` | `non_disponibile`), mai eventi parziali.
  Finance e' collegato con i soli metadati di `data_status` del feed Money gia' in cache (nessuna lettura in piu').
- `/api/search?q=<testo>[&limite=N]` (`cuore.search/1`, cuore_search.py): ricerca nei feed gia' esposti (School, Money,
  eventi validati) e nell'indice Classroom (cuore_classroom.py: materiali con link originale, compiti, annunci), in memoria; ogni fonte dichiarata (letta, non disponibile, non collegata). Domanda non valida: 400.
- Rete privata (facoltativa, spenta per default): `--tailnet-host <nome> --tailnet-user <login>` ammette le richieste
  inoltrate da un proxy locale della rete privata (es. `tailscale serve`) solo per gli utenti elencati, letti
  dall'intestazione d'identita' del proxy. Il ponte resta su 127.0.0.1: nessuna porta aperta verso l'esterno.
- Richieste cross-site (Sec-Fetch-Site `cross-site`/`same-site` o Origin estraneo): 403, prima di ogni altro controllo.
- Nessun log, nessuna cache su disco, nessun file scritto.

Scritture del titolare (spente per default): `--scritture [--school-personale <file .jsonl>]`.
- GET `/api/scritture` (`cuore.scritture/1`): `attive` vero/falso, `dati: "reali"`, operazioni ammesse; la pagina porta
  il segnale `<meta name="cuore-scritture" content="reali">` e il feed Money `scritture.attive`.
- POST `/api/scritture/esegui` con `{"op", "input", "requestId"}`: stesse operazioni del ponte sintetico
  (`scritture.py`); solo da questo Mac (niente rete privata), Origin del ponte obbligatorio, JSON fino a 64 KiB,
  `requestId` obbligatorio per le scritture (ripetuta = stessa risposta, mai un doppione). Finance: `scrivi.ts` sul
  database di `/api/money`, con backup verificato prima della prima scrittura del giorno (altrimenti 403). School:
  archivio personale (predefinito `~/Library/Application Support/CUORE/school/personale.jsonl`, 0700/0600), mai in
  iCloud o nel repository; il Registry resta in sola lettura. Le letture restano in sola lettura (`sola_lettura_verificata`).
  Registro su stderr: solo operazione ed esito, mai importi o testi.
- Senza `--scritture` ogni POST risponde 405, come prima.

Promemoria del Mac (`promemoria.py`): GET `/api/promemoria` (`cuore.promemoria.impostazioni/1`, anche senza
`--scritture`) legge solo il file delle impostazioni (`--promemoria`, predefinito
`~/Library/Application Support/CUORE/promemoria.json`): promemoria accesi/spenti, momenti, prossimo controllo. Con
`--scritture` le operazioni `promemoria_stato`, `promemoria_imposta`, `appunti_stato`, `appunti_scrivi` (appunti
Finance in `--finance-appunti`, predefinito `~/Library/Application Support/CUORE/finance/appunti.jsonl`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cuore_money  # noqa: E402
import cuore_school  # noqa: E402
import promemoria  # noqa: E402
import scritture  # noqa: E402

APP_DIR = os.path.normpath(os.path.join(HERE, ".."))
SCHOOL_PERSONALE = "~/Library/Application Support/CUORE/school/personale.jsonl"
SETTINGS_ERROR = {"schema": promemoria.SETTINGS_SCHEMA, "file": "non_valido", "promemoria": []}
WRITES_META = b'<meta name="cuore-scritture" content="reali">'
INDEX = os.path.join(APP_DIR, "index.html")
BIND = "127.0.0.1"
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; "
       "manifest-src 'self'; worker-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")

# Asset statici della PWA (solo lettura, percorso fisso: nessuna scrittura, nessun attraversamento di cartelle).
STATIC_ASSETS = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/push.js": ("push.js", "text/javascript; charset=utf-8"),     # notifiche push: solo online (stato = solo_online)
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
}


class FeedCache:
    """Cache in memoria del feed (mai su disco). Chiave: stato dei file letti + giorno; età massima `ttl` secondi."""

    def __init__(self, build=cuore_school.build_feed, state=cuore_school.inputs_state, ttl=900, clock=time.monotonic):
        self.build, self.state, self.ttl, self.clock = build, state, ttl, clock
        self.lock = threading.Lock()
        self.entry = None                                               # (stato, istante, feed)

    def get(self):
        with self.lock:
            k = (self.state(), dt.date.today().isoformat())
            e = self.entry
            if e is None or e[0] != k or self.clock() - e[1] > self.ttl:
                feed = self.build()
                self.entry = (k, self.clock(), feed) if feed.get("sola_lettura_verificata") else None   # gli errori non restano in cache
                return feed
            return e[2]


def money_cache():
    path = cuore_money.db_path()
    return FeedCache(build=lambda: cuore_money.build_feed(path),
                     state=lambda: json.dumps(cuore_money.files_state(path), sort_keys=True))


def classroom_cache():
    """Indice Classroom per la ricerca (cuore_classroom.py), stessa cache in memoria del feed School."""
    import cuore_classroom
    return FeedCache(build=cuore_classroom.build_index, state=cuore_classroom.inputs_state)


NOT_CONNECTED = {"schema": cuore_money.SCHEMA, "fonte": "reale", "operazioni": {}, "sola_lettura_verificata": False,
                 "errori": [{"ambito": "ponte", "messaggio": "CUORE Money non collegato in questo ponte"}]}


IDENTITY_HEADER = "Tailscale-User-Login"


WRITES_OFF = {"schema": "cuore.scritture/1", "attive": False, "dati": "reali", "operazioni": []}


def money_with_writes(get, writer):
    """Feed Money letto senza sovrapporsi a una scrittura e con l'indicazione delle scritture attive sui dati reali."""
    return lambda: dict(writer.locked(get), scritture={"database": "dati reali", "banche": False, "attive": True})


EVENTS_NOT_CONNECTED = {"schema": "cuore.events/1", "eventi": [], "fonti": {},
                        "errori": [{"ambito": "ponte", "messaggio": "registro degli eventi non collegato in questo ponte"}]}


def events_response(events_fn):
    """(stato HTTP, documento) per `/api/events`: il registro solo se conforme al contratto; altrimenti 503 con il codice
    e il tipo d'errore (mai il messaggio, che potrebbe contenere percorsi o dati)."""
    import cuore_events
    try:
        doc = events_fn()
    except Exception as e:      # noqa: BLE001 - registro non calcolabile: dichiarato, il ponte resta in piedi
        return 503, dict(EVENTS_NOT_CONNECTED, errori=[{"ambito": "registro", "codice": "non_disponibile",
                                                        "messaggio": "registro non calcolabile (%s)" % type(e).__name__}])
    problems = cuore_events.validate(doc)
    if problems:
        return 503, dict(EVENTS_NOT_CONNECTED, errori=[{"ambito": "registro", "codice": "contratto_non_valido",
                                                        "messaggio": "registro non conforme al contratto (%d problemi)"
                                                                     % len(problems)}])
    return 200, doc


def events_build(money_fn=None):
    """Registro degli eventi (cuore_events, sola lettura) con le fonti School reali e, se `money_fn` e' dato, i metadati
    di `data_status` del feed Money (stessa cache di `/api/money`). Feed Money non calcolabile = fonte Finance
    `illeggibile` nel registro, mai «non collegata» ne' «nessun evento»."""
    import cuore_events
    money = None
    if money_fn is not None:
        try:
            money = money_fn()
        except Exception:      # noqa: BLE001 - dichiarato dal registro come fonte illeggibile
            money = {}
    return cuore_events.build(real=True, money_feed=None if money_fn is None else money if isinstance(money, dict) else {})


def settings_fn(path):
    """Stato delle impostazioni dei promemoria (sola lettura), solo da un percorso fuori da iCloud e dal repository."""
    def read():
        try:
            return promemoria.settings_status(promemoria.outside_cloud_and_repo(path, ".json", "impostazioni promemoria"))
        except promemoria.ConfigNonValida as e:
            return dict(SETTINGS_ERROR, errore=str(e))
    return read


def make_handler(port, feed_fn=None, money_fn=None, tailnet_host=None, tailnet_users=(), events_fn=None, classroom_fn=None,
                 writer=None, reminders_fn=None):
    """`tailnet_host` / `tailnet_users` (facoltativi, SPENTI per default): accesso dalla rete privata tramite un proxy
    locale (es. `tailscale serve`, che inoltra solo verso 127.0.0.1 e aggiunge l'intestazione d'identita'). Il ponte
    resta in ascolto solo su 127.0.0.1; una richiesta con quel nome host O con l'intestazione d'identita' passa solo
    se l'utente e' nell'elenco. Nessun elenco = nessun accesso dalla rete privata."""
    feed_fn = feed_fn or FeedCache().get
    allowed_hosts = {"%s:%d" % (BIND, port), "localhost:%d" % port}
    local_origins = {"http://" + h for h in allowed_hosts}
    origins = set(local_origins)
    tailnet_host = (tailnet_host or "").strip().lower() or None
    users = {u.strip().lower() for u in tailnet_users if u and u.strip()}
    if tailnet_host and not users:
        raise ValueError("accesso dalla rete privata senza utenti autorizzati: indicare almeno un --tailnet-user")
    if tailnet_host:
        origins.add("https://" + tailnet_host)

    class Handler(BaseHTTPRequestHandler):
        server_version = "CUORE"
        sys_version = ""

        def log_message(self, *_a):                                    # nessun log (né su disco né a terminale)
            pass

        def _send(self, code, body, ctype):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _guard(self):
            if not self._same_origin():
                self._send(403, "richiesta da un altro sito", "text/plain; charset=utf-8")
                return False
            host = (self.headers.get("Host") or "").strip().lower()
            login = self.headers.get(IDENTITY_HEADER)
            via_tailnet = tailnet_host is not None and host in (tailnet_host, tailnet_host + ":443")
            if via_tailnet or login is not None:                       # richiesta arrivata dalla rete privata
                if not via_tailnet and host not in allowed_hosts:
                    self._send(403, "host non ammesso", "text/plain; charset=utf-8")
                    return False
                if tailnet_host is None or (login or "").strip().lower() not in users:
                    self._send(403, "utente non autorizzato", "text/plain; charset=utf-8")
                    return False
                return True
            if host not in allowed_hosts:
                self._send(403, "host non ammesso", "text/plain; charset=utf-8")
                return False
            return True

        def _same_origin(self):
            """Richieste cross-site rifiutate quando il browser lo dichiara (Sec-Fetch-Site) o invia un Origin estraneo.
            Senza queste intestazioni (client non browser, vecchi browser) decidono i controlli su Host e identita'."""
            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if site in ("cross-site", "same-site"):
                return False
            origin = (self.headers.get("Origin") or "").strip().lower()
            return not origin or origin in origins

        def do_GET(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    html = f.read()
                if writer is not None:
                    html = html.replace(b"<head>", b"<head>" + WRITES_META, 1)
                self._send(200, html, "text/html; charset=utf-8")
            elif path == "/api/scritture":
                doc = writer.capabilities() if writer is not None else WRITES_OFF
                self._send(200, json.dumps(doc, ensure_ascii=False), "application/json; charset=utf-8")
            elif path == "/api/school":
                self._send(200, json.dumps(feed_fn(), ensure_ascii=False), "application/json; charset=utf-8")
            elif path == "/api/money":
                if money_fn is None:
                    self._send(503, json.dumps(NOT_CONNECTED, ensure_ascii=False), "application/json; charset=utf-8")
                else:
                    self._send(200, json.dumps(money_fn(), ensure_ascii=False), "application/json; charset=utf-8")
            elif path == "/api/events":
                if events_fn is None:
                    self._send(503, json.dumps(EVENTS_NOT_CONNECTED, ensure_ascii=False), "application/json; charset=utf-8")
                else:
                    code, doc = events_response(events_fn)
                    self._send(code, json.dumps(doc, ensure_ascii=False), "application/json; charset=utf-8")
            elif path == "/api/search":
                self._search()
            elif path == "/api/promemoria":
                doc = reminders_fn() if reminders_fn is not None else dict(SETTINGS_ERROR, errore="non collegato")
                self._send(200 if reminders_fn is not None else 503, json.dumps(doc, ensure_ascii=False),
                           "application/json; charset=utf-8")
            elif path in STATIC_ASSETS:
                rel, ctype = STATIC_ASSETS[path]
                with open(os.path.join(APP_DIR, rel), "rb") as f:
                    self._send(200, f.read(), ctype)
            else:
                self._send(404, "non trovato", "text/plain; charset=utf-8")

        def _search(self):
            import cuore_search
            from urllib.parse import parse_qs
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "", max_num_fields=5)
            ev = (lambda: events_response(events_fn)[1]) if events_fn is not None else None
            try:
                doc = cuore_search.search(qs.get("q", [""])[0], school_fn=feed_fn, money_fn=money_fn, events_fn=ev,
                                          limit=int(qs.get("limite", ["50"])[0]), classroom_fn=classroom_fn)
            except (cuore_search.QueryError, ValueError) as e:
                msg = str(e) if isinstance(e, cuore_search.QueryError) else "limite non valido"
                self._send(400, json.dumps({"schema": cuore_search.SCHEMA, "errore": "domanda_non_valida", "messaggio": msg},
                                           ensure_ascii=False), "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(doc, ensure_ascii=False), "application/json; charset=utf-8")

        do_HEAD = do_GET

        def _refuse(self):
            self._send(405, "solo lettura", "text/plain; charset=utf-8")

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse

        def _json(self, code, doc):
            self._send(code, json.dumps(doc, ensure_ascii=False), "application/json; charset=utf-8")

        def _write(self):
            """Scrittura sui dati reali: solo da questo Mac, dalla pagina del ponte (Origin obbligatorio)."""
            if not self._guard():
                return
            host = (self.headers.get("Host") or "").strip().lower()
            if self.headers.get(IDENTITY_HEADER) is not None or host not in allowed_hosts:
                self._json(403, {"ok": False, "errore": "rifiutato", "motivo": "scritture solo da questo Mac"})
                return
            origin = (self.headers.get("Origin") or "").strip().lower()
            site = (self.headers.get("Sec-Fetch-Site") or "same-origin").strip().lower()
            if origin not in local_origins or site != "same-origin":
                self._json(403, {"ok": False, "errore": "rifiutato", "motivo": "origine non ammessa"})
                return
            if self.path.split("?", 1)[0] != "/api/scritture/esegui":
                self._send(404, "non trovato", "text/plain; charset=utf-8")
                return
            req, err = scritture.read_request(self.headers, self.rfile)
            self._json(*(err or writer.execute(req)))

        if writer is not None:
            do_POST = _write

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--porta", type=int, default=8765)
    ap.add_argument("--tailnet-host", help="nome del Mac nella rete privata (es. mac.esempio.ts.net); spento per default")
    ap.add_argument("--tailnet-user", action="append", default=[], help="login autorizzato (ripetibile)")
    ap.add_argument("--scritture", action="store_true", help="attiva le scritture del titolare sui dati REALI")
    ap.add_argument("--school-personale", default=SCHOOL_PERSONALE, help="archivio personale School (.jsonl)")
    ap.add_argument("--promemoria", default=promemoria.CONFIG_DEFAULT, help="impostazioni dei promemoria (.json)")
    ap.add_argument("--finance-appunti", default=promemoria.FINANCE_APPUNTI, help="appunti Finance (.jsonl)")
    a = ap.parse_args(argv)
    cache, money, classroom = FeedCache(), money_cache(), classroom_cache()
    money_fn, writer = money.get, None
    if a.scritture:
        node, err = cuore_money._node()
        try:
            if err:
                raise scritture.Rifiuto(err)
            writer = scritture.Scrittore(
                cuore_money.db_path(), scritture.school_archive(os.path.expanduser(a.school_personale)), node,
                settings_path=os.path.expanduser(a.promemoria),
                notes_path=scritture.archive(os.path.expanduser(a.finance_appunti), label="appunti Finance"))
        except scritture.Rifiuto as e:
            print("Scritture NON attivate: %s" % e, file=sys.stderr)
            return 2
        money_fn = money_with_writes(money.get, writer)
    handler = make_handler(a.porta, feed_fn=cache.get, money_fn=money_fn, tailnet_host=a.tailnet_host,
                           tailnet_users=a.tailnet_user, events_fn=lambda: events_build(money_fn),
                           classroom_fn=classroom.get, writer=writer,
                           reminders_fn=settings_fn(os.path.expanduser(a.promemoria)))
    httpd = ThreadingHTTPServer((BIND, a.porta), handler)
    threading.Thread(target=cache.get, daemon=True).start()            # primo calcolo subito, in background
    print("CUORE su http://%s:%d (solo questo Mac, Ctrl-C per fermare)" % (BIND, a.porta), flush=True)
    if writer is not None:
        print("Scritture ATTIVE sui dati reali (backup Finance verificato ogni giorno prima della prima scrittura)", flush=True)
    if a.tailnet_host:
        print("Rete privata: %s, utenti autorizzati: %d (servono `tailscale serve` attivo e il Mac acceso)"
              % (a.tailnet_host, len(a.tailnet_user)), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
