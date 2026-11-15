"""Ponte di PROVA di CUORE: l'app con Finance su una COPIA SINTETICA, con scritture (D3b, decisione C-10).

    python3 -B app/bridge/prova.py --dati <cartella della sessione> [--porta 8766]
    python3 -B app/bridge/prova.py --nuova [--porta 8766]      crea una sessione nuova sotto la cartella temporanea

Processo SEPARATO dal ponte reale (`serve.py`, porta 8765), che resta in sola lettura sui dati reali e non importa
questo modulo. Qui non c'e' nessun percorso dei dati reali: il ponte conosce solo la cartella della sessione, verificata
da `prova_guard` (percorso, collegamenti, permessi, marcatore `sintetico`) prima dell'avvio e prima di ogni richiesta.

- Solo 127.0.0.1; porta diversa da 8765 (rifiutata); stessi controlli del ponte reale su Host, Origin e Sec-Fetch-Site.
- GET `/` (app/index.html con il segnale `cuore-prova` e una fascia «PROVA — dati sintetici»), `/api/prova` (stato della sessione e operazioni ammesse, `cuore.prova/1`), `/api/money`
  (feed `cuore.money.feed/1` della copia, `fonte: "sintetico"`, validato con il contratto di `cuore_money`).
  `/api/school`: feed School costruito da un registro SINTETICO (quello dei test del motore) creato nella sessione;
  `/api/events`, `/api/search`: 503 (non collegati in prova).
- POST `/api/prova/esegui` con `{"op": ..., "input": {...}}`: solo `Content-Type: application/json`, corpo fino a
  64 KiB, operazioni dell'elenco; eseguita da `finance/scripts/prova.ts` in un processo Node con permessi ristretti
  (scrittura solo nella cartella della sessione), una richiesta alla volta. Esiti: 200, 400 (richiesta), 403 (rifiuto
  di sicurezza o sessione chiusa), 404 (non trovato), 413 (troppo grande), 415 (tipo), 422 (ingresso non valido), 500.
- Operazioni `school_stato` e `school_scrivi` (E4): dati personali School in `school-personale.jsonl` nella sessione
  (`school_personale.py`), scritti da questo processo; Finance lo scrive solo il processo Node. Nessun log.
- Operazioni dei promemoria (`promemoria_stato`, `promemoria_imposta`, `appunti_stato`, `appunti_scrivi`): impostazioni
  in `promemoria.json` e appunti Finance in `finance-appunti.jsonl` nella sessione, stesse regole del ponte reale.
- Elenco delle operazioni, lettura della richiesta ed esiti in comune con il ponte reale (`scritture.py`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cuore_money  # noqa: E402  (solo validazione del contratto e Node: nessun percorso reale usato)
import prova_guard  # noqa: E402
import scritture  # noqa: E402

INDEX = os.path.normpath(os.path.join(HERE, "..", "index.html"))
BIND = "127.0.0.1"
SCHEMA = "cuore.prova/1"
MAX_BODY = scritture.MAX_BODY
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
OPS = scritture.OPS                      # stesse operazioni di `finance/scripts/prova.ts esegui` (che le ricontrolla)
SCHOOL_OPS = scritture.SCHOOL_OPS
LOCAL_OPS = scritture.LOCAL_OPS
NOT_CONNECTED = {"errori": [{"ambito": "prova", "messaggio": "non collegato nel ponte di prova (solo Finance sintetico)"}]}


def run_node(cmd, stdin=None, run=subprocess.run):
    """Processo Node con ambiente ridotto; (codice di uscita, documento JSON | None)."""
    return scritture.run_node(cmd, stdin, prova_guard.node_env(), run=run)


def money_feed(data_dir, node, today=None, run=subprocess.run):
    """Feed Money della copia: stessa busta e stessa validazione del ponte reale (`cuore_money.build_feed`), ma il
    comando Node e' quello sintetico, costruito dopo la verifica della sessione. Mai eccezioni."""
    try:
        cmd = prova_guard.money_command(node, data_dir, today or dt.date.today().isoformat())
    except prova_guard.ProvaRifiutata as e:
        return {"schema": cuore_money.SCHEMA, "fonte": "sintetico", "operazioni": {}, "sola_lettura_verificata": False,
                "esito": "non_disponibile", "errori": [{"ambito": "prova", "messaggio": str(e)}]}
    db = os.path.join(os.path.realpath(data_dir), prova_guard.SYNTHETIC_DB)

    def synthetic_run(_ignored, **kw):                          # il comando reale di cuore_money non viene mai eseguito
        return run(cmd, **dict(kw, env=prova_guard.node_env()))

    feed = cuore_money.build_feed(db, today=today, run=synthetic_run)
    feed["fonte"] = "sintetico"
    feed["motore"] = "Cloud Finance · copia sintetica (ponte di prova)"
    feed["scritture"] = {"database": "solo copia sintetica", "banche": False}
    return feed


