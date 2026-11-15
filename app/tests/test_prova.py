"""Ponte di PROVA (D3b) e controlli dell'ambiente sintetico lato Python (prova_guard, D3a).
Solo sessioni sintetiche create in cartelle temporanee nuove: nessun dato reale, il ponte reale non viene avviato.
Esecuzione dalla radice: python3 -m unittest discover -s app/tests"""
import datetime as dt, http.client, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(APP, "bridge"))
import prova  # noqa: E402
import prova_guard as G  # noqa: E402

NODE = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
TODAY = "2026-02-15"
# oggi: il feed mostra i movimenti degli ultimi 30 giorni rispetto alla data reale, come vedra' chi prova
SPESA = {"requestId": "r1", "accountId": "conto-demo", "transactionDate": dt.date.today().isoformat(), "amountMinor": -450,
         "currency": "EUR", "description": "Caffe DEMO"}


def create(path):
    r = subprocess.run(G.create_command(NODE, path), capture_output=True, text=True, timeout=120, env=G.node_env())
    if r.returncode != 0:
        raise AssertionError("sessione sintetica non creata: %s %s" % (r.stdout[:300], r.stderr[:300]))
    return os.path.realpath(path)


class Controlli(unittest.TestCase):
    """prova_guard senza Node: percorsi, collegamenti, permessi, marcatore, porta."""

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="cuore-prova-test-"))
        self.fake_real = os.path.join(self.base, "finto-reale")
        os.mkdir(self.fake_real, 0o700)
        self.roots = {"allowed": self.base, "forbidden": [self.fake_real]}

    def tearDown(self):
        shutil.rmtree(self.base)

    def session(self, name, dataset="sintetico", sid="s-1"):
        d = os.path.join(self.base, name)
        os.mkdir(d, 0o700)
        db = os.path.join(d, G.SYNTHETIC_DB)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany("INSERT INTO meta VALUES (?, ?)", [("dataset", dataset), ("sessione", sid)])
        con.commit()
        con.close()
        os.chmod(db, 0o600)
        p = os.path.join(d, G.SESSION_FILE)
        with open(p, "w") as f:
            json.dump({"schema": G.SESSION_SCHEMA, "id": "s-1", "stato": "aperta", "database": G.SYNTHETIC_DB}, f)
        os.chmod(p, 0o600)
        return d

    def test_sessione_valida(self):
        d = self.session("ok")
        self.assertEqual(G.verify(d, self.roots)["stato"], "aperta")

    def test_percorsi_rifiutati(self):
        for bad in (None, "", "relativa/x", os.path.join(self.base, "manca")):
            with self.assertRaises(G.ProvaRifiutata):
                G.check_data_dir(bad, self.roots)
        with self.assertRaisesRegex(G.ProvaRifiutata, "vietato"):
            G.check_data_dir(self.fake_real, self.roots)
        with self.assertRaisesRegex(G.ProvaRifiutata, "fuori"):
            G.check_data_dir(self.base, self.roots)

    def test_radici_predefinite_rifiutano_repository_e_icloud(self):
        with self.assertRaises(G.ProvaRifiutata):
            G.check_data_dir(os.path.join(APP, "bridge"))
        icloud = os.path.join(os.path.expanduser("~"), "Library", "Mobile Documents")
        if os.path.isdir(icloud):
            with self.assertRaises(G.ProvaRifiutata):
                G.check_data_dir(icloud)

    def test_collegamenti_e_permessi(self):
        d = self.session("ok")
        os.symlink(d, os.path.join(self.base, "link"))
        with self.assertRaisesRegex(G.ProvaRifiutata, "simbolico"):
            G.check_data_dir(os.path.join(self.base, "link"), self.roots)
        h = self.session("hard")
        os.remove(os.path.join(h, G.SYNTHETIC_DB))
        os.link(os.path.join(d, G.SYNTHETIC_DB), os.path.join(h, G.SYNTHETIC_DB))
        with self.assertRaisesRegex(G.ProvaRifiutata, "collegamenti fisici"):
            G.verify(h, self.roots)
        os.chmod(d, 0o755)
        with self.assertRaisesRegex(G.ProvaRifiutata, "permessi"):
            G.verify(d, self.roots)

    def test_marcatore(self):
        with self.assertRaisesRegex(G.ProvaRifiutata, "marcatore"):
            G.verify(self.session("reale", dataset="reale"), self.roots)
        with self.assertRaisesRegex(G.ProvaRifiutata, "altra sessione"):
            G.verify(self.session("altra", sid="s-2"), self.roots)

    def test_wal_non_vuoto_rifiutato(self):
        d = self.session("wal")
        with open(os.path.join(d, G.SYNTHETIC_DB + "-wal"), "wb") as f:
            f.write(b"x")
        with self.assertRaisesRegex(G.ProvaRifiutata, "-wal"):
            G.verify(d, self.roots)

    def test_porta_del_ponte_reale_rifiutata(self):
        self.assertEqual(G.check_port(8766), 8766)
        for p in (8765, 80, 70000, "8766", True):
            with self.assertRaises(G.ProvaRifiutata):
                G.check_port(p)

    def test_creazione_solo_cartella_nuova_sotto_la_radice(self):
        with self.assertRaisesRegex(G.ProvaRifiutata, "esiste"):
            G.create_command("node", self.session("c"), self.roots)
        with self.assertRaises(G.ProvaRifiutata):
            G.create_command("node", os.path.join(self.fake_real, "x"), self.roots)
        cmd = G.create_command("node", os.path.join(self.base, "nuova"), self.roots)
        self.assertIn("--allow-fs-write=%s/*" % os.path.join(self.base, "nuova"), cmd)

    def test_comandi_con_permessi_ristretti(self):
        d = self.session("cmd")
        w = G.write_command("node", d, self.roots)
        self.assertIn("--permission", w)
        self.assertEqual([a for a in w if a.startswith("--allow-fs-write")], ["--allow-fs-write=%s" % d])
        m = G.money_command("node", d, TODAY, self.roots)
        self.assertEqual([a for a in m if a.startswith("--allow-fs-write")], [])
        self.assertEqual(sorted(G.node_env()), ["LANG", "TMPDIR", "TZ"])

    def test_sessione_chiusa_nessuna_scrittura(self):
        d = self.session("chiusa")
        p = os.path.join(d, G.SESSION_FILE)
        with open(p) as f:
            s = json.load(f)
        s["stato"] = "chiusa"
        with open(p, "w") as f:
            json.dump(s, f)
        with self.assertRaisesRegex(G.ProvaRifiutata, "chiusa"):
            G.write_command("node", d, self.roots)


