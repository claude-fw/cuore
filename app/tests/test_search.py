"""Ricerca globale (`cuore_search.py`, `cuore.search/1`), endpoint `/api/search`, Finance nel registro degli eventi del
ponte, strumento MCP `cuore_cerca` e sezione «Sistema» della dashboard. Solo feed SINTETICI in memoria."""
import datetime as dt, http.client, json, os, re, shutil, subprocess, sys, tempfile, threading, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(APP, "bridge"))
import cuore_events as E  # noqa: E402
import cuore_mcp as M  # noqa: E402
import cuore_search as S  # noqa: E402
import serve  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 25, 12, tzinfo=UTC)


def block(bid, titolo, dati, stato="dati"):
    return {"id": bid, "titolo": titolo, "stato": stato, "provenienza": {}, "dati": dati}


SCHOOL = {"schema": "cuore.school.feed/1", "materie": {"fisica": "Fisica", "italiano": "Italiano"}, "pagine": {
    "verifiche": {"blocchi": [block("verifiche_future", "Verifiche future", [
        {"materia": "fisica", "uid": "cvv:9999999", "data": "2026-10-28", "testo": "Verifica sul moto rettilineo", "affidabilita": "alta"},
        {"materia": "italiano", "uid": "cvv:8888888", "data": "2026-11-03", "testo": "Tema: perché leggere"}])]},
    "home": {"blocchi": [block("prossimi_impegni", "Prossimi impegni", [
        {"data": "2026-10-27", "materia": "fisica", "tipo": "compito", "oggetto": "Esercizi sul moto", "motivo": "agenda"}]),
        block("attenzione", "Attenzione", {"dettaglio": "testo", "materie_per_priorita": {"alta": ["fisica"]}}),
        block("vuoto", "Vuoto", None, stato="vuoto_valido")]},
    "studio": {"blocchi": [block("studio_fisica", "Studio di fisica", {"obiettivi": [
        {"uid": "cvv:7777777", "data": "2026-10-28", "testo": "Ripassare il moto uniformemente accelerato"}]})]}}}


def ok(data):
    return {"ok": True, "contractVersion": 1, "operation": "x", "data": data}


MONEY = {"schema": "cuore.money.feed/1", "operazioni": {
    "accounts": ok([{"id": "rev-eur", "name": "Revolut DEMO", "type": "current", "currency": "EUR", "closed": False, "lastMovementOn": "2026-10-20"}]),
    "categories": ok({"items": [{"id": "spesa", "name": "Spesa alimentare", "budgetGroup": "needs"}]}),
    "planning": ok({"goals": [{"name": "Fondo moto", "status": "active", "deadline": "2027-06-30"}],
                    "purchases": [{"title": "Casco moto", "status": "planned"}]}),
    "transactions": ok({"items": [{"date": "2026-10-20", "status": "booked", "accountId": "rev-eur", "amountMinor": -4520,
                                   "currency": "EUR", "description": "Supermercato Esempio"},
                                  {"date": "2026-10-21", "status": "pending", "accountId": "rev-eur", "amountMinor": -999,
                                   "currency": "EUR", "description": "Benzina moto"}]}),
    "data_status": ok({"accounts": [{"accountId": "rev-eur", "lastImportAt": "2026-10-22T09:00:00Z"}],
                       "anomalies": [{"code": "stale_data", "severity": "warning", "accountId": "rev-eur", "message": "x"}]})}}


def events_doc():
    return E.build(now=NOW, money_feed=MONEY)


def bank_events(tmp):
    path = os.path.join(tmp, "banca-stato.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"schema": "cuore.bank.sync_status/1", "generato": NOW.isoformat(), "database": "copia", "connessioni": [
            {"conto": "rev-eur", "banca": {"nome": "Revolut", "paese": "LT"},
             "consenso": {"scade_il": (NOW.date() + dt.timedelta(days=5)).isoformat()},
             "ultimo_tentativo": {"at": (NOW - dt.timedelta(hours=1)).isoformat(), "esito": "importata", "errore": None, "nuovi": 2}}]}, f)
    return E.build(now=NOW, money_feed=MONEY, bank_status_path=path)


