"""CUORE School: contratto `cuore.school.feed/1` (cuore_school_contract.py) e sua integrazione nel ponte e nella dashboard.
Solo dati SINTETICI: registro di prova del motore in una cartella temporanea e documenti di pagina costruiti a mano."""
import copy, datetime as dt, json, os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ENGINE = os.path.normpath(os.path.join(APP, "..", "scuola", "mcp", "classeviva"))
sys.path[:0] = [os.path.join(APP, "bridge"), os.path.join(ENGINE, "tests"), ENGINE, os.path.join(ENGINE, "..", "_common")]
import cuore_school as CS  # noqa: E402
import cuore_school_contract as K  # noqa: E402
import test_school_records as TR  # noqa: E402

NOW = dt.datetime.fromisoformat(TR.F.NOW)
NODE = shutil.which("node") or ("/usr/local/bin/node" if os.path.isfile("/usr/local/bin/node") else None)


def page(name="voti", blocks=None, charts=None, **over):
    """Documento di pagina cloud.school.dashboard/1 minimo e conforme (valori sintetici)."""
    doc = {"schema": K.PAGE_SCHEMA, "pagina": name, "materia": None,
           "riferimento_temporale": {"now": "2026-10-04T08:00:00+02:00", "data_riferimento": "2026-10-04"},
           "blocchi": blocks if blocks is not None else [
               {"id": "riepilogo", "titolo": "Riepilogo", "stato": "dati", "affidabilita": "alta",
                "provenienza": {"contratto": "cloud.school.records/1", "versione": "r1-sint"}, "dati": {"n": 3}}],
           "grafici": charts if charts is not None else [],
           "provenance": {"schema": K.PAGE_SCHEMA, "dashboard_version": "r1-sint", "sorgente": "/Users/sintetico/registry.sqlite3"}}
    doc.update(over)
    return doc


def raw_feed(pages, **over):
    f = {"schema": K.SCHEMA, "fonte": "reale", "generato": "2026-10-04T08:00:00+02:00", "pagine": pages, "errori": [],
         "freschezza": {"disponibile": True, "per_fonte": {"classeviva": {"non_letti": []}}, "tipi_obsoleti": []},
         "sola_lettura_verificata": True}
    f.update(over)
    return f


class Pagine(unittest.TestCase):
    def test_pagina_conforme_e_risultato_vuoto(self):
        self.assertEqual(K.validate_page(page(), "voti"), [])
        empty = page(blocks=[{"id": "elenco", "titolo": "Elenco", "stato": "vuoto_valido", "provenienza": {}, "dati": []}])
        self.assertEqual(K.validate_page(empty, "voti"), [])                     # vuoto = risultato valido, non errore
        f = K.finalize(raw_feed({"voti": empty}))
        self.assertEqual((f["esito"], f["errori"]), ("completo", []))

    def test_campo_obbligatorio_mancante_e_tipi_errati(self):
        cases = {
            "senza riferimento temporale": page(riferimento_temporale=None),
            "stato non ammesso": page(blocks=[{"id": "x", "titolo": "X", "stato": "boh", "provenienza": {}}]),
            "dati annunciati ma assenti": page(blocks=[{"id": "x", "titolo": "X", "stato": "dati", "provenienza": {}, "dati": None}]),
            "provenienza non oggetto": page(blocks=[{"id": "x", "titolo": "X", "stato": "vuoto_valido", "provenienza": "?"}]),
            "y testuale in un grafico a linee": page(charts=[{"id": "g", "tipo": "linea", "provenienza": {},
                                                              "serie": [{"nome": "s", "punti": [{"x": "2026-10-01", "y": "8"}]}]}]),
            "calendario senza data": page(charts=[{"id": "c", "tipo": "calendario", "provenienza": {},
                                                    "serie": [{"nome": "m", "punti": [{"x": None, "y": "matematica"}]}]}]),
            "schema diverso": page(schema="cloud.school.dashboard/2"),
            "nome diverso dalla chiave": page(name="assenze"),
        }
        for why, doc in cases.items():
            self.assertTrue(K.validate_page(doc, "voti"), why)
        ok_cal = page(charts=[{"id": "c", "tipo": "calendario", "provenienza": {},
                               "serie": [{"nome": "m", "punti": [{"x": "2026-10-20", "y": "matematica"}]}]}])
        self.assertEqual(K.validate_page(ok_cal, "voti"), [])

    def test_pagina_non_conforme_tolta_feed_parziale(self):
        f = K.finalize(raw_feed({"voti": page(), "assenze": page(name="assenze", blocchi="non una lista")}))
        self.assertEqual(list(f["pagine"]), ["voti"])
        self.assertEqual(f["errori"], [K.error("assenze", "contratto_non_valido")])
        self.assertEqual(f["esito"], "parziale")
        self.assertEqual(K.validate_feed(f), [])

    def test_tutte_le_pagine_non_conformi(self):
        f = K.finalize(raw_feed({"voti": page(blocchi=None)}))
        self.assertEqual((f["pagine"], f["esito"]), ({}, "non_disponibile"))

    def test_provenienza_conservata_percorso_personale_tolto(self):
        src = page()
        f = K.finalize(raw_feed({"voti": copy.deepcopy(src)}))
        doc = f["pagine"]["voti"]
        self.assertEqual(doc["blocchi"][0]["provenienza"], src["blocchi"][0]["provenienza"])
        self.assertEqual(doc["provenance"]["sorgente"], "registry.sqlite3")
        self.assertNotIn("/Users/", json.dumps(f))