def school_feed(data_dir):
    """Feed School della prova: registro sintetico dei test del motore (creato una volta nella sessione), data fissa
    del registro sintetico. Nessun percorso reale: registro, materie e file manuale stanno nella sessione."""
    engine = os.path.join(HERE, "..", "..", "scuola", "mcp", "classeviva")
    for d in (os.path.join(engine, "..", "_common"), engine, os.path.join(engine, "tests")):
        if d not in sys.path:
            sys.path.append(d)
    import cuore_school
    import test_school_records as tr
    base = os.path.join(os.path.realpath(data_dir), "school-sintetico")
    reg, subj = os.path.join(base, "registro.sqlite3"), os.path.join(base, "materie.json")
    if not os.path.exists(reg):
        os.makedirs(base, mode=0o700, exist_ok=True)
        tr.make(path=reg).close()
        with open(subj, "w", encoding="utf-8") as f:
            json.dump(tr.SUBJECTS, f)
    feed = cuore_school.build_feed(reg, dt.datetime.fromisoformat(tr.F.NOW), subjects_file=subj, manual_path=os.path.join(base, "manuale-assente.json"))
    feed["fonte"] = "sintetico"
    return feed


def school_op(data_dir, op, i, richiesta=None):
    base = os.path.realpath(data_dir)
    if op in SCHOOL_OPS:
        return scritture.school_op(os.path.join(base, "school-personale.jsonl"), op, i, richiesta)
    return scritture.promemoria_op(os.path.join(base, "promemoria.json"), os.path.join(base, "finance-appunti.jsonl"),
                                   op, i, richiesta)


BANNER = ('<div role="status" style="position:sticky;top:0;z-index:9999;background:#7a1f00;color:#fff;font:600 14px/1.4 '
          '-apple-system,system-ui,sans-serif;padding:8px 16px;text-align:center">PROVA — Finance e School su dati SINTETICI '
          '(copia di prova, scritture ammesse solo qui). I dati reali non sono toccati.</div>')


def page(html):
    """`index.html` com'e', piu' il segnale `cuore-prova` (per l'interfaccia, D2) e una fascia sempre visibile: senza,
    la pagina chiamerebbe «reali» i dati sintetici. Il file su disco non cambia (lo serve anche il ponte reale)."""
    meta = '<meta name="cuore-prova" content="sintetico">'
    html = html.replace("<head>", "<head>" + meta, 1) if "<head>" in html else meta + html
    i = html.find("<body")
    j = html.find(">", i) if i >= 0 else -1
    return html[:j + 1] + BANNER + html[j + 1:] if j >= 0 else BANNER + html