class Ricerca(unittest.TestCase):
    def run_search(self, q, **kw):
        args = dict(school_fn=lambda: SCHOOL, money_fn=lambda: MONEY, events_fn=events_doc)
        args.update(kw)
        return S.search(q, **args)

    def test_tutte_le_fonti_parole_e_accenti(self):
        d = self.run_search("moto")
        self.assertEqual(d["schema"], "cuore.search/1")
        by = {(r["modulo"], r["titolo"]) for r in d["risultati"]}
        for want in (("school", "Verifica sul moto rettilineo"), ("school", "Esercizi sul moto"),
                     ("school", "Ripassare il moto uniformemente accelerato"), ("money", "Fondo moto"), ("money", "Casco moto"),
                     ("money", "Benzina moto")):
            self.assertIn(want, by, want)
        self.assertEqual({k: v["stato"] for k, v in d["fonti"].items()}, {"school": "letta", "classroom": "non_collegata", "money": "letta", "eventi": "letta"})
        self.assertEqual([r["titolo"] for r in self.run_search("PERCHE leggere")["risultati"]], ["Tema: perché leggere"])
        self.assertEqual(self.run_search("moto fisica")["totale"], 3)                        # tutte le parole
        hit = next(r for r in d["risultati"] if r["titolo"] == "Verifica sul moto rettilineo")
        self.assertEqual((hit["rotta"], hit["data"], hit["provenienza"]),
                         ("school/verifiche", "2026-10-28", {"fonte": "pagina verifiche", "blocco": "verifiche_future"}))
        self.assertEqual(self.run_search("conto")["totale"], 0)                              # «conto» del dettaglio non conta
        self.assertEqual([r["titolo"] for r in self.run_search("rev-eur 45,20")["risultati"]], ["Supermercato Esempio"])
        mov = next(r for r in d["risultati"] if r["titolo"] == "Benzina moto")
        self.assertEqual((mov["rotta"], mov["dettaglio"]), ("money/movimenti", "-9,99 EUR · conto rev-eur · in attesa"))

    def test_eventi_e_ordine(self):
        d = self.run_search("anomalia dei dati")
        self.assertEqual([(r["modulo"], r["rotta"]) for r in d["risultati"]], [("eventi", "cuore/eventi")])
        dates = [r["data"] for r in self.run_search("moto")["risultati"] if r["data"]]
        self.assertTrue(dates)

    def test_nessun_identificativo_interno_nei_risultati(self):
        text = json.dumps(self.run_search("moto", limit=200))
        for bad in ("cvv:", "uid", "hash", "registry_version", "_testo", "_titolo"):
            self.assertNotIn(bad, text, bad)

    def test_movimenti_esclusi_su_richiesta_e_fonti_dichiarate(self):
        d = self.run_search("moto", movements=False)
        self.assertNotIn("Benzina moto", [r["titolo"] for r in d["risultati"]])
        self.assertEqual(d["fonti"]["money"].get("movimenti"), "esclusi")

        def boom():
            raise RuntimeError("/percorso/privato")
        d = self.run_search("moto", money_fn=boom, events_fn=None)
        self.assertEqual(d["fonti"]["money"], {"stato": "non_disponibile", "motivo": "RuntimeError"})
        self.assertEqual(d["fonti"]["eventi"], {"stato": "non_collegata"})
        self.assertNotIn("percorso", json.dumps(d))
        d = self.run_search("moto", events_fn=lambda: {"schema": "cuore.events/1", "eventi": [], "errori": [{"codice": "x"}]})
        self.assertEqual(d["fonti"]["eventi"]["stato"], "non_disponibile")              # registro in errore: mai «0 risultati»

    def test_domande_non_valide_e_limite(self):
        for q in ("", "a", "x" * 81, "!!"):
            with self.assertRaises(S.QueryError, msg=q):
                self.run_search(q)
        d = self.run_search("moto", limit=2)
        self.assertEqual((len(d["risultati"]), d["omessi"], d["totale"]), (2, d["totale"] - 2, d["totale"]))


