"""M19+M20 - school_material_store (cloud.school.material_store/1, cloud.school.study_material/1): acquisizione solo da riferimenti
del catalogo M15 con id Drive corrispondente, archivio separato e append-only, idempotenza, versioni, rifiuti senza scritture, stato
per materia/obiettivo, studio M13 sul materiale con provenienza 'materiale_scolastico_acquisito', materia rispettata, collegamento
M5 all'obiettivo, Registry mai scritto, strumenti MCP."""
import hashlib, json, os, sqlite3, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import registry as R
import school_material_store as S
import school_study_session as SS
from test_school_materials_catalog import make, NOW, MAN, SUBJ, fresh_fake

SIM = "cls:C-MF/w-sim#drive_file:d3"          # matematica determinata, M5: prepara (probabile)
NOSUBJ = "cls:C-MF/w-nosubj#drive_file:d4"    # possibile: fisica/matematica
META = {"id": "d3", "title": "Simulazione.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-10-27T16:53:13Z",
        "owner": "prof@scuola", "fileSize": "1500", "viewUrl": "https://drive.google.com/file/d/d3/view"}
TEXT = "Esercizio 1 - funzione irrazionale con parametri. Esercizio 2 - equazioni irrazionali."


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = make()
        cls.p = mock.patch.object(R.Registry, "freshness", fresh_fake())
        cls.p.start()

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        cls.reg.close()

    def setUp(self):
        self.store = os.path.join(tempfile.mkdtemp(), "sub", "materials.sqlite3")

    def acq(self, ref=SIM, text=TEXT, meta=META):
        return S.acquire(self.store, self.reg, NOW, ref, text, meta, MAN, subjects=SUBJ)

    def rows(self):
        db = sqlite3.connect(self.store)
        try:
            return db.execute("SELECT ref_id, version, sha256, modified_time FROM versions ORDER BY id").fetchall()
        finally:
            db.close()


class Acquisizione(Base):
    def test_first_then_idempotent_then_new_version(self):
        a = self.acq()
        self.assertEqual((a["esito"], a["scritture"], a["versione"]["versione"]), ("acquisito", True, 1))
        self.assertEqual(a["versione"]["sha256"], hashlib.sha256(TEXT.encode()).hexdigest())
        b = self.acq()
        self.assertEqual((b["esito"], b["scritture"]), ("invariato", False))
        c = self.acq(text=TEXT + " Esercizio 3.")
        self.assertEqual((c["esito"], c["versione"]["versione"]), ("nuova_versione", 2))
        d = self.acq(meta=dict(META, modifiedTime="2026-10-29T10:00:00Z"))     # stesso testo, file modificato su Drive
        self.assertEqual((d["esito"], d["versione"]["versione"]), ("nuova_versione", 3))
        self.assertEqual(len(self.rows()), 3)

    def test_provenance_and_catalog_link(self):
        a = self.acq()
        self.assertEqual(a["attribuzione"]["stato"], "determinata")
        self.assertEqual(a["attribuzione"]["materia"], "matematica")
        self.assertEqual(a["padre"]["uid"], "cls:C-MF/w-sim")
        self.assertEqual([x["stato"] for x in a["associazioni_m5"]], ["probabile"])
        v = a["versione"]
        self.assertEqual((v["proprietario"], v["modificato_su_drive"], v["dimensione_byte"], v["metodo"]),
                         ("prof@scuola", "2026-10-27T16:53:13Z", 1500, "connettore_google_drive"))
        self.assertTrue(v["acquisito_il"].startswith("2026-11-01"))
        self.assertTrue(a["provenance"]["store_version"].startswith("r1-"))

    def test_refusals_write_nothing(self):
        cases = [(dict(ref="cls:inesistente"), "non_nel_catalogo"),
                 (dict(ref="cls:C-SC/a-sci1#youtube:yt1"), "non_acquisibile"),
                 (dict(ref="cvv:1003#allegato_classeviva:1"), "non_acquisibile"),
                 (dict(meta=dict(META, id="d9")), "id_drive_diverso"),
                 (dict(meta=dict(META, mimeType="video/mp4")), "formato_non_leggibile"),
                 (dict(text="   \n"), "testo_vuoto"),
                 (dict(ref="cls:C-MF/w-draft#drive_file:d5", meta=dict(META, id="d5")), "non_disponibile")]
        for kw, code in cases:
            out = self.acq(**kw)
            self.assertEqual((out["esito"], out["motivo"], out["scritture"]), ("rifiutato", code, False), kw)
        self.assertFalse(os.path.exists(self.store))                     # nessun archivio creato da un rifiuto

    def test_historical_material_not_acquired(self):
        out = self.acq(ref="cls:C-FI/m-fil-old#drive_file:d9", meta=dict(META, id="d9"))
        self.assertEqual(out["motivo"], "non_nel_catalogo")               # perimetro operativo soltanto

    def test_title_mismatch_is_a_warning_not_a_rename(self):
        out = self.acq(meta=dict(META, title="Simulazione verifica (rinominata).pdf"))
        self.assertEqual(out["esito"], "acquisito")
        self.assertEqual([w["codice"] for w in out["avvisi"]], ["titolo_diverso"])

    def test_invalid_inputs(self):
        for args in ((" ", TEXT, META), (SIM, None, META), (SIM, TEXT, None), (SIM, TEXT, {"title": "x"}),
                     (SIM, TEXT, dict(META, mimeType="")), (SIM, TEXT, dict(META, fileSize="tanti"))):
            with self.assertRaises(ValueError):
                S.acquire(self.store, self.reg, NOW, *args, MAN, subjects=SUBJ)

    def test_store_private_permissions(self):
        self.acq()
        self.assertEqual(os.stat(self.store).st_mode & 0o777, 0o600)

    def test_append_only(self):
        self.acq()
        db = sqlite3.connect(self.store)
        try:
            for sql in ("DELETE FROM versions", "UPDATE versions SET text = 'x'"):
                with self.assertRaises(sqlite3.DatabaseError):
                    db.execute(sql)
        finally:
            db.close()
        self.assertEqual(len(self.rows()), 1)

    def test_registry_file_untouched(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "registry.sqlite3")
        make(path=path).close()
        def h():
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        before = h()
        reg = R.Registry.from_snapshot(path)
        try:
            S.acquire(self.store, reg, NOW, SIM, TEXT, META, MAN, subjects=SUBJ)
        finally:
            reg.close()
        self.assertEqual(before, h())
        self.assertNotEqual(os.path.dirname(self.store), d)


