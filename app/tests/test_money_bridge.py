"""CUORE Money: adattatore di sola lettura sul motore Finance (cuore.money.feed/1).
Solo database SINTETICI costruiti in una cartella temporanea con gli esempi del repository: il database reale non viene
mai aperto. Esecuzione dalla radice: python3 -m unittest discover -s app/tests"""
import hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
FINANCE = os.path.normpath(os.path.join(APP, "..", "finance"))
sys.path.insert(0, os.path.join(APP, "bridge"))
import cuore_money as CM  # noqa: E402

NODE = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
TODAY = "2026-01-31"
GOAL = "Fondo SINTETICO"
PURCHASE = "Zaino SINTETICO"
OPS = {"accounts", "overview", "summary", "categories", "planning", "balances", "data_status", "obligations", "transactions",
       "trend", "manual", "budgets", "daily"}


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build_demo(path):
    """Database Finance sintetico: migrazioni, due conti, esempi CSV, regole e classificazione, un obiettivo con un
    accantonamento e due acquisti pianificati (uno senza stima)."""
    steps = [["migrate"],
             ["account:add", "--id", "conto", "--name", "Conto DEMO", "--type", "current", "--currency", "EUR"],
             ["account:add", "--id", "carta", "--name", "Carta DEMO", "--type", "card", "--currency", "EUR"],
             ["import", "--account", "conto", "--format", "cloud-finance-csv", "--file", "examples/sintetici/conto-demo.csv"],
             ["import", "--account", "carta", "--format", "cloud-finance-csv", "--file", "examples/sintetici/carta-demo.csv"],
             ["import", "--account", "conto", "--format", "cloud-finance-balances-csv", "--file",
              "examples/sintetici/saldi-conto-demo.csv"],
             ["rules:publish", "--file", "examples/sintetici/regole-demo.json"], ["classify"],
             ["counterparty:add", "--id", "amico", "--name", "Amico DEMO"],
             ["obligation:add", "--counterparty", "amico", "--direction", "receivable", "--amount", "15.00", "--currency",
              "EUR", "--date", "2026-01-10", "--description", "Prestito DEMO", "--due", "2026-02-10"]]
    def cli(s):
        r = subprocess.run([NODE, os.path.join(FINANCE, "src", "cli", "main.ts"), "--db", path] + s, cwd=FINANCE,
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError("costruzione del database sintetico: %s -> %s" % (s[0], r.stderr[:300]))
        return r.stdout
    for s in steps:
        cli(s)
    goal = json.loads(cli(["goal:add", "--name", GOAL, "--target", "200.00", "--currency", "EUR", "--deadline", "2026-06-30"]))
    cli(["goal:allocate", "--goal", goal["id"], "--amount", "50.00", "--date", "2026-01-15"])
    cli(["purchase:add", "--title", PURCHASE, "--currency", "EUR", "--estimate", "80.00", "--priority", "2", "--goal", goal["id"]])
    cli(["purchase:add", "--title", "Idea SINTETICA", "--currency", "EUR"])


@unittest.skipUnless(NODE, "node non disponibile")
class Adattatore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "demo.sqlite3")
        build_demo(cls.db)
        for x in ("-wal", "-shm"):                                    # stato di partenza come il database reale
            if os.path.exists(cls.db + x):
                raise AssertionError("file accessori dopo la costruzione")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_feed_reale_completo_e_database_intatto(self):
        before = sha(self.db)
        f = CM.build_feed(self.db, today=TODAY)
        self.assertTrue(f["sola_lettura_verificata"], f["errori"])
        self.assertEqual(f["errori"], [])
        self.assertEqual(set(f["operazioni"]), OPS)
        self.assertTrue(all(e["ok"] for e in f["operazioni"].values()))
        self.assertEqual(f["esito"], "completo")
        self.assertNotIn("pianificazione", f)                                # una sola fonte: la consultazione
        self.assertEqual(f["operazioni"]["overview"]["contractVersion"], 1)
        self.assertEqual(sha(self.db), before)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["demo.sqlite3"])    # nessun -wal/-shm creato

    def test_serie_mensile(self):
        """`trend`: 12 mesi di calendario in ordine, l'ultimo fino a oggi; solo entrate, spesa e categorie."""
        f = CM.build_feed(self.db, today=TODAY)
        months = f["operazioni"]["trend"]["data"]["months"]
        self.assertEqual(len(months), 12)
        self.assertEqual(months[-1]["month"], TODAY[:7])
        self.assertEqual(months[-1]["to"], TODAY)
        self.assertEqual([m["month"] for m in months], sorted(m["month"] for m in months))
        last = months[-1]["currencies"][0]
        summ = f["operazioni"]["summary"]["data"]["currencies"][0]
        self.assertEqual((last["netSpending"], last["spendingByCategory"]), (summ["netSpending"], summ["spendingByCategory"]))
        self.assertEqual(last["income"], f["operazioni"]["overview"]["data"]["currencies"][0]["income"]["total"])
        bad = json.loads(json.dumps(f["operazioni"]["trend"]["data"]))
        bad["months"][-1]["currencies"][0]["description"] = "x"
        self.assertIn("campi oltre il contratto minimo", " ".join(CM.validate("trend", bad)))
        bad["months"] = bad["months"][::-1]
        self.assertIn("trend.months: mesi ripetuti o non in ordine", CM.validate("trend", bad))

    def test_serie_giornaliera(self):
        """`daily`: dal primo giorno del mese precedente a oggi; i giorni del mese sommano la spesa del riepilogo."""
        f = CM.build_feed(self.db, today=TODAY)
        d = f["operazioni"]["daily"]["data"]
        self.assertEqual((d["from"], d["to"]), ("2025-12-01", TODAY))
        self.assertEqual(f["periodo"]["giorni"], {"from": "2025-12-01", "to": TODAY})
        summ = f["operazioni"]["summary"]["data"]["currencies"][0]
        month = [c for x in d["days"] if x["date"].startswith(TODAY[:7]) for c in x["currencies"] if c["currency"] == summ["currency"]]
        self.assertEqual(sum(c["netSpending"] for c in month), summ["netSpending"])
        bad = json.loads(json.dumps(d))
        bad["days"][0]["currencies"][0]["description"] = "x"
        self.assertIn("campi diversi da quelli attesi", " ".join(CM.validate("daily", bad)))
        bad["days"] = bad["days"][::-1] + bad["days"][:1]
        self.assertIn("daily.days: giorni ripetuti o non in ordine", CM.validate("daily", bad))

    def test_contratto_minimo(self):
        f = CM.build_feed(self.db, today=TODAY)
        tx = f["operazioni"]["transactions"]["data"]
        self.assertTrue(tx["items"])
        self.assertLessEqual(len(tx["items"]), 50)
        for t in tx["items"]:
            self.assertEqual(set(t) - {"id", "date", "status", "accountId", "amountMinor", "currency", "description", "kind",
                                       "categoryId", "excluded"}, set())
            self.assertIsInstance(t["amountMinor"], int)                  # unita' minime, mai float
        for a in f["operazioni"]["accounts"]["data"]:
            self.assertNotIn("profile", a)                               # nessun dato di titolarita'
        ob = f["operazioni"]["obligations"]["data"]["items"]
        self.assertEqual(len(ob), 1)
        self.assertEqual(set(ob[0]), {"direction", "currency", "dueOn", "daysToDue", "status", "overdue", "residualMinor"})
        text = json.dumps(f)
        self.assertNotIn('"counterparty"', text)
        self.assertNotIn("Amico DEMO", text)                             # nessun nome di controparte
        self.assertNotIn("Prestito DEMO", text)                          # nessuna descrizione di crediti/debiti
        self.assertIsNone(re.search(r"IT\d{2}[A-Z]\d{10}", text))       # nessun IBAN in chiaro

    def test_categorie_e_gruppi_di_budget(self):
        s = CM.build_feed(self.db, today=TODAY)["operazioni"]["summary"]
        self.assertTrue(s["ok"], s)
        c = s["data"]["currencies"][0]
        self.assertEqual(set(c), CM.SUMMARY_FIELDS)
        self.assertEqual(set(c["spendingByBudgetGroup"]), CM.BUDGET_GROUPS)
        self.assertEqual(sum(c["spendingByCategory"].values()), c["netSpending"])        # nessun valore ricostruito
        self.assertEqual(sum(c["budgetSharesBp"].values()), 10000)
        self.assertGreater(c["unclassifiedCount"], 0)                                   # incompleto: segnalato, non nascosto

    def test_categorie_con_nome_e_gruppo(self):
        items = CM.build_feed(self.db, today=TODAY)["operazioni"]["categories"]["data"]["items"]
        by = {c["id"]: c for c in items}
        self.assertEqual((by["spesa-alimentare"]["name"], by["spesa-alimentare"]["budgetGroup"]), ("Spesa alimentare", "needs"))
        self.assertTrue(any(c["budgetGroup"] is None for c in items))         # senza gruppo = «unassigned» nel riepilogo

    def test_obiettivi_e_acquisti_dalla_consultazione(self):
        env = CM.build_feed(self.db, today=TODAY)["operazioni"]["planning"]
        self.assertEqual((env["ok"], env["contractVersion"]), (True, 1))
        p = env["data"]
        [g] = p["goals"]
        self.assertEqual((g["name"], g["targetMinor"], g["allocatedMinor"], g["progressBp"], g["reached"]),
                         (GOAL, 20000, 5000, 2500, False))
        by = {x["title"]: x for x in p["purchases"]}
        self.assertEqual((by[PURCHASE]["estimatedMinor"], by[PURCHASE]["goalId"]), (8000, g["id"]))
        self.assertIsNone(by["Idea SINTETICA"]["estimatedMinor"])                        # mancante, non zero
        for x in p["purchases"]:
            self.assertEqual(set(x), CM.PURCHASE_FIELDS)                                # niente id di movimenti o Notion

    def test_errore_parziale_spegne_solo_la_sezione(self):
        real = subprocess.run

        def tamper(cmd, **kw):
            d = json.loads(real(cmd, **kw).stdout)
            d["operazioni"]["planning"]["data"]["goals"][0]["targetMinor"] = "200"     # tipo errato
            d["operazioni"]["categories"]["data"]["items"][0]["iban"] = "X"            # campo in piu'
            return subprocess.CompletedProcess(cmd, 0, json.dumps(d), "")
        f = CM.build_feed(self.db, today=TODAY, run=tamper)
        for op in ("planning", "categories"):
            self.assertEqual(f["operazioni"][op]["error"]["code"], "contratto_non_valido")
            self.assertNotIn("data", f["operazioni"][op])
        self.assertTrue(all(e["ok"] for k, e in f["operazioni"].items() if k not in ("planning", "categories")))
        self.assertEqual({e["ambito"] for e in f["errori"]}, {"planning", "categories"})
        self.assertEqual(f["esito"], "parziale")

    def test_summary_non_valido(self):
        real = subprocess.run

        def tamper(cmd, **kw):
            d = json.loads(real(cmd, **kw).stdout)
            d["operazioni"]["summary"]["data"]["currencies"][0]["spendingByBudgetGroup"]["needs"] = 1.5
            return subprocess.CompletedProcess(cmd, 0, json.dumps(d), "")
        f = CM.build_feed(self.db, today=TODAY, run=tamper)
        self.assertEqual(f["operazioni"]["summary"]["error"]["code"], "contratto_non_valido")

    def test_aggiornamento_dei_dati(self):
        a = CM.build_feed(self.db, today=TODAY)["aggiornamento"]
        self.assertEqual(set(a), {"letto_il", "ultimo_movimento", "ultima_importazione", "avvisi_dati_vecchi", "stato"})
        self.assertRegex(a["ultimo_movimento"], r"^2026-01-\d\d$")
        self.assertIn(a["stato"], ("ok", "dati_vecchi"))

    def test_database_senza_movimenti_non_risulta_aggiornato(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        try:
            p = os.path.join(d, "vuoto.sqlite3")
            subprocess.run([NODE, os.path.join(FINANCE, "src", "cli", "main.ts"), "--db", p, "migrate"], cwd=FINANCE,
                           check=True, capture_output=True, timeout=120)
            f = CM.build_feed(p, today=TODAY)
            self.assertTrue(f["sola_lettura_verificata"], f["errori"])
            self.assertEqual((f["aggiornamento"]["stato"], f["aggiornamento"]["ultimo_movimento"]), ("nessun_dato", None))
            self.assertEqual(f["operazioni"]["planning"]["data"], {"goals": [], "purchases": []})   # zero, non errore
            self.assertEqual(f["operazioni"]["categories"]["data"], {"items": []})
            self.assertEqual(f["esito"], "completo")
        finally:
            shutil.rmtree(d)

    def test_contratto_non_valido_non_diventa_dato(self):
        real = subprocess.run

        def tamper(cmd, **kw):
            out = real(cmd, **kw)
            d = json.loads(out.stdout)
            d["operazioni"]["balances"]["data"]["accounts"][0]["balanceMinor"] = 12.5      # float: non ammesso
            d["operazioni"]["transactions"]["data"]["items"][0]["counterparty"] = "X"       # campo in piu'
            return subprocess.CompletedProcess(cmd, 0, json.dumps(d), "")
        f = CM.build_feed(self.db, today=TODAY, run=tamper)
        for op in ("balances", "transactions"):
            self.assertEqual((f["operazioni"][op]["ok"], f["operazioni"][op]["error"]["code"]), (False, "contratto_non_valido"))
            self.assertNotIn("data", f["operazioni"][op])
        self.assertTrue(f["operazioni"]["overview"]["ok"])
        self.assertEqual({e["ambito"] for e in f["errori"]}, {"balances", "transactions"})

    def test_operazioni_mancanti_o_busta_sconosciuta(self):
        real = subprocess.run

        def drop(cmd, **kw):
            d = json.loads(real(cmd, **kw).stdout)
            del d["operazioni"]["overview"]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(d), "")
        f = CM.build_feed(self.db, today=TODAY, run=drop)
        self.assertEqual((f["operazioni"], f["errori"][0]["ambito"]), ({}, "contratto"))
        self.assertEqual(CM.validate("sconosciuta", {}), ["operazione non prevista dal contratto: sconosciuta"])

    def test_scrittura_rifiutata_da_sqlite(self):
        script = os.path.join(self.tmp, "w.ts")
        with open(script, "w", encoding="utf-8") as f:
            f.write('import { ImmutableDb } from %s;\n' % json.dumps(os.path.join(APP, "bridge", "cuore_money.ts")) +
                    'const db = new ImmutableDb(process.argv[2]);\n'
                    'try { await db.batch([{ sql: "CREATE TABLE x(a)", params: [] }]); console.log("SCRITTO"); }'
                    ' catch (e) { console.log("rifiutata"); }\n'
                    'try { await db.all("CREATE TABLE y(a)"); console.log("SCRITTO"); } catch (e) { console.log("rifiutata"); }\n')
        try:
            before = sha(self.db)
            r = subprocess.run([NODE, script, self.db], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.split(), ["rifiutata", "rifiutata"], r.stderr)
            self.assertEqual(sha(self.db), before)
        finally:
            os.remove(script)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["demo.sqlite3"])

    def test_database_assente(self):
        f = CM.build_feed(os.path.join(self.tmp, "manca.sqlite3"), today=TODAY)
        self.assertEqual((f["operazioni"], f["errori"][0]["ambito"], f["sola_lettura_verificata"]), ({}, "database", False))

    def test_wal_non_vuoto_rifiutato(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        try:
            p = os.path.join(d, "copia.sqlite3")
            shutil.copy(self.db, p)
            with open(p + "-wal", "wb") as f:
                f.write(b"\0" * 32)
            f = CM.build_feed(p, today=TODAY)
            self.assertEqual(f["operazioni"], {})
            self.assertIn("-wal non vuoto", f["errori"][0]["messaggio"])
        finally:
            shutil.rmtree(d)

    def test_schema_diverso_segnalato_senza_dati(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        try:
            p = os.path.join(d, "vuoto.sqlite3")
            sqlite3.connect(p).close()                                   # database SQLite senza schema Finance
            f = CM.build_feed(p, today=TODAY)
            self.assertTrue(f["sola_lettura_verificata"])
            self.assertTrue(all(not e["ok"] for e in f["operazioni"].values()))
            self.assertEqual({e["codice"] for e in f["errori"]}, {"schema_mismatch"})
        finally:
            shutil.rmtree(d)

    def test_file_cambiato_durante_la_lettura_scarta_il_feed(self):
        real = subprocess.run

        def touches(cmd, **kw):
            out = real(cmd, **kw)
            os.utime(self.db, ns=(1, 1))                                  # simula una modifica del file
            return out
        before = os.stat(self.db).st_mtime_ns
        try:
            f = CM.build_feed(self.db, today=TODAY, run=touches)
        finally:
            os.utime(self.db, ns=(before, before))
        self.assertEqual((f["operazioni"], f["sola_lettura_verificata"]), ({}, False))
        self.assertEqual(f["errori"][-1]["ambito"], "sola_lettura")

    def test_node_assente(self):
        with mock.patch.object(CM.shutil, "which", return_value=None), mock.patch.object(CM.os.path, "isfile",
                                                                                        side_effect=lambda p: p == self.db):
            f = CM.build_feed(self.db, today=TODAY)
        self.assertEqual(f["errori"][0]["ambito"], "node")

    def test_comando_con_permessi_e_ambiente_ridotto(self):
        seen = {}

        def spy(cmd, **kw):
            seen.update(cmd=cmd, env=kw.get("env"))
            return subprocess.run(cmd, **kw)
        CM.build_feed(self.db, today=TODAY, run=spy)
        self.assertIn("--permission", seen["cmd"])
        self.assertFalse([a for a in seen["cmd"] if a.startswith(("--allow-fs-write", "--allow-child-process",
                                                                   "--allow-worker", "--allow-addons"))])
        self.assertEqual(set(seen["env"]), {"LANG", "TZ"})                # nessuna variabile d'ambiente ereditata

    def test_riassunto_senza_importi(self):
        s = json.dumps(CM.summary(CM.build_feed(self.db, today=TODAY)))
        for bad in ("amountMinor", "description", "Conto DEMO"):
            self.assertNotIn(bad, s)

    def test_nessun_modulo_di_rete(self):
        with open(CM.SCRIPT, encoding="utf-8") as f:
            imports = re.findall(r'from "([^"]+)"', f.read())
        self.assertEqual(sorted(i for i in imports if i.startswith("node:")), ["node:url"])   # SQLite solo dal motore
        for root in (os.path.join(FINANCE, "src", "query"), os.path.join(FINANCE, "src", "db")):
            for name in os.listdir(root):
                if name.endswith(".ts"):
                    with open(os.path.join(root, name), encoding="utf-8") as f:
                        self.assertIsNone(re.search(r'from "node:(http|https|net|tls|dgram|dns)"|fetch\(', f.read()), name)

    def test_uso_da_terminale_richiede_percorso_esplicito(self):
        r = subprocess.run([sys.executable, "-B", os.path.join(APP, "bridge", "cuore_money.py")], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 64)                                # nessuna lettura del database reale per default


import http.client, threading  # noqa: E402
import serve  # noqa: E402

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {};
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set,
  location: { hash: "", protocol: "file:" }, history: { replaceState() {} }, matchMedia: () => ({ matches: false, addEventListener() {} }),
  addEventListener() {}, scrollTo() {},
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()), querySelectorAll: () => [], querySelector: () => null, addEventListener() {} } };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const C = ctx.CUORE, out = {};
C.MONEY_STATE.mode = "real"; C.MONEY_STATE.feed = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
for (const [s] of C.APPS.money.sections) out["money/" + s] = C.viewHtml(C.parse("money/" + s));
out.home = C.viewHtml(C.parse("")); out.school = C.viewHtml(C.parse("school/oggi"));
C.SCHOOL_STATE.mode = "real"; C.SCHOOL_STATE.feed = { schema: "cuore.school.feed/1", sola_lettura_verificata: true, pagine: { x: {} }, errori: [],
  generato: "2026-01-31T09:00:00+01:00", freschezza: { disponibile: true, per_fonte: { classeviva: { aggiornato_al: "2026-01-30T07:00:00+01:00" }, classroom: { aggiornato_al: null } }, tipi_obsoleti: ["classroom/compiti"] },
  automazione: { disponibile: true, job: { esito: "OK", fine: "2026-01-31T08:01:00+01:00" } } };
