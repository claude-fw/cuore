"""CUORE School: esito dell'ultimo giro del job scuola letto dal file di stato del job. Solo cartelle temporanee con
file SINTETICI: lo stato reale del job non viene letto. Nessuno stato di Notion entra nel feed (Notion non fa parte di CUORE)."""
import json, os, shutil, sys, tempfile, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bridge"))
import cuore_school as CS  # noqa: E402

STATE = {"ultimo": {"inizio": "2026-01-31T08:00:00+01:00", "fine": "2026-01-31T08:01:30+01:00", "modo": "commit", "esito": "OK",
                    "exit_code": 0},
         "ultimo_commit_ok": "2026-01-31T08:01:30+01:00",
         "notion": {"impronta": "abc", "verificata_il": "2026-01-29T17:00:00+01:00", "riferimento": {"hub": "x"}}}


class Automazione(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name, data):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_ultimo_giro_del_job(self):
        self.write("state.json", STATE)
        a = CS.automation_status(self.dir)
        self.assertEqual(a, {"disponibile": True, "job": {"esito": "OK", "modo": "commit", "fine": "2026-01-31T08:01:30+01:00",
                                                          "ultimo_commit_ok": "2026-01-31T08:01:30+01:00"}, "problemi": [],
                             "chiusure_in_attesa": 0})
        self.assertNotIn("notion", json.dumps(a).lower())                          # stato di Notion mai riportato

    def test_file_assente_o_rotto_non_diventa_un_esito(self):
        self.assertEqual(CS.automation_status(self.dir)["disponibile"], False)
        self.write("state.json", "{non json")
        a = CS.automation_status(self.dir)
        self.assertEqual(a["disponibile"], False)
        self.assertIn("non leggibile", a["motivo"])

    def test_stato_vuoto_non_viene_inventato(self):
        self.write("state.json", {})
        self.assertEqual(CS.automation_status(self.dir)["job"]["esito"], None)

    def test_cartella_del_job_accanto_al_registry(self):
        self.assertEqual(CS.job_dir("/tmp/x/school/registry.sqlite3"), "/tmp/x/school/job")

    def test_problemi_aperti_solo_vocabolario_fisso(self):
        self.write("state.json", dict(STATE, problemi=PROBLEMS))
        ps = CS.automation_status(self.dir)["problemi"]
        self.assertEqual([(p["tipo"], p["fonte"], p["notificato"], p["in_osservazione"], p["giri"]) for p in ps],
                         [("anomalia", None, True, False, 1), ("altro", None, False, False, None),
                          ("parziale", "classroom", True, False, None), ("parziale_temporaneo", "classeviva", False, True, 1),
                          ("temporaneo", "classroom", False, True, 2)])                    # chiave del job dal 25/10
        text = json.dumps(ps)
        for bad in ("cvv:1234567", "announcement", "material", "TESTO LIBERO", "dettaglio segreto"):   # niente chiavi o testi
            self.assertNotIn(bad, text)
        self.assertEqual(ps[1]["titolo"], None)                                    # titolo non del job: non esposto
        self.assertEqual(ps[1]["dal"], None)                                       # data non valida: mai inventata

    def test_problemi_non_validi_ignorati(self):
        self.write("state.json", dict(STATE, problemi=["non", "un", "oggetto"]))
        self.assertEqual(CS.automation_status(self.dir)["problemi"], [])
        self.write("state.json", dict(STATE, problemi={"x": "stringa", "fallito": None}))
        self.assertEqual(CS.automation_status(self.dir)["problemi"], [])