def make_handler(port, data_dir, node, run=subprocess.run):
    allowed_hosts = {"%s:%d" % (BIND, port), "localhost:%d" % port}
    origins = {"http://" + h for h in allowed_hosts}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "CUORE-prova"
        sys_version = ""

        def log_message(self, *_a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body, ensure_ascii=False)
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
            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            origin = (self.headers.get("Origin") or "").strip().lower()
            if site in ("cross-site", "same-site") or (origin and origin not in origins):
                self._send(403, "richiesta da un altro sito", "text/plain; charset=utf-8")
                return False
            if (self.headers.get("Host") or "").strip().lower() not in allowed_hosts:
                self._send(403, "host non ammesso", "text/plain; charset=utf-8")
                return False
            return True

        def _session(self):
            try:
                return prova_guard.verify(data_dir), None
            except prova_guard.ProvaRifiutata as e:
                return None, str(e)

        def do_GET(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                with open(INDEX, encoding="utf-8") as f:
                    self._send(200, page(f.read()), "text/html; charset=utf-8")
            elif path == "/api/prova":
                info, err = self._session()
                doc = {"schema": SCHEMA, "dati": "sintetici", "porta": port, "operazioni": list(OPS),
                       "sessione": info and {"id": info["sessione"], "stato": info["stato"]}, "errore": err}
                self._send(200 if info else 503, doc)
            elif path == "/api/money":
                with lock:                                      # nessuna lettura a meta' di una scrittura
                    feed = money_feed(data_dir, node, run=run)
                self._send(200 if feed.get("sola_lettura_verificata") else 503, feed)
            elif path == "/api/school":
                info, err = self._session()
                if not info:
                    self._send(503, {"errori": [{"ambito": "prova", "messaggio": err}]})
                    return
                with lock:
                    feed = school_feed(data_dir)
                self._send(200 if feed.get("sola_lettura_verificata") else 503, feed)
            elif path in ("/api/events", "/api/search"):
                self._send(503, NOT_CONNECTED)
            else:
                self._send(404, "non trovato", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self):
            if not self._guard():
                return
            if self.path.split("?", 1)[0] != "/api/prova/esegui":
                self._send(404, "non trovato", "text/plain; charset=utf-8")
                return
            req, err = scritture.read_request(self.headers, self.rfile)
            if err:
                self._send(*err)
                return
            if req["op"] in LOCAL_OPS:
                info, err = self._session()
                if not info or info["stato"] != "aperta":
                    self._send(403, {"ok": False, "errore": "rifiutato", "motivo": err or "sessione chiusa"})
                    return
                with lock:
                    try:
                        self._send(200, {"ok": True, "risultato": school_op(data_dir, req["op"], req.get("input", {}),
                                                                            req.get("requestId"))})
                    except scritture.INPUT_ERRORS as e:
                        self._send(422, {"ok": False, "errore": "non_valido", "messaggio": str(e)})
                return
            with lock:
                try:
                    cmd = prova_guard.write_command(node, data_dir)
                except prova_guard.ProvaRifiutata as e:
                    self._send(403, {"ok": False, "errore": "rifiutato", "motivo": str(e)})
                    return
                code, doc = run_node(cmd, json.dumps({"op": req["op"], "input": req.get("input", {})}), run=run)
            self._send(*scritture.node_status(code, doc))

        def _refuse(self):
            self._send(405, "metodo non ammesso", "text/plain; charset=utf-8")

        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse

    return Handler


def new_session_dir():
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(tempfile.gettempdir(), "cuore-prova-%s-%d" % (stamp, os.getpid()))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dati", help="cartella di una sessione esistente (sotto la cartella temporanea)")
    g.add_argument("--nuova", action="store_true", help="crea una sessione nuova con i dati sintetici di esempio")
    ap.add_argument("--porta", type=int, default=prova_guard.DEFAULT_PROVA_PORT)
    a = ap.parse_args(argv)
    try:
        port = prova_guard.check_port(a.porta)
        node, err = cuore_money._node()
        if err:
            raise prova_guard.ProvaRifiutata(err)
        data_dir = a.dati
        if a.nuova:
            data_dir = new_session_dir()
            code, doc = run_node(prova_guard.create_command(node, data_dir))
            if code != 0 or not (doc or {}).get("ok"):
                raise prova_guard.ProvaRifiutata("sessione non creata: %s" % ((doc or {}).get("motivo") or code))
        info = prova_guard.verify(data_dir)
    except prova_guard.ProvaRifiutata as e:
        print("Ponte di prova NON avviato: %s" % e, file=sys.stderr)
        return 2
    httpd = ThreadingHTTPServer((BIND, port), make_handler(port, info["cartella"], node))
    print("CUORE · PROVA su http://%s:%d — dati SINTETICI, sessione %s (stato: %s)\nCartella: %s\nCtrl-C per fermare."
          % (BIND, port, info["sessione"], info["stato"], info["cartella"]), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