class Feed(unittest.TestCase):
    def test_freschezza_esplicita(self):
        self.assertEqual(K.freshness_state({"disponibile": False}), "non_disponibile")
        self.assertEqual(K.freshness_state({"disponibile": True, "tipi_obsoleti": ["classroom/compiti"]}), "dati_vecchi")
        self.assertEqual(K.freshness_state({"disponibile": True, "per_fonte": {"classroom": {"non_letti": ["classroom/x"]}}}),
                         "fonti_non_lette")
        self.assertEqual(K.freshness_state({"disponibile": True, "tipi_obsoleti": [], "per_fonte": {}}), "aggiornati")
        stale = K.finalize(raw_feed({"voti": page()}, freschezza={"disponibile": True, "tipi_obsoleti": ["cvv/grade"]}))
        self.assertEqual((stale["esito"], stale["freschezza"]["stato"]), ("completo", "dati_vecchi"))   # vecchi, non errore

    def test_versione_sconosciuta_o_feed_non_oggetto(self):
        self.assertEqual(K.validate_feed({"schema": "cuore.school.feed/2"}), ["schema: 'cuore.school.feed/2' non supportato (atteso 'cuore.school.feed/1')"])
        self.assertEqual(K.validate_feed([]), ["feed: oggetto atteso"])
        bad = K.finalize(raw_feed({"voti": page()}))
        bad["errori"] = [{"ambito": "x", "codice": "inventato", "messaggio": "Traceback ..."}]
        self.assertTrue(K.validate_feed(bad))

    def test_esito_completo_incoerente_rilevato(self):
        f = K.finalize(raw_feed({"voti": page()}))
        f["errori"].append(K.error("assenze", "errore_motore"))
        self.assertTrue(K.validate_feed(f))


