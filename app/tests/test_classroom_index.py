"""Indice Classroom per la ricerca (`cuore_classroom.py`, `cuore.school.classroom_index/1`). Registry SINTETICO (fixture del
catalogo M15 del motore) in una cartella temporanea e archivio dei materiali sintetico: nessun file reale viene letto."""
import datetime as dt, hashlib, json, os, shutil, sqlite3, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ENGINE = os.path.normpath(os.path.join(APP, "..", "scuola", "mcp", "classeviva"))
sys.path[:0] = [os.path.join(APP, "bridge"), os.path.join(ENGINE, "tests"), ENGINE, os.path.join(ENGINE, "..", "_common")]
import cuore_classroom as CC  # noqa: E402
import cuore_search as S  # noqa: E402
import registry as R  # noqa: E402
import school_materials_catalog as MC  # noqa: E402
import test_school_materials_catalog as TM  # noqa: E402

F = TM.F
NOW = dt.datetime.fromisoformat(F.NOW) if isinstance(F.NOW, str) else F.NOW


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Indice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.reg = os.path.join(cls.dir, "registry.sqlite3")
        TM.make(path=cls.reg).close()
        snap = R.Registry.from_snapshot(cls.reg)
        try:
            refs = MC.collect(snap, F.SUBJECTS, F.MANUAL)
        finally:
            snap.close()
        cls.integrali = next(r["id"] for r in refs if r["titolo"] == "Integrali.pdf")
        cls.store = os.path.join(cls.dir, "materials.sqlite3")
        con = sqlite3.connect(cls.store)
        con.execute("CREATE TABLE versions (ref_id TEXT, text TEXT)")
        con.execute("INSERT INTO versions VALUES (?, ?)", (cls.integrali, "TESTO PRIVATO del file"))
        con.commit()
        con.close()
        cls.before = {p: sha(p) for p in (cls.reg, cls.store)}
        cls.listing = sorted(os.listdir(cls.dir))                                       # file accessori della fixture compresi
        cls.doc = CC.build_index(cls.reg, now=NOW, store=cls.store, subjects=F.SUBJECTS, manual=F.MANUAL)
        cls.by = {v["titolo"]: v for v in cls.doc["voci"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_materiali_compiti_annunci_corsi_con_link_originale(self):
        d = self.doc
        self.assertEqual((d["schema"], d["errori"], d["sola_lettura_verificata"]), (CC.SCHEMA, [], True))
        for t in ("file Drive", "link", "video YouTube", "compito Classroom", "annuncio Classroom", "corso Classroom"):
            self.assertIn(t, d["conteggi"], t)
        v = self.by["Integrali.pdf"]
        self.assertEqual((v["tipo"], v["link"], v["rotta"], v["contenuto"]),
                         ("file Drive", "https://drive.google.com/file/d/d2/view", "school/materiali", "letto (archivio materiali)"))
        self.assertEqual(self.by["Soluzioni disequazioni.pdf"]["contenuto"], "non letto")
        self.assertEqual(self.by["Grammar page"]["link"], "https://example.org/grammar")
        self.assertNotIn("Bozza.pdf", self.by)                                         # compito non pubblicato: escluso
        self.assertEqual(d["contenuti"]["letti"], 1)
        self.assertIn("nessuno scope Drive", d["contenuti"]["nota"])

    def test_nessun_identificativo_interno_ne_testo_dei_file(self):
        text = json.dumps(self.doc)
        for bad in ("cls:", "registry_version", '"hash"', "TESTO PRIVATO", self.integrali):
            self.assertNotIn(bad, text, bad)

    def test_sola_lettura(self):
        self.assertEqual({p: sha(p) for p in self.before}, self.before)
        self.assertEqual(sorted(os.listdir(self.dir)), self.listing)                     # nessun file nuovo

    def test_solo_link_https(self):
        self.assertIsNone(CC._https("http://example.org/x"))
        self.assertIsNone(CC._https("javascript:alert(1)"))
        self.assertEqual(CC._https("https://youtu.be/yt1"), "https://youtu.be/yt1")

    def test_ricerca_trova_i_file_per_titolo_corso_e_argomento(self):
        d = S.search("integrali", classroom_fn=lambda: self.doc)
        hit = next(r for r in d["risultati"] if r["titolo"] == "Integrali.pdf")
        self.assertEqual((hit["modulo"], hit["link"], d["fonti"]["classroom"]["stato"]),
                         ("school", "https://drive.google.com/file/d/d2/view", "letta"))
        self.assertIn("contenuto letto", hit["dettaglio"])
        course = self.by["Integrali.pdf"].get("corso")
        self.assertTrue(course)
        self.assertIn("Integrali.pdf", [r["titolo"] for r in S.search(course + " integrali", classroom_fn=lambda: self.doc)["risultati"]])
        self.assertEqual(S.search("PRIVATO", classroom_fn=lambda: self.doc)["totale"], 0)    # il contenuto non e' indicizzato

    def test_errori_dichiarati(self):
        missing = CC.build_index(os.path.join(self.dir, "assente.sqlite3"), now=NOW, store=self.store,
                                 subjects=F.SUBJECTS, manual=F.MANUAL)
        self.assertEqual((missing["voci"], missing["errori"]), ([], [{"codice": "registro_assente"}]))
        self.assertEqual(S.search("integrali", classroom_fn=lambda: missing)["fonti"]["classroom"]["stato"], "non_disponibile")
        states = iter([("a",), ("b",)])
        with mock.patch.object(CC, "inputs_state", lambda *a, **k: next(states)):
            moved = CC.build_index(self.reg, now=NOW, store=self.store, subjects=F.SUBJECTS, manual=F.MANUAL)
        self.assertEqual((moved["voci"], moved["errori"]), ([], [{"codice": "sola_lettura_violata"}]))
        no_store = CC.build_index(self.reg, now=NOW, store=os.path.join(self.dir, "nessun-archivio.sqlite3"),
                                  subjects=F.SUBJECTS, manual=F.MANUAL)
        self.assertEqual(no_store["contenuti"]["letti"], 0)
        self.assertEqual({v["contenuto"] for v in no_store["voci"] if "contenuto" in v}, {"non letto"})


if __name__ == "__main__":
    unittest.main()
