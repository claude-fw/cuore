"""CUORE School: adattatore di sola lettura (cuore.school.feed/1), ponte locale e viste reali.
Solo dati sintetici (registro di prova del motore in una cartella temporanea). Esecuzione dalla radice:
python3 -m unittest discover -s app/tests"""
import datetime as dt, http.client, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ENGINE = os.path.normpath(os.path.join(APP, "..", "scuola", "mcp", "classeviva"))
sys.path[:0] = [os.path.join(APP, "bridge"), os.path.join(ENGINE, "tests"), ENGINE, os.path.join(ENGINE, "..", "_common")]
import cuore_school as CS  # noqa: E402
import serve  # noqa: E402
import test_school_records as TR  # noqa: E402   (registro sintetico del motore)
import cvv_dryrun as dry  # noqa: E402

NOW = dt.datetime.fromisoformat(TR.F.NOW)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.reg_path = os.path.join(self.tmp, "registry.sqlite3")
        TR.make(path=self.reg_path).close()
        self.subjects = os.path.join(self.tmp, "materie.json")
        with open(self.subjects, "w", encoding="utf-8") as f:
            json.dump(TR.SUBJECTS, f)
        self.manual = os.path.join(self.tmp, "manuale-assente.json")

    def feed(self, **kw):
        kw.setdefault("subjects_file", self.subjects)
        return CS.build_feed(self.reg_path, NOW, manual_path=self.manual, **kw)