class _ConRegistro(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.reg = os.path.join(self.tmp, "registry.sqlite3")
        TR.make(path=self.reg).close()
        self.subjects = os.path.join(self.tmp, "materie.json")
        with open(self.subjects, "w", encoding="utf-8") as f:
            json.dump(TR.SUBJECTS, f)

    def feed(self, **kw):
        return CS.build_feed(kw.pop("reg", self.reg), NOW, subjects_file=self.subjects,
                             manual_path=os.path.join(self.tmp, "manuale.json"), **kw)



class NelPonte(_ConRegistro):
    def test_feed_reale_sintetico_conforme(self):
        f = self.feed()
        self.assertEqual(K.validate_feed(f), [])
        self.assertEqual((f["esito"], f["contratto"]["schema"]), ("completo", K.SCHEMA))
        self.assertIn(f["freschezza"]["stato"], K.FRESHNESS_STATES)
        self.assertEqual(set(f["pagine"]), set(CS.PAGES))

    def test_fonte_indisponibile(self):
        f = self.feed(reg=os.path.join(self.tmp, "manca.sqlite3"))
        self.assertEqual(K.validate_feed(f), [])                                # anche un errore e' un feed conforme
        self.assertEqual((f["esito"], f["errori"][0]["codice"]), ("non_disponibile", "registro_assente"))
        self.assertEqual(f["freschezza"]["stato"], "non_disponibile")

    def test_errore_del_motore_parziale_e_messaggio_pubblico(self):
        real = CS.SD.PageSession.build

        def flaky(session, pagina, *a, **kw):
            if pagina == "assenze":
                raise OSError("/Users/sintetico/registry.sqlite3: TOKEN-SINTETICO cvv:1234567")
            return real(session, pagina, *a, **kw)
        with mock.patch.object(CS.SD.PageSession, "build", autospec=True, side_effect=flaky):
            f = self.feed(pages=("voti", "assenze"))
        self.assertEqual(K.validate_feed(f), [])
        self.assertEqual((f["esito"], list(f["pagine"])), ("parziale", ["voti"]))
        self.assertEqual(f["errori"], [{"ambito": "assenze", "codice": "errore_motore",
                                        "messaggio": K.PUBLIC_MESSAGES["errore_motore"], "dettaglio": "OSError"}])
        for secret in ("/Users/", "TOKEN-SINTETICO", "cvv:1234567"):
            self.assertNotIn(secret, json.dumps(f["errori"]))

    def test_pagina_non_conforme_dal_motore_non_rompe_il_feed(self):
        real = CS.SD.PageSession.build

        def broken(session, pagina, *a, **kw):
            doc = real(session, pagina, *a, **kw)
            if pagina == "voti":
                doc["blocchi"][0]["stato"] = "inventato"
            return doc
        with mock.patch.object(CS.SD.PageSession, "build", autospec=True, side_effect=broken):
            f = self.feed(pages=("voti", "home"))
        self.assertEqual(list(f["pagine"]), ["home"])
        self.assertEqual(f["errori"][0]["codice"], "contratto_non_valido")
        self.assertEqual(f["versione_motore"], f["pagine"]["home"]["provenance"]["dashboard_version"])


HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = () => ({ innerHTML: "", textContent: "", dataset: {}, setAttribute() {}, scrollIntoView() {} });
const els = {};
const ctx = { console, Intl, Math, Date, String, Number, Object, Array, JSON, RegExp, Set, location: { hash: "", protocol: "file:" },
  history: { replaceState() {}, pushState() {} }, matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, scrollTo() {},
  document: { documentElement: { dataset: {} }, getElementById: id => (els[id] = els[id] || el()), querySelectorAll: () => [], querySelector: () => null, addEventListener() {} } };
ctx.window = ctx; vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const C = ctx.CUORE, f = JSON.parse(fs.readFileSync(process.argv[3], "utf8")), out = { problemi: {} };
for (const [k, v] of Object.entries({ valido: f, v2: Object.assign({}, f, { schema: "cuore.school.feed/2" }), altro: { schema: "x" },
  malformato: Object.assign({}, f, { pagine: [] }), nullo: null })) out.problemi[k] = C.schoolFeedProblem(v);
C.SCHOOL_STATE.mode = "real"; C.SCHOOL_STATE.feed = f;
out.viste = {}; for (const [s] of C.APPS.school.sections) out.viste[s] = C.viewHtml(C.parse("school/" + s));
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node non disponibile")
class NellaDashboard(_ConRegistro):
    def test_dashboard_compatibile_e_feed_non_conformi_gestiti(self):
        f = self.feed()
        with open(os.path.join(APP, "index.html"), encoding="utf-8") as fh:
            script = re.search(r"<script>(.*?)</script>", fh.read(), re.S).group(1)
        paths = {n: os.path.join(self.tmp, n) for n in ("page.js", "h.js", "feed.json")}
        for n, text in (("page.js", script), ("h.js", HARNESS), ("feed.json", json.dumps(f))):
            with open(paths[n], "w", encoding="utf-8") as fh:
                fh.write(text)
        r = subprocess.run([NODE, paths["h.js"], paths["page.js"], paths["feed.json"]], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        v = json.loads(r.stdout)
        self.assertEqual(v["problemi"], {"valido": None, "v2": "versione del feed School non supportata da questa dashboard",
                                         "altro": "feed School non riconosciuto", "malformato": "feed School non conforme al contratto",
                                         "nullo": "risposta del ponte non valida"})
        for s, html in v["viste"].items():
            self.assertIn(">sola lettura<", html, s)
            for bad in ("undefined", "NaN", "[object", "/Users/"):
                self.assertNotIn(bad, html, (s, bad))


if __name__ == "__main__":
    unittest.main()