class Stato(Base):
    def test_status_without_store_creates_nothing(self):
        st = S.status(self.store, self.reg, NOW, materia="matematica", manual=MAN, subjects=SUBJ)
        self.assertFalse(os.path.exists(self.store))
        self.assertFalse(st["provenance"]["archivio_presente"])
        self.assertEqual(st["sintesi"]["acquisito"], 0)
        sim = next(i for i in st["materiali"] if i["riferimento"] == SIM)
        self.assertEqual(sim["acquisizione"], "da_acquisire")
        self.assertIn("fileId=d3", sim["come"])

    def test_status_after_acquisition_and_filters(self):
        self.acq()
        st = S.status(self.store, self.reg, NOW, materia="matematica", manual=MAN, subjects=SUBJ)
        sim = next(i for i in st["materiali"] if i["riferimento"] == SIM)
        self.assertEqual((sim["acquisizione"], sim["versioni"]), ("acquisito", 1))
        self.assertEqual(st["sintesi"]["totale"], sum(st["sintesi"][k] for k in S.ACQ_STATES))
        obj = sim["associazioni_m5"][0]["con"][0]
        only = S.status(self.store, self.reg, NOW, materia="matematica", obiettivo=obj, manual=MAN, subjects=SUBJ)
        self.assertEqual([i["riferimento"] for i in only["materiali"]], [SIM])
        one = S.status(self.store, self.reg, NOW, riferimento="cls:C-SC/a-sci1#youtube:yt1", manual=MAN, subjects=SUBJ)
        self.assertEqual(one["materiali"][0]["acquisizione"], "non_acquisibile")


