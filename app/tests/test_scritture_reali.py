"""Scritture del titolare dal ponte REALE (`serve.py --scritture`, `scritture.py`, `finance/scripts/scrivi.ts`).
Solo copie SINTETICHE in cartelle temporanee, disposte come i dati veri (`<base>/finance/db/cloud-finance.sqlite3`,
archivio School `<base>/CUORE/school/personale.jsonl`): il database e l'archivio reali non vengono mai aperti, il ponte
reale sulla 8765 non viene avviato (porte libere scelte dal sistema)."""
import datetime as dt, http.client, json, os, re, shutil, stat, subprocess, sys, tempfile, threading, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(APP, "bridge"))
import cuore_money as CM  # noqa: E402
import prova_guard as G  # noqa: E402  (solo per creare la copia sintetica di partenza)
import scritture as W  # noqa: E402
import serve  # noqa: E402

NODE = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
TODAY = dt.date.today().isoformat()
SPESA = {"accountId": "conto-demo", "transactionDate": TODAY, "amountMinor": -4321, "currency": "EUR",
         "description": "Pizzeria SEGRETA"}


def real_like(base):
    """Copia sintetica v7 nella disposizione dei dati veri; restituisce il percorso del database."""
    s = os.path.join(base, "s")
    fin = os.path.join(G.FINANCE, "src")
    js = ("import {createSyntheticSession} from %s; import {MIGRATIONS} from %s; "
          "await createSyntheticSession(process.argv[1], {allowedRoot: process.argv[2], forbidden: []}, MIGRATIONS);"
          % (json.dumps(os.path.join(fin, "synthetic", "generate.ts")), json.dumps(os.path.join(fin, "db", "migrations", "index.ts"))))
    r = subprocess.run([NODE, "--input-type=module", "-e", js, s, os.path.realpath(base)], capture_output=True, text=True, timeout=120,
                       env=G.node_env())
    if r.returncode != 0:
        raise AssertionError("copia sintetica non creata: %s" % r.stdout[:300])
    d = os.path.join(base, "finance", "db")
    os.makedirs(d, mode=0o700)
    db = os.path.join(d, "cloud-finance.sqlite3")
    shutil.copyfile(os.path.join(s, G.SYNTHETIC_DB), db)
    os.chmod(db, 0o600)
    return db