PROBLEMS = {
    "parziale_temporaneo:classeviva": {"dal": "2026-01-31T08:01:30+01:00", "ultima_notifica": None, "titolo":
                                       "Cloud Scuola: sincronizzazione parziale ripetuta", "giri": 1, "attesa_conferma": 2},
    "parziale:classroom:announcement,material": {"dal": "2026-01-30T20:00:00+01:00", "ultima_notifica": "2026-01-30T21:42:00+01:00",
                                                 "titolo": "Cloud Scuola: sincronizzazione parziale"},
    "anomalia:assenze_ambigue:cvv:1234567": {"dal": "2026-01-31T08:01:30+01:00", "ultima_notifica": "2026-01-31T08:01:31+01:00",
                                             "titolo": "Cloud Scuola: controllo richiesto", "giri": 1},
    "boh:qualcosa": {"dal": "ieri", "titolo": "TESTO LIBERO dettaglio segreto"},
    "temporaneo:classroom": {"dal": "2026-01-31T08:01:30+01:00", "ultima_notifica": None, "giri": 2, "attesa_conferma": 3,
                             "titolo": "Cloud Scuola: Classroom non aggiornato da piu' giri"},
}

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {};
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set, location: { hash: "", protocol: "file:" },
  history: { replaceState() {}, pushState() {} }, matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, scrollTo() {},
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()), querySelectorAll: () => [], querySelector: () => null, addEventListener() {} } };
ctx.window = ctx; vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const C = ctx.CUORE, au = JSON.parse(fs.readFileSync(process.argv[3], "utf8")), out = {};
C.SCHOOL_STATE.mode = "real"; C.SCHOOL_STATE.feed = { schema: "cuore.school.feed/1", sola_lettura_verificata: true, pagine: { x: {} }, errori: [],
  generato: "2026-01-31T09:00:00+01:00", freschezza: { disponibile: true, per_fonte: {}, tipi_obsoleti: [] }, automazione: au };
out.home = C.viewHtml(C.parse("")); out.sistema = C.viewHtml(C.parse("cuore/automazioni"));
C.SCHOOL_STATE.mode = "error"; C.SCHOOL_STATE.error = null;
C.SCHOOL_STATE.feed = { schema: "cuore.school.feed/1", sola_lettura_verificata: false, pagine: {}, errori: [{ ambito: "registro", messaggio: "x" }], automazione: au };
out.school = C.viewHtml(C.parse("school/oggi")); out.sistema_errore = C.viewHtml(C.parse("cuore/automazioni"));
console.log(JSON.stringify(out));
"""


class ProblemiNelleViste(unittest.TestCase):
    def render(self, au):
        import re, shutil, subprocess
        node = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)
        if not node:
            self.skipTest("node non disponibile")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(HERE, "..", "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        for n, text in (("page.js", script), ("h.js", HARNESS), ("au.json", json.dumps(au))):
            with open(os.path.join(d, n), "w", encoding="utf-8") as fh:
                fh.write(text)
        r = subprocess.run([node, os.path.join(d, "h.js"), os.path.join(d, "page.js"), os.path.join(d, "au.json")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_problemi_in_sistema_non_in_home(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp)
        with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as f:
            json.dump(dict(STATE, problemi=PROBLEMS, risolti_in_attesa={"freschezza": "Cloud Scuola: dati non aggiornati"}), f)
        v = self.render(CS.automation_status(tmp))
        self.assertIn("Problemi del job", v["sistema"])
        self.assertIn("5 aperti · 2 notificati · 2 in osservazione · 1 chiusura in attesa", v["sistema"])
        self.assertIn("Problemi aperti del job", v["sistema_errore"])
        for w in ("parziale per errori temporanei · ClasseViva", "errori temporanei (rete o Mac in stop) · Classroom", "in osservazione: notifica se si ripete", "notificato",
                  "controllo richiesto", "1 giro"):
            self.assertIn(w, v["sistema_errore"], w)
        self.assertNotIn("Problemi del job", v["home"])                   # la Home resta un punto di ingresso
        self.assertNotIn("Problemi aperti del job", v["school"])           # e la vista School non mostra il job
        for bad in ("undefined", "NaN", "null", "TESTO LIBERO", "cvv:"):
            self.assertNotIn(bad, v["home"] + v["school"] + v["sistema"] + v["sistema_errore"], bad)

    def test_nessun_problema_e_dato_assente(self):
        v = self.render({"disponibile": True, "job": {"esito": "OK"}, "problemi": []})
        self.assertIn("nessuno aperto", v["sistema"])
        self.assertIn("Nessun problema aperto", v["sistema_errore"])
        v = self.render({"disponibile": True, "job": {"esito": "OK"}})                     # ponte precedente: niente righe
        self.assertNotIn("Problemi del job", v["sistema"])
        self.assertNotIn("Problemi aperti del job", v["sistema_errore"])


if __name__ == "__main__":
    unittest.main()