class Separazione(unittest.TestCase):
    def test_il_ponte_reale_non_conosce_la_prova(self):
        with open(os.path.join(APP, "bridge", "serve.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIsNone(re.search(r"prova", src))
        self.assertIn("do_POST = do_PUT", src)                       # il ponte reale rifiuta ancora ogni scrittura

    def test_il_ponte_di_prova_non_usa_percorsi_reali_ne_variabili(self):
        for name in ("prova.py", "prova_guard.py", "prova_money.ts"):
            with open(os.path.join(APP, "bridge", name), encoding="utf-8") as f:
                src = f.read()
            self.assertIsNone(re.search(r"os\.environ|getenv|process\.env|CUORE_[A-Z]|db_path\(|Application Support/Cloud/",
                                        src), name)


@unittest.skipUnless(NODE, "node non disponibile")
class Ponte(unittest.TestCase):
    """Il ponte di prova su una porta libera, con una sessione sintetica vera (Node)."""

    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="cuore-prova-ponte-")
        cls.dir = create(os.path.join(cls.base, "s"))
        cls.httpd = prova.ThreadingHTTPServer((prova.BIND, 0), None)
        cls.port = cls.httpd.server_address[1]
        cls.httpd.RequestHandlerClass = prova.make_handler(cls.port, cls.dir, NODE)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.base)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection(prova.BIND, self.port, timeout=120)
        h = {"Host": "127.0.0.1:%d" % self.port, **(headers or {})}
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode("utf-8", "replace")

    def test_stato_e_app(self):
        s, doc = self.req("GET", "/api/prova")
        self.assertEqual(s, 200)
        self.assertEqual((doc["schema"], doc["dati"], doc["sessione"]["stato"]), ("cuore.prova/1", "sintetici", "aperta"))
        s, html = self.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn("<html", html.lower())
        self.assertIn('<meta name="cuore-prova" content="sintetico">', html)
        self.assertIn("PROVA — Finance e School su dati SINTETICI", html)
        with open(prova.INDEX, encoding="utf-8") as f:
            self.assertNotIn('<meta name="cuore-prova"', f.read())   # il file servito dal ponte reale non porta il segnale
        for p in ("/api/events", "/api/search"):
            self.assertEqual(self.req("GET", p)[0], 503)

    def test_school_sintetico_e_dati_personali(self):
        s, feed = self.req("GET", "/api/school")
        self.assertEqual((s, feed["schema"], feed["fonte"]), (200, "cuore.school.feed/1", "sintetico"))
        ok = lambda ev: self.req("POST", "/api/prova/esegui", {"op": "school_scrivi", "input": ev})
        self.assertEqual(ok({"tipo": "compito", "uid": "cvv:3000001", "fatto": True})[0], 200)
        s, nota = ok({"tipo": "nota", "testo": "chiedere il cap. 4", "materia": "fisica"})
        self.assertEqual(s, 200)
        self.assertEqual(ok({"tipo": "obiettivo", "materia": "fisica", "media": 7.5})[0], 200)
        for bad in ({"tipo": "impegno", "testo": "x", "data": "2026-13-01"}, {"tipo": "obiettivo", "materia": "fisica", "media": 11},
                    {"tipo": "annulla", "id": "inesistente"}, {"tipo": "sconosciuto"}):
            self.assertEqual(ok(bad)[0], 422, bad)
        self.assertEqual(ok({"tipo": "annulla", "id": nota["risultato"]["id"]})[0], 200)
        s, st = self.req("POST", "/api/prova/esegui", {"op": "school_stato", "input": {}})
        self.assertEqual((s, list(st["risultato"]["fatti"]), st["risultato"]["obiettivi"], st["risultato"]["note"]),
                         (200, ["cvv:3000001"], {"fisica": 7.5}, []))          # nota annullata: fuori dallo stato, resta nel file
        self.assertEqual(st["risultato"]["eventi"], 4)

    def test_promemoria_e_appunti_nella_sessione(self):
        ok = lambda op, i: self.req("POST", "/api/prova/esegui", {"op": op, "input": i, "requestId": "prova-%s-0001" % op})
        s, doc = ok("promemoria_imposta", {"attivo": True, "promemoria": {"fin_settimana": True}})
        self.assertEqual((s, doc["risultato"]["file"]), (200, "presente"), doc)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "promemoria.json")))
        self.assertEqual(ok("promemoria_imposta", {"altro": 1})[0], 422)
        self.assertEqual(ok("appunti_scrivi", {"tipo": "crea", "testo": "Occhio alle spese", "quando": "2026-11-05"})[0], 200)
        s, st = ok("appunti_stato", {})
        self.assertEqual((s, st["risultato"]["aperti"]), (200, 1))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "finance-appunti.jsonl")))

    def test_scrittura_poi_feed_della_copia(self):
        s, doc = self.req("POST", "/api/prova/esegui", {"op": "aggiungi_manuale", "input": dict(SPESA, requestId="feed")})
        self.assertEqual((s, doc["risultato"]["status"]), (200, "created"), doc)
        s, again = self.req("POST", "/api/prova/esegui", {"op": "aggiungi_manuale", "input": dict(SPESA, requestId="feed")})
        self.assertEqual(s, 200)
        self.assertNotEqual(again["risultato"]["status"], "created")      # stessa richiesta: nessun doppione
        s, feed = self.req("GET", "/api/money")
        self.assertEqual(s, 200, feed)
        self.assertEqual((feed["fonte"], feed["sola_lettura_verificata"]), ("sintetico", True))
        self.assertTrue(all(e.get("ok") for e in feed["operazioni"].values()), feed["errori"])
        descr = [t["description"] for t in feed["operazioni"]["transactions"]["data"]["items"]]
        self.assertTrue(any("Caffe" in d for d in descr), descr)

    def test_esiti_degli_errori(self):
        s, doc = self.req("POST", "/api/prova/esegui", {"op": "correggi_manuale", "input": {"id": "manca", "modifiche": {},
                                                                                            "motivo": "x"}})
        self.assertEqual((s, doc["codice"]), (404, "non_trovato"))
        s, doc = self.req("POST", "/api/prova/esegui", {"op": "aggiungi_manuale", "input": dict(SPESA, requestId="v",
                                                                                                currency="XXX")})
        self.assertEqual((s, doc["codice"]), (422, "non_valido"), doc)
        self.assertEqual(self.req("POST", "/api/prova/esegui", {"op": "cancella_tutto"})[0], 400)
        self.assertEqual(self.req("POST", "/api/prova/esegui", b"non json")[0], 400)
        self.assertEqual(self.req("POST", "/api/prova/esegui", {"op": "consulta"},
                                  {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("POST", "/api/prova/esegui", b"x" * (prova.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.req("POST", "/api/altro", {"op": "consulta"})[0], 404)
        self.assertEqual(self.req("DELETE", "/api/prova/esegui")[0], 405)

    def test_richieste_estranee_rifiutate(self):
        self.assertEqual(self.req("GET", "/api/money", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/prova/esegui", {"op": "consulta"},
                                  {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/prova/esegui", {"op": "consulta"},
                                  {"Sec-Fetch-Site": "cross-site"})[0], 403)


if __name__ == "__main__":
    unittest.main()