out.home_entrambe = C.viewHtml(C.parse("")); out.sistema = C.viewHtml(C.parse("cuore/diagnostica"));
C.SCHOOL_STATE.mode = "error"; C.SCHOOL_STATE.feed = null; C.SCHOOL_STATE.error = "ponte locale: risposta 503";
out.home_school_errore = C.viewHtml(C.parse(""));
C.MONEY_STATE.mode = "error"; C.MONEY_STATE.feed = null; C.MONEY_STATE.error = "ponte locale: risposta 503";
out.errore = C.viewHtml(C.parse("money/saldi"));
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node non disponibile")
class PonteEViste(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "demo.sqlite3")
        build_demo(cls.db)
        cls.feed = CM.build_feed(cls.db, today=TODAY)
        assert cls.feed["sola_lettura_verificata"], cls.feed["errori"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def server(self, money_fn):
        httpd = serve.ThreadingHTTPServer((serve.BIND, 0), None)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = serve.make_handler(port, feed_fn=lambda: {"schema": "x"}, money_fn=money_fn)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, port

    def req(self, port, method, path, host=None):
        c = http.client.HTTPConnection(serve.BIND, port, timeout=30)
        c.request(method, path, headers={"Host": host or "127.0.0.1:%d" % port})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_endpoint_legge_davvero_in_sola_lettura(self):
        before = sha(self.db)
        httpd, port = self.server(lambda: CM.build_feed(self.db, today=TODAY))
        self.assertEqual(httpd.server_address[0], "127.0.0.1")
        r, body = self.req(port, "GET", "/api/money")
        d = json.loads(body)
        self.assertEqual((r.status, d["schema"], d["sola_lettura_verificata"]), (200, "cuore.money.feed/1", True))
        self.assertIn("connect-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(sha(self.db), before)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["demo.sqlite3"])

    def test_endpoint_non_collegato_e_metodi_rifiutati(self):
        _, port = self.server(None)
        r, body = self.req(port, "GET", "/api/money")
        self.assertEqual((r.status, json.loads(body)["operazioni"]), (503, {}))
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.req(port, m, "/api/money")[0].status, 405, m)
        self.assertEqual(self.req(port, "GET", "/api/money", host="evil.example:%d" % port)[0].status, 403)

    def test_endpoint_con_contratto_non_valido(self):
        real = subprocess.run

        def tamper(cmd, **kw):
            d = json.loads(real(cmd, **kw).stdout)
            d["operazioni"]["obligations"]["data"]["items"][0]["counterparty"] = {"name": "X"}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(d), "")
        _, port = self.server(lambda: CM.build_feed(self.db, today=TODAY, run=tamper))
        d = json.loads(self.req(port, "GET", "/api/money")[1])
        self.assertEqual(d["operazioni"]["obligations"]["error"]["code"], "contratto_non_valido")
        self.assertNotIn('"X"', json.dumps(d))

    def test_viste_money_reali(self):
        with open(os.path.join(APP, "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        paths = {n: os.path.join(self.tmp, n) for n in ("page.js", "h.js", "feed.json")}
        for n, text in (("page.js", script), ("h.js", HARNESS), ("feed.json", json.dumps(self.feed))):
            with open(paths[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        try:
            r = subprocess.run([NODE, paths["h.js"], paths["page.js"], paths["feed.json"]], capture_output=True, text=True, timeout=60)
        finally:
            for p in paths.values():
                os.remove(p)
        self.assertEqual(r.returncode, 0, r.stderr)
        v = json.loads(r.stdout)
        demo_only = ("Risparmio DEMO", "Obiettivo DEMO", "Bar DEMO", "Compagno DEMO", "Amico DEMO", "Prestito DEMO")
        for k, html in v.items():
            if k.startswith("money/"):
                self.assertIn(">sola lettura<", html, k)
                self.assertNotIn("DATI DIMOSTRATIVI", html, k)
                for w in demo_only:
                    self.assertNotIn(w, html, (k, w))
                for bad in ("undefined", "NaN", "[object"):
                    self.assertNotIn(bad, html, (k, bad))
        self.assertIn(GOAL, v["money/budget"])
        self.assertIn(PURCHASE, v["money/acquisti"])
        self.assertIn("stima mancante", v["money/acquisti"])               # mancante distinto da zero
        self.assertIn("senza stima, esclusi", v["money/acquisti"])         # e mai sommato come zero
        self.assertIn("Necessità", v["money/budget"])
        self.assertIn("Nomi e gruppi delle categorie", v["money/budget"])          # fonti con nomi leggibili
        self.assertIn("ultimo movimento contabilizzato", v["money/budget"])
        self.assertIn("Spesa alimentare", v["money/flussi"])
        self.assertIn("Da classificare", v["money/flussi"])
        for k in v:
            if k.startswith("money/"):
                self.assertIn("Provenienza dei dati", v[k], k)
        self.assertIn("Nessuna fonte", v["money/investimenti"])
        self.assertIn("non sono implementati", v["money/investimenti"])
        self.assertIn("Credito", v["money/debiti"])
        self.assertIn("Crediti − debiti", v["money/patrimonio"])
        self.assertIn("non ancora registrati", v["money/patrimonio"])  # investimenti assenti: nessuna cifra unica (brief 06/11: meno scritte)
        self.assertIn("DATI DIMOSTRATIVI", v["school"])                   # School invariata (qui senza ponte)
        # home (B2, 04/11): due campi School e Finance (prossima verifica; saldo e avvisi), riga Sistema; niente dettagli tecnici
        h = v["home_entrambe"]
        for w in ('data-route="school"', 'data-route="money"', "sui conti", "Sistema"):
            self.assertIn(w, h, w)
        for bad in ("SINTETICO", "Conto DEMO", "undefined", "NaN", "Notion", "consultazione v1", "Ultimo giro del job"):
            self.assertNotIn(bad, h, bad)
        self.assertIn("Dati dimostrativi", v["home"])                     # School senza ponte: dichiarato, non simulato
        sy = v["sistema"]                                                  # dettagli tecnici e azioni: in Sistema
        for w in ("Sincronizza tutto", "tutte le sezioni lette", "Cosa fare:",
                  "ultimo giro ok 31 gen alle 08:01", "ultima lettura 30 gen alle 07:00", "mai letta"):
            self.assertIn(w, sy, w)
        self.assertNotIn("undefined", sy)
        self.assertIn("Dati non disponibili", v["home_school_errore"])
        self.assertIn("ponte locale: risposta 503", v["home_school_errore"])
        self.assertIn("Dati reali non disponibili", v["errore"])
        self.assertNotIn("DATI DIMOSTRATIVI", v["errore"])

    def render(self, feed):
        with open(os.path.join(APP, "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        paths = {n: os.path.join(self.tmp, n) for n in ("page.js", "h.js", "feed.json")}
        for n, text in (("page.js", script), ("h.js", HARNESS), ("feed.json", json.dumps(feed))):
            with open(paths[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        try:
            r = subprocess.run([NODE, paths["h.js"], paths["page.js"], paths["feed.json"]], capture_output=True, text=True, timeout=60)
        finally:
            for p in paths.values():
                os.remove(p)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_aggiornamento_per_conto(self):
        """Conti: per ogni conto ultima importazione, ultimo movimento e avvisi; mancanze dichiarate, mai inventate."""
        ds = self.feed["operazioni"]["data_status"]["data"]
        ids = [a["accountId"] for a in ds["accounts"]]
        self.assertGreaterEqual(len(ids), 2)
        html = self.render(self.feed)["money/saldi"]
        self.assertIn("Aggiornamento per conto", html)
        self.assertEqual(html.count('class="d">ultima importazione '), sum(1 for a in ds["accounts"] if a.get("lastImportAt")))
        feed = json.loads(json.dumps(self.feed))
        d2 = feed["operazioni"]["data_status"]["data"]
        d2["accounts"][0]["lastImportAt"] = None                               # nessuna importazione riuscita
        for k in ("lastBookedOn", "pendingCount"):
            d2["accounts"][0].pop(k, None)                                     # campi facoltativi nel contratto
        d2["anomalies"] = [{"code": "stale_data", "severity": "warning", "accountId": ids[1]}]
        html = self.render(feed)["money/saldi"]
        self.assertIn("nessuna importazione riuscita", html)
        self.assertIn("nessun movimento contabilizzato", html)
        self.assertIn("chip warn", html)
        self.assertIn("dati vecchi", html)
        for bad in ("undefined", "NaN", "null", "[object"):
            self.assertNotIn(bad, html, bad)


if __name__ == "__main__":
    unittest.main()
