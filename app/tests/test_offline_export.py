"""CUORE: copia offline esplicita e ridotta (`cuore.offline/1`). Solo database SINTETICI in cartelle temporanee: il
database reale non viene mai aperto, nessun file e' scritto fuori dalle cartelle temporanee dei test."""
import datetime as dt, json, os, re, shutil, stat, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(APP, "bridge"))
sys.path.insert(0, HERE)
import export_offline as EX  # noqa: E402
from test_money_bridge import GOAL, NODE, PURCHASE, TODAY, build_demo, sha  # noqa: E402

NOW = dt.datetime(2026, 1, 31, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=1)))


def payload_of(html):
    m = re.search(r'<script type="application/json" id="cuore-offline">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


@unittest.skipUnless(NODE, "node non disponibile")
class CopiaOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "demo.sqlite3")
        build_demo(cls.db)
        cls.out_dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)
        shutil.rmtree(cls.out_dir)

    def build(self, path):
        return EX.cuore_money.build_feed(path, today=TODAY)

    def export(self, name="copia.html", db=None, now=NOW):
        return EX.export(db or self.db, os.path.join(self.out_dir, name), now=now, build=self.build)

    def test_formato_data_ora_origine_e_permessi(self):
        before = sha(self.db)
        path = self.export("formato.html")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as f:
            html = f.read()
        p = payload_of(html)
        self.assertEqual(p["schema"], "cuore.offline/1")
        self.assertEqual(p["creato_il"], "2026-01-31T09:30:00+01:00")
        self.assertRegex(p["origine"]["letto_il"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(p["origine"]["feed"], "cuore.money.feed/1")
        self.assertEqual(p["stato"], {"money": "completo", "school": "omesso"})
        self.assertTrue(p["omessi"])
        self.assertIn("COPIA OFFLINE · NON AGGIORNATA", html)
        self.assertIn("default-src 'none'", html)                              # nessuna rete dalla pagina
        self.assertNotRegex(html, r"https?://")
        self.assertEqual(sha(self.db), before)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["demo.sqlite3"])        # nessun -wal/-shm, nessuna copia del db

    def test_minimizzazione(self):
        with open(self.export("minimo.html"), encoding="utf-8") as f:
            html = f.read()
        p = payload_of(html)
        m = p["money"]
        self.assertEqual(set(m), {"oggi", "mese", "aggiornamento", "saldi_totali", "flussi_del_mese", "budget", "obiettivi",
                                  "acquisti", "crediti_debiti", "avvisi", "nomi_categorie"})
        self.assertEqual(m["obiettivi"][0]["name"], GOAL)
        self.assertEqual(m["acquisti"], {"idea": 2, "planned": 0, "purchased": 0, "cancelled": 0})
        for bad in ("Conto DEMO", "Carta DEMO", PURCHASE, "Amico DEMO", "Prestito DEMO", "amountMinor", "description",
                    "accountId", "demo.sqlite3", self.tmp, "SQLite format"):
            self.assertNotIn(bad, html, bad)

    def test_non_disponibile_resta_distinto_da_zero(self):
        def broken(path):
            f = self.build(path)
            f["operazioni"]["summary"] = {"ok": False, "error": {"code": "x", "message": "x"}}
            f["errori"].append({"ambito": "summary"})
            return f
        p = EX.reduce_feed(broken(self.db), NOW)
        self.assertIsNone(p["money"]["budget"])                                  # non disponibile: null, mai zero
        self.assertEqual(p["stato"]["money"], "parziale")
        self.assertEqual(p["errori"], ["summary"])

    def test_errori_nessun_file_scritto(self):
        cases = {
            "repository": os.path.join(APP, "copia-offline-test.html"),
            "icloud": os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/cuore-test.html"),
            "estensione": os.path.join(self.out_dir, "copia.txt"),
            "cartella": os.path.join(self.out_dir, "manca", "copia.html"),
        }
        for why, out in cases.items():
            with self.assertRaises(EX.ExportError, msg=why):
                EX.export(self.db, out, now=NOW, build=self.build)
            self.assertFalse(os.path.exists(out), why)
        existing = os.path.join(self.out_dir, "esiste.html")
        with open(existing, "w") as f:
            f.write("x")
        with self.assertRaises(EX.ExportError):
            EX.export(self.db, existing, now=NOW, build=self.build)
        with open(existing) as f:
            self.assertEqual(f.read(), "x")                                       # mai sovrascritto
        for why, db in (("assente", os.path.join(self.tmp, "manca.sqlite3")),):
            with self.assertRaises(EX.ExportError, msg=why):
                self.export("assente.html", db=db)
            self.assertFalse(os.path.exists(os.path.join(self.out_dir, "assente.html")))

    def test_wal_non_vuoto_nessun_file(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        try:
            p = os.path.join(d, "copia.sqlite3")
            shutil.copy(self.db, p)
            with open(p + "-wal", "wb") as f:
                f.write(b"\0" * 32)
            with self.assertRaises(EX.ExportError):
                self.export("wal.html", db=p)
            self.assertFalse(os.path.exists(os.path.join(self.out_dir, "wal.html")))
        finally:
            shutil.rmtree(d)

    def test_html_non_iniettabile(self):
        f = self.build(self.db)
        f["operazioni"]["planning"]["data"]["goals"][0]["name"] = '</script><img src=x onerror=alert(1)>&'
        html = EX.render(EX.reduce_feed(f, NOW))
        self.assertNotIn("</script><img", html)
        self.assertEqual(html.count("</script>"), 2)                             # solo i due blocchi del modello
        self.assertEqual(payload_of(html)["money"]["obiettivi"][0]["name"], '</script><img src=x onerror=alert(1)>&')

    def test_pagina_mostra_copia_offline(self):
        path = self.export("pagina.html")
        harness = os.path.join(self.tmp, "h.js")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(r"""
const vm = require("vm"), fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const json = html.match(/<script type="application\/json" id="cuore-offline">([\s\S]*?)<\/script>/)[1];
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const mk = (tag) => ({ tag, children: [], className: "", textContent: "", appendChild(c) { this.children.push(c); return c; } });
const els = { "cuore-offline": { textContent: json }, view: mk("div"), banner: mk("span") };
const text = n => (n.textContent || "") + " " + n.children.map(text).join(" ");
const ctx = { Intl, Math, Date, String, JSON, Object, document: { getElementById: id => els[id], createElement: mk } };
vm.createContext(ctx); vm.runInContext(code, ctx);
console.log(JSON.stringify({ banner: els.banner.textContent, view: text(els.view) }));
""")
        try:
            r = subprocess.run([NODE, harness, path], capture_output=True, text=True, timeout=60)
        finally:
            os.remove(harness)
        self.assertEqual(r.returncode, 0, r.stderr)
        v = json.loads(r.stdout)
        self.assertIn("Creata", v["banner"])
        self.assertIn("Per i dati attuali usa CUORE sul Mac", v["banner"])
        self.assertIn("lettura completa", v["banner"])
        self.assertRegex(v["banner"], r"dati (aggiornati|GIÀ NON AGGIORNATI) al momento della copia")
        self.assertIn(GOAL, v["view"])
        self.assertIn("Omesso da questa copia", v["view"])
        for bad in ("undefined", "NaN", "[object"):
            self.assertNotIn(bad, v["view"] + v["banner"])

    def test_uso_da_terminale_richiede_db_e_uscita(self):
        r = subprocess.run([sys.executable, "-B", os.path.join(APP, "bridge", "export_offline.py")], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 2)                                        # argparse: argomenti obbligatori


if __name__ == "__main__":
    unittest.main()