class Studio(Base):
    def study(self, materia="matematica", modalita="ripassa", ref=SIM, **kw):
        return S.study(self.store, self.reg, NOW, materia, modalita, ref, manual=MAN, subjects=SUBJ, **kw)

    def test_not_acquired(self):
        self.assertEqual(self.study()["esito"]["stato"], "materiale_non_acquisito")
        self.assertEqual(self.study(ref="cls:x")["esito"]["stato"], "riferimento_non_nel_catalogo")

    def test_session_on_material_with_school_provenance(self):
        self.acq()
        out = self.study()
        self.assertEqual(out["schema"], "cloud.school.study_material/1")
        self.assertEqual(out["esito"]["stato"], "ammessa")
        part = out["contenuto"]["parti"][0]
        self.assertEqual((part["id"], part["provenienza"]), ("materiale", "materiale_scolastico_acquisito"))
        self.assertEqual(part["sha256"], out["materiale"]["versione"]["sha256"])
        self.assertEqual(out["contenuto"]["testo"], TEXT)
        codes = [f["codice"] for f in out["sessione"]["frasi_obbligatorie"]]
        self.assertIn("materiale", codes)
        self.assertIn("acquisito_non_studiato", codes)
        self.assertNotIn("contenuto_utente", codes)                       # il materiale non e' contenuto dell'utente
        self.assertIn("materiale_scolastico_acquisito", out["sessione"]["etichette_provenienza"])
        self.assertTrue(set(SS.COMMON_FORBIDDEN) <= set(out["sessione"]["azioni_vietate"]))
        self.assertEqual(out["materiale"]["attribuzione"]["verifica"], "confermata")
        self.assertEqual(json.dumps(out, sort_keys=True), json.dumps(self.study(), sort_keys=True))   # deterministico

    def test_no_forbidden_words(self):
        import re
        import school_materials_catalog as MC
        self.acq()
        out = self.study(obiettivo="cvv:x")
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in set(SS.FORBIDDEN_WORDS) | set(MC.FORBIDDEN_WORDS)), re.I)
        texts = [f["frase"] for f in out["sessione"]["frasi_obbligatorie"]] + out["regole_agente"]
        texts += S.status(self.store, self.reg, NOW, materia="matematica", manual=MAN, subjects=SUBJ)["regole_agente"]
        for t in texts:
            self.assertIsNone(rx.search(t), t)

    def test_subject_respected(self):
        self.acq()
        self.assertEqual(self.study(materia="fisica")["esito"]["stato"], "materia_non_corrispondente")
        self.acq(ref=NOSUBJ, meta=dict(META, id="d4", title="Esercizi misti.pdf"))
        out = self.study(materia="fisica", ref=NOSUBJ)
        self.assertEqual(out["materiale"]["attribuzione"]["verifica"], "possibile")
        self.assertIn("attribuzione_possibile", [f["codice"] for f in out["sessione"]["frasi_obbligatorie"]])

    def test_objective_relation_is_m5_only(self):
        self.acq()
        obj = S.status(self.store, self.reg, NOW, riferimento=SIM, manual=MAN, subjects=SUBJ)["materiali"][0]["associazioni_m5"][0]["con"][0]
        out = self.study(obiettivo=obj)
        self.assertEqual([x["stato"] for x in out["materiale"]["relazione_obiettivo"]], ["probabile"])
        rel = next(f for f in out["sessione"]["frasi_obbligatorie"] if f["codice"] == "relazione_obiettivo")
        self.assertIn("non stabilisce gli argomenti", rel["frase"])

    def test_correction_keeps_user_answer_separate(self):
        self.acq()
        out = self.study(modalita="correggi", risposta_utente="x = 2")
        provs = [p["provenienza"] for p in out["contenuto"]["parti"]]
        self.assertIn("materiale_scolastico_acquisito", provs)
        self.assertEqual(out["esito"]["stato"], "ammessa")
        self.assertIsNotNone(out["contenuto"]["risposta_utente"])


class ServerTool(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        d = tempfile.mkdtemp()
        self.db = os.path.join(d, "registry.sqlite3")
        make(path=self.db).close()
        self.store = os.path.join(d, "materials.sqlite3")
        for target, value in ((server.regmod, "default_path"), (server.smstore, "default_store")):
            p = mock.patch.object(target, value, return_value=self.db if value == "default_path" else self.store)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("subjects", lambda: SUBJ),):
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(server.sctx, "load_manual", return_value={"dati": MAN, "presente": True, "percorso": "x"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(R.Registry, "freshness", fresh_fake())
        p.start()
        self.addCleanup(p.stop)

    def test_tools_end_to_end(self):
        st = self.server.school_materials_status_v1(materia="matematica")
        self.assertFalse(st["verifica_registro_non_scritto"]["file_modificati"])
        a = self.server.school_material_acquire_v1(SIM, TEXT, META)
        self.assertEqual(a["esito"], "acquisito")
        self.assertFalse(a["verifica_registro_non_scritto"]["file_modificati"])
        out = self.server.school_study_material_v1("matematica", "interroga", SIM)
        self.assertEqual(out["esito"]["stato"], "ammessa")
        self.assertTrue(os.path.isfile(self.store))


if __name__ == "__main__":
    unittest.main()