@unittest.skipUnless(NODE, "node non disponibile")
class PonteRealeConScritture(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="cuore-scritture-test-"))
        self.addCleanup(shutil.rmtree, self.base)
        self.db = real_like(self.base)
        self.school = os.path.join(self.base, "CUORE", "school", "personale.jsonl")
        self.logs = []
        self.writer = W.Scrittore(self.db, self.school, NODE, log=self.logs.append)
        self.port = self.serve(self.writer)
        self.rid = 0

    def serve(self, writer):
        httpd = serve.ThreadingHTTPServer((serve.BIND, 0), None)
        port = httpd.server_address[1]
        money = lambda: CM.build_feed(self.db)
        httpd.RequestHandlerClass = serve.make_handler(
            port, feed_fn=lambda: {"schema": "x"}, writer=writer,
            money_fn=serve.money_with_writes(money, writer) if writer else money)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port

    def req(self, method, path, body=None, headers=None, port=None):
        port = port or self.port
        c = http.client.HTTPConnection(serve.BIND, port, timeout=180)
        h = {"Host": "127.0.0.1:%d" % port, "Origin": "http://127.0.0.1:%d" % port, **(headers or {})}
        data = None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers={k: v for k, v in h.items() if v is not None})
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode("utf-8", "replace")

    def op(self, op, i, rid=None, **kw):
        if rid is None:
            self.rid += 1
            rid = "richiesta-%04d" % self.rid
        return self.req("POST", "/api/scritture/esegui", {"op": op, "input": i, "requestId": rid}, **kw)

    def feed(self):
        s, f = self.req("GET", "/api/money")
        self.assertEqual(s, 200)
        return f

    def backups(self):
        d = os.path.join(self.base, "finance", "backup")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_capacita_segnale_e_feed(self):
        s, cap = self.req("GET", "/api/scritture")
        self.assertEqual((s, cap["attive"], cap["dati"]), (200, True, "reali"))
        s, html = self.req("GET", "/")
        self.assertIn('<meta name="cuore-scritture" content="reali">', html)
        f = self.feed()
        self.assertTrue(f["sola_lettura_verificata"], f["errori"])
        self.assertEqual(f["scritture"], {"database": "dati reali", "banche": False, "attive": True})
        self.assertTrue(f["operazioni"]["manual"]["ok"] and f["operazioni"]["budgets"]["ok"], f["errori"])
        self.assertEqual(self.backups(), [])                                   # le letture non fanno backup

    def test_ogni_operazione_con_backup_e_idempotenza(self):
        s, add = self.op("aggiungi_manuale", SPESA, rid="spesa-0001")
        self.assertEqual((s, add["risultato"]["status"], add["backup"]), (200, "created", "creato"), add)
        mid = add["risultato"]["id"]
        files = self.backups()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^cloud-finance-prima-di-scrittura-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z\.sqlite3$")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.base, "finance", "backup", files[0])).st_mode), 0o600)
        s, again = self.op("aggiungi_manuale", SPESA, rid="spesa-0001")
        self.assertEqual((s, again["ripetuta"], again["risultato"]["id"]), (200, True, mid))
        s, other = self.op("aggiungi_manuale", dict(SPESA, amountMinor=-1), rid="spesa-0001")
        self.assertEqual((s, other["errore"]), (409, "requestId_gia_usato"))
        fresh = W.Scrittore(self.db, self.school, NODE, log=self.logs.append)    # dopo un riavvio: lo protegge il motore
        s, doc = fresh.execute({"op": "aggiungi_manuale", "input": SPESA, "requestId": "spesa-0001"})
        self.assertEqual((s, doc["risultato"]["status"], doc["backup"]), (200, "already_recorded", "presente"))
        self.assertEqual(len(self.backups()), 1)                               # un solo backup al giorno

        self.assertEqual(self.op("correggi_manuale", {"id": mid, "modifiche": {"amountMinor": -4000}, "motivo": "scontrino"})[0], 200)
        s, hist = self.op("storico_manuale", {"id": mid})
        self.assertEqual((s, [h["action"] for h in hist["risultato"]]), (200, ["add_manual_transaction", "correct_manual_transaction"]))
        self.assertEqual(self.op("classifica", {"id": mid, "tipo": "expense", "categoria": None})[0], 200)
        s, b = self.op("imposta_budget", {"scope": "total", "target": None, "validFrom": TODAY[:7], "currency": "EUR",
                                          "amountMinor": 30000})
        self.assertEqual(s, 200, b)
        s, st = self.op("stato_budget", {"mese": TODAY[:7], "valuta": "EUR"})
        self.assertEqual((s, st["risultato"]["lines"][0]["spentMinor"]), (200, 4000))
        self.assertEqual(self.op("crea_conto", {"id": "contanti", "name": "Contanti", "type": "cash", "currency": "EUR"})[0], 200)
        self.assertEqual(self.op("rileva_abbinamenti", {})[0], 200)
        for o in ("conferma_abbinamento", "rifiuta_abbinamento", "revoca_abbinamento"):
            self.assertIn(self.op(o, {"collegamento": "manca", "motivo": "x"})[0], (404, 422), o)
        s, q = self.op("consulta", {"operazione": "accounts", "input": {}})
        self.assertEqual((s, q["risultato"]["ok"]), (200, True))
        self.assertEqual(self.op("annulla_manuale", {"id": mid, "motivo": "doppione"})[0], 200)
        self.assertEqual(self.op("annulla_manuale", {"id": mid, "motivo": "doppione"})[0], 422)   # gia' annullato

        f = self.feed()
        self.assertTrue(f["sola_lettura_verificata"], f["errori"])
        man = f["operazioni"]["manual"]["data"]["items"]
        self.assertEqual([(m["id"], m["voided"]) for m in man], [(mid, True)])
        self.assertNotIn(mid, [t.get("id") for t in f["operazioni"]["transactions"]["data"]["items"]])
        lines = f["operazioni"]["budgets"]["data"]["currencies"][0]["lines"]
        self.assertEqual((lines[0]["budgetMinor"], lines[0]["spentMinor"]), (30000, 0))
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.db))), ["cloud-finance.sqlite3"])

    def test_school_archivio_personale(self):
        s, st = self.op("school_stato", {})
        self.assertEqual((s, st["risultato"]["eventi"]), (200, 0))
        self.assertFalse(os.path.exists(os.path.dirname(self.school)))        # leggere non crea nulla
        ev = {"tipo": "nota", "testo": "Nota RISERVATA", "materia": "fisica"}
        s, n = self.op("school_scrivi", ev, rid="nota-00001")
        self.assertEqual(s, 200, n)
        self.assertEqual(self.op("school_scrivi", ev, rid="nota-00001")[1]["ripetuta"], True)
        fresh = W.Scrittore(self.db, self.school, NODE, log=self.logs.append)
        self.assertEqual(fresh.execute({"op": "school_scrivi", "input": ev, "requestId": "nota-00001"})[1]["risultato"]["id"],
                         n["risultato"]["id"])
        self.assertEqual(self.op("school_scrivi", {"tipo": "impegno", "testo": "x", "data": "2026-13-01"})[0], 422)
        self.assertEqual(self.op("school_scrivi", {"tipo": "annulla", "id": n["risultato"]["id"]})[0], 200)
        s, st = self.op("school_stato", {})
        self.assertEqual((st["risultato"]["eventi"], st["risultato"]["note"]), (2, []))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.school)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.school).st_mode), 0o600)
        os.chmod(self.school, 0o644)
        self.assertEqual(self.op("school_scrivi", {"tipo": "compito", "uid": "cvv:1", "fatto": True})[0], 403)

    def test_backup_non_verificato_rifiuta_la_scrittura(self):
        with open(os.path.join(self.base, "finance", "backup"), "w") as f:
            f.write("non una cartella")
        s, doc = self.op("aggiungi_manuale", SPESA)
        self.assertEqual((s, doc["errore"]), (403, "rifiutato"), doc)
        self.assertIn("backup", doc["motivo"])
        self.assertEqual(self.feed()["operazioni"]["manual"]["data"]["items"], [])

    def test_richieste_estranee_rifiutate(self):
        body = {"op": "aggiungi_manuale", "input": SPESA, "requestId": "estranea-01"}
        for h in ({"Host": "evil.example"}, {"Origin": None}, {"Origin": "https://evil.example"},
                  {"Sec-Fetch-Site": "cross-site"}, {"Tailscale-User-Login": "a@b"}):
            self.assertEqual(self.req("POST", "/api/scritture/esegui", body, h)[0], 403, h)
        self.assertEqual(self.op("aggiungi_manuale", SPESA, rid="")[0], 400)                    # requestId obbligatorio
        self.assertEqual(self.req("POST", "/api/scritture/esegui", {"op": "cancella_tutto", "requestId": "x" * 10})[0], 400)
        self.assertEqual(self.req("POST", "/api/scritture/esegui", body, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("POST", "/api/scritture/esegui", b"x" * (W.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.req("POST", "/api/prova/esegui", body)[0], 404)
        self.assertEqual(self.req("PUT", "/api/scritture/esegui", body)[0], 405)
        self.assertEqual(self.backups(), [])

    def test_registro_senza_dati(self):
        self.op("aggiungi_manuale", SPESA)
        self.op("school_scrivi", {"tipo": "nota", "testo": "Nota RISERVATA"})
        self.op("aggiungi_manuale", dict(SPESA, currency="XXX"))
        text = "\n".join(self.logs)
        self.assertEqual(len(self.logs), 3)
        for secret in ("4321", "43,21", "Pizzeria", "SEGRETA", "RISERVATA", "conto-demo", "XXX", self.base):
            self.assertNotIn(secret, text)
        self.assertRegex(self.logs[0], r"^CUORE scrittura: op=aggiungi_manuale esito=200 backup=creato$")

    def test_interfaccia_con_modifiche_sui_dati_reali(self):
        s, add = self.op("aggiungi_manuale", SPESA)
        self.op("aggiungi_manuale", dict(SPESA, amountMinor=-100, description="Altro"))
        self.op("annulla_manuale", {"id": add["risultato"]["id"], "motivo": "doppione"})
        self.op("imposta_budget", {"scope": "total", "target": None, "validFrom": TODAY[:7], "currency": "EUR", "amountMinor": 30000})
        feed = self.feed()
        with open(os.path.join(APP, "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        paths = {n: os.path.join(self.base, n) for n in ("page.js", "h.js", "feed.json")}
        for n, text in (("page.js", script), ("h.js", HARNESS), ("feed.json", json.dumps(feed))):
            with open(paths[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        out = {}
        for meta in ("cuore-scritture", "nessuno"):
            r = subprocess.run([NODE, paths["h.js"], paths["page.js"], paths["feed.json"], meta], capture_output=True,
                               text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            out[meta] = json.loads(r.stdout)
        on, off = out["cuore-scritture"], out["nessuno"]
        for k in ("movimenti", "budget"):
            self.assertIn(">dati reali<", on[k])                       # modalità nella riga di stato
            self.assertIn(">sola lettura<", off[k])
            for bad in ("undefined", "NaN", "[object"):
                self.assertNotIn(bad, on[k], (k, bad))
        self.assertIn("Inseriti a mano", on["movimenti"])
        self.assertIn("annullato", on["movimenti"])
        self.assertIn('data-sheet="annulla"', on["movimenti"])
        self.assertIn("+ Spesa", on["movimenti"])
        self.assertIn("Inseriti a mano", off["movimenti"])                    # A5: visibili anche in sola lettura
        self.assertNotIn("+ Spesa", off["movimenti"])
        self.assertNotIn('data-sheet="annulla"', off["movimenti"])
        self.assertIn("+ Limite", on["budget"])
        self.assertIn("300,00", off["budget"])
        self.assertNotIn("+ Limite", off["budget"])


HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {}, meta = process.argv[4];
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set,
  location: { hash: "", protocol: "file:" }, history: { replaceState() {} }, matchMedia: () => ({ matches: false, addEventListener() {} }),
  addEventListener() {}, scrollTo() {},
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()), querySelectorAll: () => [],
    querySelector: sel => sel.includes(meta) ? {} : null, addEventListener() {} } };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const C = ctx.CUORE;
C.MONEY_STATE.mode = "real"; C.MONEY_STATE.feed = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
console.log(JSON.stringify({ movimenti: C.viewHtml(C.parse("money/movimenti")), budget: C.viewHtml(C.parse("money/budget")) }));
"""


class PromemoriaDalPonte(unittest.TestCase):
    """Impostazioni dei promemoria e appunti Finance dal ponte reale: file SINTETICI in una cartella temporanea disposta
    come quella vera (`<base>/CUORE/promemoria.json`, `<base>/CUORE/finance/appunti.jsonl`). Node non serve."""

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="cuore-promemoria-ponte-test-"))
        self.addCleanup(shutil.rmtree, self.base)
        self.settings = os.path.join(self.base, "CUORE", "promemoria.json")
        self.notes = os.path.join(self.base, "CUORE", "finance", "appunti.jsonl")
        self.logs = []
        self.writer = W.Scrittore(os.path.join(self.base, "assente.sqlite3"), os.path.join(self.base, "p.jsonl"), None,
                                  log=self.logs.append, settings_path=self.settings, notes_path=self.notes)
        httpd = serve.ThreadingHTTPServer((serve.BIND, 0), None)
        self.port = httpd.server_address[1]
        httpd.RequestHandlerClass = serve.make_handler(self.port, feed_fn=lambda: {"schema": "x"}, writer=self.writer,
                                                       reminders_fn=serve.settings_fn(self.settings))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.rid = 0

    req = PonteRealeConScritture.req
    op = PonteRealeConScritture.op

    def test_impostazioni_dall_app(self):
        s, doc = self.req("GET", "/api/promemoria")
        self.assertEqual((s, doc["schema"], doc["file"], doc["attivo"]), (200, "cuore.promemoria.impostazioni/1", "assente", False))
        self.assertFalse(os.path.exists(os.path.dirname(self.settings)))                       # leggere non crea nulla
        change = {"attivo": True, "promemoria": {"fin_contanti": False, "sch_voti": {"attivo": True, "momenti": ["sera"]}}}
        s, doc = self.op("promemoria_imposta", change, rid="impost-0001")
        self.assertEqual(s, 200, doc)
        rows = {r["chiave"]: r for r in doc["risultato"]["promemoria"]}
        self.assertEqual((rows["fin_contanti"]["acceso"], rows["sch_voti"]["momenti"]), (False, ["sera"]))
        self.assertIsNotNone(rows["sch_voti"]["prossimo_controllo"])
        self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o600)
        self.assertEqual(self.op("promemoria_imposta", change, rid="impost-0001")[1]["ripetuta"], True)
        self.assertEqual(self.op("promemoria_imposta", {"attivo": False}, rid="impost-0001")[0], 409)
        self.assertEqual(self.op("promemoria_imposta", {"promemoria": {"fin_contanti": {"momenti": ["mattina"]}}})[0], 422)
        self.assertEqual(self.op("promemoria_imposta", {"attivo": False}, rid="")[0], 400)        # requestId obbligatorio
        s, st = self.req("POST", "/api/scritture/esegui", {"op": "promemoria_stato", "input": {}})  # lettura: senza requestId
        self.assertEqual((s, st["risultato"]["file"], st["risultato"]["attivo"]), (200, "presente", True))
        self.assertEqual(self.req("GET", "/api/promemoria")[1]["promemoria"], st["risultato"]["promemoria"])
        self.assertFalse(serve.promemoria.read_settings(self.settings)[0]["promemoria"]["fin_contanti"]["attivo"])

    def test_appunti_finance(self):
        s, st = self.op("appunti_stato", {})
        self.assertEqual((s, st["risultato"]["appunti"]), (200, []))
        self.assertFalse(os.path.exists(os.path.dirname(self.notes)))
        s, a = self.op("appunti_scrivi", {"tipo": "crea", "testo": "Comprare il casco SEGRETO", "categoria": "da_comprare",
                                          "quando": "2026-11-05T10:00"}, rid="appunto-001")
        self.assertEqual(s, 200, a)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.notes)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.notes).st_mode), 0o600)
        self.assertEqual(self.op("appunti_scrivi", {"tipo": "crea", "testo": "Comprare il casco SEGRETO", "categoria": "da_comprare",
                                                    "quando": "2026-11-05T10:00"}, rid="appunto-001")[1]["ripetuta"], True)
        aid = a["risultato"]["id"]
        st = self.op("appunti_stato", {})[1]["risultato"]
        self.assertEqual((len(st["appunti"]), st["appunti"][0]["invio_previsto"]), (1, None))      # promemoria spenti
        self.op("promemoria_imposta", {"attivo": True})
        st = self.op("appunti_stato", {})[1]["risultato"]
        self.assertIsNone(st["appunti"][0]["invio_previsto"])                    # generale acceso, appunti ancora spenti
        self.op("promemoria_imposta", {"promemoria": {"fin_appunti": True}})
        st = self.op("appunti_stato", {})[1]["risultato"]
        self.assertEqual(st["appunti"][0]["invio_previsto"], "2026-11-05T15:00:00+01:00")
        self.assertEqual(self.op("appunti_scrivi", {"tipo": "promemoria", "id": aid, "attivo": False})[0], 200)
        self.assertEqual(self.op("appunti_scrivi", {"tipo": "annulla", "id": aid, "motivo": "comprato"})[0], 200)
        self.assertEqual(self.op("appunti_scrivi", {"tipo": "completa", "id": aid})[0], 422)
        text = "\n".join(self.logs)
        self.assertNotIn("SEGRETO", text)
        self.assertIn("op=appunti_scrivi esito=200", text)

    def test_protezioni_e_percorsi(self):
        body = {"op": "promemoria_imposta", "input": {"attivo": True}, "requestId": "estranea-01"}
        for h in ({"Host": "evil.example"}, {"Origin": None}, {"Origin": "https://evil.example"},
                  {"Sec-Fetch-Site": "cross-site"}, {"Tailscale-User-Login": "a@b"}):
            self.assertEqual(self.req("POST", "/api/scritture/esegui", body, h)[0], 403, h)
        self.assertFalse(os.path.exists(self.settings))
        os.makedirs(os.path.dirname(self.notes), mode=0o755)
        self.assertEqual(self.op("appunti_scrivi", {"tipo": "crea", "testo": "x"})[0], 403)        # cartella aperta
        icloud = os.path.join(os.path.expanduser("~"), "Library", "Mobile Documents", "x", "promemoria.json")
        for settings in (os.path.join(W.REPO_ROOT, "promemoria.json"), icloud):
            w = W.Scrittore("x", "x", None, log=self.logs.append, settings_path=settings, notes_path=self.notes)
            self.assertEqual(w.execute({"op": "promemoria_imposta", "input": {"attivo": True}, "requestId": "percorso-01"})[0], 403)
            self.assertIn("errore", serve.settings_fn(settings)())
        self.assertFalse(os.path.exists(os.path.join(W.REPO_ROOT, "promemoria.json")))
        bare = W.Scrittore("x", "x", None, log=self.logs.append)
        self.assertEqual(bare.execute({"op": "promemoria_stato", "input": {}})[0], 403)


class SenzaOpzione(unittest.TestCase):
    def test_ponte_resta_in_sola_lettura(self):
        httpd = serve.ThreadingHTTPServer((serve.BIND, 0), None)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = serve.make_handler(port, feed_fn=lambda: {"schema": "x"})
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        c = http.client.HTTPConnection(serve.BIND, port, timeout=30)
        h = {"Host": "127.0.0.1:%d" % port, "Origin": "http://127.0.0.1:%d" % port, "Content-Type": "application/json"}
        c.request("POST", "/api/scritture/esegui", body=b'{"op":"consulta","requestId":"abcdefgh"}', headers=h)
        self.assertEqual(c.getresponse().status, 405)
        c.close()
        c = http.client.HTTPConnection(serve.BIND, port, timeout=30)
        c.request("GET", "/api/scritture", headers={"Host": "127.0.0.1:%d" % port})
        self.assertFalse(json.loads(c.getresponse().read())["attive"])
        c.close()
        c = http.client.HTTPConnection(serve.BIND, port, timeout=30)
        c.request("GET", "/", headers={"Host": "127.0.0.1:%d" % port})
        self.assertNotIn(b'<meta name="cuore-scritture"', c.getresponse().read())
        c.close()

    def test_archivio_in_icloud_o_nel_repository_rifiutato(self):
        icloud = os.path.join(os.path.expanduser("~"), "Library", "Mobile Documents", "x", "personale.jsonl")
        for bad in (os.path.join(W.REPO_ROOT, "personale.jsonl"), icloud, "relativo.jsonl"):
            with self.assertRaises(W.Rifiuto):
                W.school_archive(bad, create=True)
        self.assertFalse(os.path.exists(os.path.join(W.REPO_ROOT, "personale.jsonl")))
        self.assertEqual(serve.main(["--scritture", "--porta", "0", "--school-personale",
                                     os.path.join(W.REPO_ROOT, "personale.jsonl")]), 2)

    def test_comando_finance_con_permessi(self):
        cmd = W.finance_command("node", "/x/finance/db/cloud-finance.sqlite3")
        self.assertIn("--permission", cmd)
        self.assertEqual([a for a in cmd if a.startswith("--allow-fs-write")],
                         ["--allow-fs-write=/x/finance/backup", "--allow-fs-write=/x/finance/backup/*"])
        self.assertEqual(cmd[-3:], ["esegui", "--db", "/x/finance/db/cloud-finance.sqlite3"])


if __name__ == "__main__":
    unittest.main()