class Ponte(unittest.TestCase):
    def server(self, **kw):
        httpd = serve.ThreadingHTTPServer((serve.BIND, 0), None)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = serve.make_handler(port, **kw)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port

    def get(self, port, path, method="GET", host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request(method, path, headers={"Host": host or "127.0.0.1:%d" % port})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_api_search_con_le_protezioni_del_ponte(self):
        port = self.server(feed_fn=lambda: SCHOOL, money_fn=lambda: MONEY, events_fn=events_doc)
        code, body = self.get(port, "/api/search?q=moto%20rettilineo")
        d = json.loads(body)
        self.assertEqual((code, [r["titolo"] for r in d["risultati"]]), (200, ["Verifica sul moto rettilineo"]))
        self.assertEqual(self.get(port, "/api/search?q=a")[0], 400)
        self.assertEqual(self.get(port, "/api/search?q=moto&limite=tanti")[0], 400)
        self.assertEqual(self.get(port, "/api/search?q=moto", method="POST")[0], 405)
        self.assertEqual(self.get(port, "/api/search?q=moto", host="evil.example:%d" % port)[0], 403)
        bad = events_doc()
        bad["eventi"][0]["dettaglio"]["messaggio"] = "testo libero"
        port = self.server(feed_fn=lambda: SCHOOL, money_fn=None, events_fn=lambda: bad)
        d = json.loads(self.get(port, "/api/search?q=moto")[1])
        self.assertEqual((d["fonti"]["eventi"]["stato"], d["fonti"]["money"]["stato"]), ("non_disponibile", "non_collegata"))

    def test_finance_nel_registro_degli_eventi_del_ponte(self):
        calls = []

        def build(**kw):
            calls.append(kw.get("money_feed"))
            return {"schema": "x"}
        orig = E.build
        E.build = build
        try:
            serve.events_build(lambda: MONEY)
            serve.events_build(lambda: (_ for _ in ()).throw(RuntimeError("x")))
            serve.events_build(None)
        finally:
            E.build = orig
        self.assertEqual(calls, [MONEY, {}, None])                 # letto | illeggibile | non collegato
        doc = E.build(now=NOW, money_feed=MONEY)
        self.assertEqual(doc["fonti"]["finance.data_status"]["stato"], "ok")
        self.assertEqual(E.build(now=NOW, money_feed={})["fonti"]["finance.data_status"]["stato"], "illeggibile")


class StrumentoMcp(unittest.TestCase):
    def test_cuore_cerca_movimenti_solo_su_richiesta(self):
        srv = M.build_server(events_doc, None, school_fn=lambda: SCHOOL, money_fn=lambda: MONEY)

        def call(args):
            r = srv._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "cuore_cerca", "arguments": args}})
            return json.loads(r["result"]["content"][0]["text"])
        self.assertNotIn("Benzina moto", [r["titolo"] for r in call({"domanda": "moto"})["risultati"]])
        self.assertIn("Benzina moto", [r["titolo"] for r in call({"domanda": "moto", "movimenti": True})["risultati"]])
        self.assertEqual(call({"domanda": "x"})["errore"], "domanda_non_valida")


HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {};
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set, Map, encodeURIComponent, setTimeout, clearTimeout,
  location: { hash: "", protocol: "file:", host: "" }, history: { replaceState() {}, pushState() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, scrollTo() {},
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()), querySelectorAll: () => [], querySelector: () => null, addEventListener() {} } };
ctx.window = ctx; vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const C = ctx.CUORE, data = JSON.parse(fs.readFileSync(process.argv[3], "utf8")), out = {};
const view = r => C.viewHtml(C.parse(r));
(async () => {
  out.nav = Object.keys(C.APPS);
  out.file_cerca = view("cuore/cerca"); C.SEARCH_STATE.q = "moto"; await C.runSearch(); out.file_cerca_q = view("cuore/cerca");
  out.file_eventi = view("cuore/eventi");
  ctx.location.protocol = "http:"; ctx.location.host = "127.0.0.1:8765";
  ctx.fetch = async url => ({ ok: true, status: 200, json: async () => data.search });
  await C.runSearch(); out.cerca = view("cuore/cerca");
  ctx.fetch = async () => ({ ok: false, status: 400, json: async () => ({ schema: "cuore.search/1", errore: "domanda_non_valida", messaggio: "la ricerca richiede da 2 a 80 caratteri" }) });
  await C.runSearch(); out.cerca_err = view("cuore/cerca");
  const pending = [], reply = d => ({ ok: true, status: 200, json: async () => ({ schema: "cuore.search/1", domanda: d, risultati: [], totale: 0, omessi: 0, fonti: {} }) });
  ctx.fetch = () => new Promise(res => pending.push(res));
  C.SEARCH_STATE.q = "vecchia"; const p1 = C.runSearch();
  C.SEARCH_STATE.q = "nuova"; const p2 = C.runSearch();
  pending[1](reply("nuova")); await p2;
  pending[0](reply("vecchia")); await p1;                                   // arriva dopo: non deve sostituire la nuova
  out.ultima = C.SEARCH_STATE.doc.domanda;
  C.EVENTS_STATE.doc = data.events; out.eventi = view("cuore/eventi");
  C.SCHOOL_STATE.mode = "real"; C.SCHOOL_STATE.feed = data.school; C.MONEY_STATE.mode = "error"; C.MONEY_STATE.error = "ponte locale: risposta 503";
  out.diag = view("cuore/diagnostica");
  C.EVENTS_STATE.doc = data.events_bank; out.diag_banca = view("cuore/diagnostica"); out.eventi_banca = view("cuore/eventi");
  console.log(JSON.stringify(out));
})();
"""


class Dashboard(unittest.TestCase):
    def test_sezione_sistema_cerca_eventi_diagnostica(self):
        node = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
        if not node:
            self.skipTest("node non disponibile")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(APP, "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        school = dict(SCHOOL, esito="completo", generato="2026-10-25T10:00:00+00:00", sola_lettura_verificata=True,
                      freschezza={"stato": "aggiornati"}, errori=[])
        found = S.search("moto", school_fn=lambda: SCHOOL, money_fn=lambda: MONEY, events_fn=events_doc)
        found["risultati"] += [dict(found["risultati"][0], titolo="Dispensa moto.pdf", link="https://drive.google.com/file/d/x/view"),
                               dict(found["risultati"][0], titolo="Link pericoloso", link="javascript:alert(1)")]
        data = {"search": found,
                "events": events_doc(), "school": school, "events_bank": bank_events(tmp)}
        p = {n: os.path.join(tmp, n) for n in ("page.js", "h.js", "data.json")}
        for n, text in (("page.js", script), ("h.js", HARNESS), ("data.json", json.dumps(data))):
            with open(p[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        r = subprocess.run([node, p["h.js"], p["page.js"], p["data.json"]], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        v = json.loads(r.stdout)
        self.assertEqual(v["nav"], ["school", "money", "cuore"])
        self.assertIn("Cerca in tutto CUORE", v["file_cerca"])
        self.assertIn("ponte locale", v["file_cerca_q"])                                    # da file: mai risultati inventati
        self.assertIn("Il registro si legge dal ponte locale", v["file_eventi"])
        c = v["cerca"]
        for w in ("Verifica sul moto rettilineo", 'data-route="school/verifiche"', "Benzina moto", 'data-route="money/movimenti"',
                  "Fonti: School: letta", 'id="q"', 'value="moto"'):
            self.assertIn(w, c, w)
        self.assertIn('href="https://drive.google.com/file/d/x/view" target="_blank" rel="noopener noreferrer"', c)
        self.assertEqual(c.count("Apri originale"), 1)                                      # mai un link non https
        self.assertNotIn("javascript:", c)
        self.assertIn("la ricerca richiede da 2 a 80 caratteri", v["cerca_err"])
        self.assertEqual(v["ultima"], "nuova")                                              # risposta superata ignorata
        e = v["eventi"]
        for w in ("Fonti del registro", "finance.data_status", "anomalia dei dati", "Duplicati scartati", 'data-evt-filter="finance"'):
            self.assertIn(w, e, w)
        b = v["diag_banca"]
        for w in ("rev-eur (Revolut)", "importata, collaudo su copia", "consenso in scadenza (5 giorni)", "parziale"):
            self.assertIn(w, b, w)
        self.assertNotIn("nessun collegamento automatico configurato", b)
        self.assertIn("sincronizzazione bancaria", v["eventi_banca"])
        d = v["diag"]
        for w in ("Lettura dei dati School", "aggiornati", "Lettura del database Finance", "ponte locale: risposta 503",
                  "Collegamento bancario (Revolut, MPS)", "nessun collegamento: i dati arrivano dagli estratti importati",
                  "attivo su 127.0.0.1:8765", "Sincronizza tutto", "Cosa fare:"):
            self.assertIn(w, d, w)
        rows = dict(re.findall(r'<li class="srow">.*?<div class="t">([^<]+)</div>.*?<span class="st [a-z]*">([^<]+)</span>', d))
        self.assertEqual(rows["Collegamento bancario (Revolut, MPS)"], "non configurato")   # mai «funzionante» senza prova
        self.assertEqual(rows["Lettura del database Finance"], "errore")
        self.assertEqual(rows["Ponte locale"], "sì, funziona")
        self.assertEqual(rows["Assistente allo studio in Claude"], "non verificato")
        for page in v.values():
            for bad in ("undefined", "NaN", "[object", "cvv:"):
                self.assertNotIn(bad, json.dumps(page), bad)


class AvvioSulMac(unittest.TestCase):
    def test_script_di_avvio_solo_locale_e_senza_servizi(self):
        path = os.path.join(APP, "CUORE.command")
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual(subprocess.run(["/bin/bash", "-n", path]).returncode, 0)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('URL="http://127.0.0.1:${PORT}/"', text)
        self.assertIn("app/bridge/serve.py", text)
        for bad in ("launchctl", "LaunchAgents", "sudo", "pmset", "defaults write", "0.0.0.0", "--tailnet", "nohup", "disown"):
            self.assertNotIn(bad, text, bad)


if __name__ == "__main__":
    unittest.main()