class Adattatore(Base):
    def test_feed_completo_e_sola_lettura(self):
        before = dry._fs_state(self.reg_path)
        f = self.feed()
        self.assertEqual(dry._fs_state(self.reg_path), before)
        self.assertTrue(f["sola_lettura_verificata"])
        self.assertEqual(f["errori"], [])
        self.assertEqual(set(f["pagine"]), set(CS.PAGES))
        for p, doc in f["pagine"].items():
            self.assertEqual(doc["schema"], "cloud.school.dashboard/1", p)        # documento del motore, non rielaborato
        self.assertEqual(f["data_riferimento"], "2026-11-01")
        self.assertTrue(f["freschezza"]["disponibile"])
        self.assertEqual(f["scritture"], {"registry": False, "classeviva": False, "classroom": False, "drive": False})

    def test_stesso_risultato_del_motore(self):
        import school_dashboard as SD
        f = self.feed(pages=("voti",))
        ref = SD.build_from_path(self.reg_path, NOW, manual_path=self.manual, subjects=TR.SUBJECTS, pagina="voti")
        self.assertEqual(f["pagine"]["voti"]["provenance"]["sorgente"], "registry.sqlite3")   # nessun percorso personale
        ref["provenance"]["sorgente"] = "registry.sqlite3"
        self.assertEqual(f["pagine"]["voti"], ref)                           # per il resto: documento del motore invariato

    def test_materie_formato_cv_subjects(self):
        cv = {"count": len(TR.SUBJECTS), "subjects": [{"id": s["id"], "materia": s["desc"], "chiave": s["key"], "docenti": s["docenti_nomi"]}
                                                     for s in TR.SUBJECTS]}
        p = os.path.join(self.tmp, "cv.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cv, f)
        self.assertEqual(CS.load_subjects(p), TR.SUBJECTS)
        self.assertEqual(self.feed(subjects_file=p, pages=("voti",))["pagine"], self.feed(pages=("voti",))["pagine"])

    def test_materie_assenti_nessun_dato(self):
        f = self.feed(subjects_file=os.path.join(self.tmp, "manca.json"))
        self.assertEqual(f["pagine"], {})
        self.assertFalse(f["sola_lettura_verificata"])
        self.assertEqual(f["errori"][0]["ambito"], "materie")

    def test_materie_non_valide(self):
        with open(self.subjects, "w", encoding="utf-8") as fh:
            json.dump([{"nome": "x"}], fh)
        self.assertEqual(self.feed()["errori"][0]["ambito"], "materie")

    def test_registro_assente(self):
        f = CS.build_feed(os.path.join(self.tmp, "assente.sqlite3"), NOW, subjects_file=self.subjects, manual_path=self.manual)
        self.assertEqual((f["pagine"], f["errori"][0]["ambito"]), ({}, "registro"))

    def test_errore_del_motore_visibile_senza_dati(self):
        with mock.patch.object(CS.SD.PageSession, "build", autospec=True, side_effect=ValueError("rotto")):
            f = self.feed(pages=("voti",))
        self.assertEqual(f["pagine"], {})
        e = f["errori"][0]
        self.assertEqual((e["codice"], e["dettaglio"]), ("errore_motore", "ValueError"))
        self.assertNotIn("rotto", e["messaggio"])                          # testo dell'eccezione mai pubblico
        self.assertEqual(f["esito"], "non_disponibile")

    def test_rete_bloccata_durante_la_lettura(self):
        def calls_out(*a, **k):
            socket.create_connection(("classeviva.example", 443))
        with mock.patch.object(CS.SD.PageSession, "build", autospec=True, side_effect=calls_out):
            f = self.feed(pages=("home",))
        self.assertEqual(f["pagine"], {})
        self.assertEqual(f["errori"][0]["codice"], "rete_bloccata")
        self.assertNotIn("classeviva.example", json.dumps(f["errori"]))
        orig = (socket.socket.connect, socket.create_connection, socket.getaddrinfo)
        with CS.no_network(), socket.socket() as sk:
            self.assertRaises(CS.NetworkBlocked, socket.getaddrinfo, "example.com", 443)
            self.assertRaises(CS.NetworkBlocked, sk.connect, ("127.0.0.1", 9))
        self.assertEqual((socket.socket.connect, socket.create_connection, socket.getaddrinfo), orig)   # ripristinati

    def test_scrittura_durante_la_lettura_scarta_il_feed(self):
        real = CS.SD.PageSession.build

        def writes(session, *a, **k):
            out = real(session, *a, **k)
            with open(session.path, "ab") as fh:
                fh.write(b"\0")
            return out
        with mock.patch.object(CS.SD.PageSession, "build", autospec=True, side_effect=writes):
            f = self.feed(pages=("voti",))
        self.assertEqual(f["pagine"], {})
        self.assertFalse(f["sola_lettura_verificata"])
        self.assertEqual(f["errori"][-1]["ambito"], "sola_lettura")

    def test_riassunto_senza_valori(self):
        s = json.dumps(CS.summary(self.feed()), ensure_ascii=False)
        self.assertNotIn('"punti"', s)
        self.assertNotIn('"valore"', s)

    def test_nessun_client_di_rete_importato(self):
        import ast
        with open(CS.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
               {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        for bad in ("cvv_client", "cls_client", "cls_auth", "notion_dashboard", "notion_autorun", "urllib.request", "http.client", "server"):
            self.assertNotIn(bad, mods, bad)
        calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "") for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for bad in ("login", "load_credentials", "write", "unlink", "remove", "rename", "makedirs", "mkdir"):
            self.assertNotIn(bad, calls, bad)
        for n in ast.walk(tree):                                         # ogni open() è in sola lettura
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open":
                self.assertEqual(len(n.args), 1)
                self.assertFalse([k for k in n.keywords if k.arg == "mode"])


class Cache(unittest.TestCase):
    def setUp(self):
        self.state, self.t, self.calls, self.ok = ["a"], [0.0], [], True

        def build():
            self.calls.append(1)
            return {"sola_lettura_verificata": self.ok, "n": len(self.calls)}
        self.c = serve.FeedCache(build=build, state=lambda: self.state[0], ttl=900, clock=lambda: self.t[0])

    def test_riusa_finche_gli_ingressi_non_cambiano(self):
        self.assertEqual([self.c.get()["n"], self.c.get()["n"]], [1, 1])
        self.state[0] = "b"                                              # il job ha aggiornato il Registry
        self.assertEqual(self.c.get()["n"], 2)
        self.t[0] = 901                                                  # età massima superata
        self.assertEqual(self.c.get()["n"], 3)

    def test_errori_mai_in_cache(self):
        self.ok = False
        self.assertEqual([self.c.get()["n"], self.c.get()["n"]], [1, 2])

    def test_stato_ingressi(self):
        self.assertIsInstance(CS.inputs_state(os.devnull, os.devnull, os.devnull), str)


class Ponte(Base):
    def setUp(self):
        super().setUp()
        feed = self.feed()
        self.httpd = serve.ThreadingHTTPServer((serve.BIND, 0), None)
        self.port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass = serve.make_handler(self.port, feed_fn=lambda: feed)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def req(self, method, path, host=None):
        c = http.client.HTTPConnection(serve.BIND, self.port, timeout=10)
        c.request(method, path, headers={"Host": host or "127.0.0.1:%d" % self.port})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_solo_loopback(self):
        self.assertEqual(serve.BIND, "127.0.0.1")
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_pagina_e_feed(self):
        r, body = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"CUORE", body)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertIn("connect-src 'self'", r.getheader("Content-Security-Policy"))
        r, body = self.req("GET", "/api/school")
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(body)["schema"], "cuore.school.feed/1")

    def test_solo_lettura_e_host(self):
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.req(m, "/api/school")[0].status, 405, m)
        self.assertEqual(self.req("GET", "/../scuola/mcp/classeviva/registry.py")[0].status, 404)
        self.assertEqual(self.req("GET", "/api/school", host="evil.example:%d" % self.port)[0].status, 403)


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
C.SCHOOL_STATE.mode = "real"; C.SCHOOL_STATE.feed = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
for (const [s] of C.APPS.school.sections) out["school/" + s] = C.viewHtml(C.parse("school/" + s));
out.home = C.viewHtml(C.parse("")); out.money = C.viewHtml(C.parse("money/quadro"));
C.SCHOOL_STATE.mode = "error"; C.SCHOOL_STATE.feed = null; C.SCHOOL_STATE.error = "ponte locale: risposta 500";
out.errore = C.viewHtml(C.parse("school/voti"));
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node non disponibile")
class VisteReali(Base):
    def test_viste_school_dal_feed(self):
        with open(os.path.join(APP, "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        paths = {n: os.path.join(self.tmp, n) for n in ("page.js", "harness.js", "feed.json")}
        for n, text in (("page.js", script), ("harness.js", HARNESS), ("feed.json", json.dumps(self.feed()))):
            with open(paths[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        r = subprocess.run(["node", paths["harness.js"], paths["page.js"], paths["feed.json"]], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        v = json.loads(r.stdout)
        demo_only = ("Funzioni e grafici", "Dispensa funzioni.pdf", "Esercizi 12–18", "Uscita didattica", "media aritmetica (demo)")
        for k, html in v.items():
            if k.startswith("school/"):
                self.assertIn(">sola lettura<", html, k)
                self.assertNotIn("DATI DIMOSTRATIVI", html, k)
                for w in demo_only:
                    self.assertNotIn(w, html, (k, w))                       # nessun dato dimostrativo nelle viste reali
                self.assertNotIn("undefined", html, k)
                self.assertNotIn("NaN", html, k)
        self.assertIn("Verifiche in programma", v["school/verifiche"])   # brief 06/11: niente note esplicative
        self.assertIn("DATI DIMOSTRATIVI", v["money"])                    # Money resta dimostrativo
        self.assertNotIn("€", v["home"])
        self.assertIn("Dati reali non disponibili", v["errore"])
        self.assertNotIn("DATI DIMOSTRATIVI", v["errore"])


if __name__ == "__main__":
    unittest.main()
