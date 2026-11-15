"""M15 v1 - school_materials_catalog/1 (cloud.school.materials_catalog/1): catalogo dei materiali CITATI dalle fonti. Stati separati
(stato_materiale / attribuzione), nessun 'acquisito', attribuzione per elemento separata dall'appartenenza al corso, corsi condivisi,
corsi non confermati, allegati delle circolari generali, associazioni solo via M5 con il loro stato, perimetro operativo/storico mai
mescolati, paginazione deterministica, nessuna parola su utilita'/sufficienza, provenance, determinismo, ordine invertito, nessuna
scrittura, input immutati, clock una volta, M1-M14 invariati."""
import copy, datetime as dt
import hashlib, json, os, re, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import context4_fixtures as F
import cvv_cls_links as L
import cvv_comms as M
import cvv_dryrun as dry
import registry as R
import school_action as AC
import school_context as SC
import school_context3 as C3
import school_context4 as C4
import school_materials_catalog as MC
import school_priority as PR
import school_records as RC
import school_status as ST
import school_study_assistant as SA
import school_study_context as SX
import school_study_session as SS
from links_fixtures import ann, material, work
from test_school_context4 import fresh_fake, pretty, counts

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL
A = lambda c, a: "cls:%s/%s" % (c, a)            # noqa: E731


def drive(fid, title):
    return {"driveFile": {"driveFile": {"id": fid, "title": title, "alternateLink": "https://drive.google.com/file/d/%s/view" % fid},
                          "shareMode": "VIEW"}}


def with_mats(obj, mats):
    return dict(obj, materials=mats)


ATT = {"m-mat1": [drive("d1", "Soluzioni disequazioni.pdf")], "m-int": [drive("d2", "Integrali.pdf")],
       "w-sim": [drive("d3", "Simulazione.pdf")], "w-nosubj": [drive("d4", "Esercizi misti.pdf")],
       "m-fil-old": [drive("d9", "Metafisica.pdf")]}
MATERIALS = [with_mats(m, ATT[m["id"]]) if m["id"] in ATT else m for m in F.LF.MATERIALS]
WORKS = [with_mats(w, ATT[w["id"]]) if w["id"] in ATT else w for w in F.WORKS] + [
    dict(work("C-MF", "w-draft", "Bozza", "2026-10-30", "tM"), state="DRAFT", materials=[drive("d5", "Bozza.pdf")])]
ANNS = [a for a in F.ANNOUNCEMENTS if a["id"] not in ("a-ing", "a-sci1", "a-x", "a-old-mat")] + [
    with_mats(ann("C-EN", "a-ing", "Esercizi per giovedi 29/10 ripasso generale per verifica", "2026-10-26"),
              [{"link": {"url": "https://example.org/grammar", "title": "Grammar page"}}]),
    with_mats(ann("C-SC", "a-sci1", "Presentazione stechiometria delle reazioni chimiche", "2026-10-22"),
              [{"youtubeVideo": {"id": "yt1", "title": "Stechiometria", "alternateLink": "https://youtu.be/yt1"}}]),
    with_mats(ann("C-X", "a-x", "Verifica del 2 novembre 2026", "2026-10-28"), [drive("d6", "Arte.pdf")]),
    with_mats(ann("C-OLD", "a-old-mat", "Esercizi di matematica per la prossima lezione", "2026-10-29"), [drive("d7", "Vecchio corso.pdf")])]


def make(reverse=False, path=":memory:", **kw):
    args = dict(materials=MATERIALS, works=WORKS, announcements=ANNS)
    args.update(kw)
    return F.make_registry(path=path, reverse=reverse, **args)


def build(reg, **kw):
    kw.setdefault("manual", MAN)
    return MC.build(reg, kw.pop("now", NOW), subjects=kw.pop("subjects", SUBJ), **kw)


def refs(d):
    return {r["id"]: r for r in d["riferimenti"]}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = make()
        cls.p = mock.patch.object(R.Registry, "freshness", fresh_fake())
        cls.p.start()
        cls.doc = build(cls.reg, limite=200, dettaglio=True)
        cls.r = refs(cls.doc)

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        cls.reg.close()


# ------------------------------------------------------------------------------------------------ struttura
class Struttura(Base):
    def test_schema_keys(self):
        self.assertEqual(MC.SCHEMA, "cloud.school.materials_catalog/1")
        self.assertEqual(list(self.doc), ["schema", "natura", "riferimento_temporale", "ambito", "sintesi", "riferimenti", "fonti",
                                          "freshness", "gap", "stati", "provenance", "regole_agente"])
        self.assertTrue(MC.catalog_version().startswith("r1-"))
        self.assertEqual(self.doc["riferimento_temporale"]["anno_operativo"], "2026-27")

    def test_never_acquired(self):
        for r in self.doc["riferimenti"]:
            self.assertIn(r["stato_materiale"], MC.MATERIAL_STATES)
            self.assertNotIn(r["stato_materiale"], MC.RESERVED_STATES)
        self.assertIn("contenuto_non_acquisito", [g["codice"] for g in self.doc["gap"]])

    def test_closed_vocabularies(self):
        for r in self.doc["riferimenti"]:
            self.assertIn(r["attribuzione"]["stato"], MC.ATTRIBUTION_STATES)
            self.assertIn(r["tipo_risorsa"], MC.RESOURCE_TYPES)
            self.assertIn(r["padre"]["tipo"], MC.PARENT_KINDS)

    def test_no_usefulness_words(self):
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in MC.FORBIDDEN_WORDS), re.I)
        for txt in [MC.NATURA] + MC.RULES_FOR_AGENTS + [g["messaggio"] for g in self.doc["gap"]]:
            self.assertIsNone(rx.search(txt), txt)

    def test_invalid_inputs(self):
        for kw in ({"materia": ""}, {"materia": "religione"}, {"obiettivo": " "}, {"perimetro": "tutto"}, {"limite": 0},
                   {"limite": "5"}, {"pagina": 0}, {"dettaglio": "si"}, {"soglia_ore": 0}):
            with self.assertRaises(ValueError):
                build(self.reg, **kw)


# ------------------------------------------------------------------------------------------------ identificazione e attribuzione
class Attribuzione(Base):
    def test_drive_reference_identified_verbatim(self):
        r = self.r[A("C-MF", "m-mat1") + "#drive_file:d1"]
        self.assertEqual((r["titolo"], r["drive_file_id"], r["stato_materiale"]), ("Soluzioni disequazioni.pdf", "d1", "identificato_non_acquisito"))
        self.assertEqual((r["attribuzione"]["stato"], r["attribuzione"]["materia"]), ("determinata", "matematica"))
        self.assertEqual(r["padre"]["corso"]["confermato_per"], ["fisica", "matematica"])          # appartenenza al corso a parte
        self.assertTrue(r["padre"]["corso"]["condiviso"])

    def test_shared_course_without_topic_is_possible(self):
        r = self.r[A("C-MF", "w-nosubj") + "#drive_file:d4"]
        self.assertEqual((r["attribuzione"]["stato"], r["attribuzione"]["materia"], r["attribuzione"]["candidati"]),
                         ("possibile", None, ["fisica", "matematica"]))
        for k in ("matematica", "fisica"):
            self.assertIn(r["id"], [x["id"] for x in build(self.reg, materia=k, limite=200)["riferimenti"]])

    def test_unconfirmed_courses(self):
        prop = self.r[A("C-OLD", "a-old-mat") + "#drive_file:d7"]
        self.assertEqual((prop["attribuzione"]["stato"], prop["attribuzione"]["candidati"], prop["attribuzione"]["regola"]),
                         ("possibile", ["matematica"], "proposta corso"))
        none = self.r[A("C-X", "a-x") + "#drive_file:d6"]
        self.assertEqual(none["attribuzione"]["stato"], "non_determinabile")

    def test_unpublished_is_not_available(self):
        r = self.r[A("C-MF", "w-draft") + "#drive_file:d5"]
        self.assertEqual(r["stato_materiale"], "non_disponibile")
        self.assertIn("non pubblicato", r["motivo_stato"])

    def test_other_resource_types(self):
        link = next(r for r in self.doc["riferimenti"] if r["tipo_risorsa"] == "link")
        self.assertEqual((link["titolo"], link["url"], link["attribuzione"]["materia"]), ("Grammar page", "https://example.org/grammar", "inglese"))
        yt = next(r for r in self.doc["riferimenti"] if r["tipo_risorsa"] == "youtube")
        self.assertEqual((yt["titolo"], yt["attribuzione"]["materia"]), ("Stechiometria", "scienze"))

    def test_notice_attachments_general(self):
        gen = [r for r in self.doc["riferimenti"] if r["sorgente"] == "classeviva"]
        self.assertEqual(sorted(r["titolo"] for r in gen), ["4 ALLEGATO", "Modulo.pdf"])
        for r in gen:
            self.assertEqual((r["attribuzione"]["stato"], r["attribuzione"]["materia"]), ("generale", None))
        for k in SC.AGENT_SUBJECTS:
            self.assertFalse([x for x in build(self.reg, materia=k, limite=200)["riferimenti"] if x["padre"]["tipo"] == "notice"])

    def test_subject_filter_never_mixes_determined_other_subject(self):
        d = build(self.reg, materia="fisica", limite=200)
        for r in d["riferimenti"]:
            a = r["attribuzione"]
            self.assertTrue(a["materia"] == "fisica" or (a["stato"] == "possibile" and "fisica" in a["candidati"]))


# ------------------------------------------------------------------------------------------------ associazioni M5
class Associazioni(Base):
    def test_association_only_via_m5_with_state(self):
        r = self.r[A("C-MF", "w-sim") + "#drive_file:d3"]
        a = next(x for x in r["associazioni_m5"] if x["con"]["uid"] == "cvv:1000101")
        self.assertEqual((a["relazione"], a["stato"]), ("prepara", "probabile"))
        res = L.compute(L.collect(self.reg, SUBJ, MAN))
        m5 = {(x["a"]["uid"], x["b"]["uid"]): x["stato"] for x in res["collegamenti"]}
        for ref in self.doc["riferimenti"]:
            for x in ref["associazioni_m5"]:
                if x["tipo"] == "collegamento":
                    self.assertEqual(m5[(x["con"]["uid"], ref["padre"]["uid"])], x["stato"])
                    self.assertIn(x["stato"], ("probabile", "proposto"))

    def test_objective_filter_exact(self):
        d = build(self.reg, obiettivo="cvv:1000101", limite=200)
        self.assertEqual([r["id"] for r in d["riferimenti"]], [A("C-MF", "w-sim") + "#drive_file:d3"])
        self.assertEqual(build(self.reg, obiettivo="cvv:265450")["riferimenti"], [])     # nessuna ricerca approssimata

    def test_no_association_invented(self):
        r = self.r[A("C-MF", "w-nosubj") + "#drive_file:d4"]
        self.assertFalse([x for x in r["associazioni_m5"] if x["tipo"] == "collegamento"])


# ------------------------------------------------------------------------------------------------ perimetro e paginazione
class PerimetroEPaginazione(Base):
    def test_operational_and_history_never_mixed(self):
        old_id = A("C-SF", "m-fil-old") + "#drive_file:d9"
        self.assertNotIn(old_id, self.r)
        st = build(self.reg, perimetro="storico", limite=200, dettaglio=True)
        self.assertIn(old_id, refs(st))
        self.assertTrue(all(r["padre"]["anno"] != "2026-27" for r in st["riferimenti"]))
        self.assertTrue(all(r["padre"]["anno"] == "2026-27" for r in self.doc["riferimenti"]))
        self.assertEqual(self.doc["sintesi"]["fuori_perimetro"], {"2025-26": 1})

    def test_pagination_deterministic_and_complete(self):
        allr = [r["id"] for r in build(self.reg, limite=200)["riferimenti"]]
        p1 = build(self.reg, limite=3, pagina=1)
        p2 = build(self.reg, limite=3, pagina=2)
        self.assertEqual([r["id"] for r in p1["riferimenti"]] + [r["id"] for r in p2["riferimenti"]], allr[:6])
        self.assertEqual(p1["sintesi"]["totale"], len(allr))
        self.assertEqual(p1["sintesi"]["pagine"], -(-len(allr) // 3))
        big = build(self.reg, limite=5000)
        self.assertEqual((big["ambito"]["limite"], big["ambito"]["limite_richiesto"]), (MC.LIMIT_MAX, 5000))

    def test_order_date_desc(self):
        dates = [r["padre"]["data"] for r in self.doc["riferimenti"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_compact_vs_detail(self):
        c = build(self.reg, limite=200)
        ci = refs(c)[A("C-MF", "w-sim") + "#drive_file:d3"]
        self.assertEqual(ci["padre"]["hash"], self.r[ci["id"]]["padre"]["hash"][:12])
        self.assertEqual(ci["associazioni_m5"][0]["con"], "cvv:1000101")
        self.assertNotIn("criterio", ci["attribuzione"])

    def test_sources_unread_and_empty(self):
        with mock.patch.object(R.Registry, "freshness", fresh_fake(none_kinds=("classroom/material",))):
            d = build(self.reg)
        self.assertEqual(d["fonti"]["classroom/material"]["stato"], "non_letto")
        self.assertIn("fonte_non_letta", [g["codice"] for g in d["gap"]])
        reg = make(materials=[], works=[], announcements=[], notices=[])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            e = build(reg)
        self.assertEqual((e["sintesi"]["totale"], e["riferimenti"]), (0, []))
        reg.close()


# ------------------------------------------------------------------------------------------------ provenance, determinismo, sicurezza
class ProvenanceESicurezza(unittest.TestCase):
    def test_provenance_parent_hash(self):
        reg = make()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg, limite=200, dettaglio=True)
        rec = {(r["source"], r["source_uid"]): r["content_hash"] for r in reg.list_records()}
        for r in d["riferimenti"]:
            src = "classroom" if r["sorgente"] == "classroom" else "classeviva"
            suid = r["padre"]["uid"].split(":", 1)[1]
            self.assertEqual(rec[(src, suid)], r["padre"]["hash"])
        reg.close()

    def test_byte_identical_and_reversed(self):
        a, b = make(), make(reverse=True)
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            for kw in ({}, {"dettaglio": True, "limite": 200}, {"materia": "matematica"}, {"perimetro": "storico"}, {"limite": 2, "pagina": 2}):
                x1, x2, y = build(a, **kw), build(a, **kw), build(b, **kw)
                self.assertEqual(pretty(x1), pretty(x2))
                self.assertEqual(pretty(x1), pretty(y))
        a.close(); b.close()

    def test_builder_never_reads_clock_or_network(self):
        src = open(MC.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "time.time", "date.today", "utcnow", "urllib", "requests", "http.client", "open("):
            self.assertNotIn(bad, src)

    def test_inputs_not_mutated(self):
        reg = make()
        man, subs = copy.deepcopy(MAN), copy.deepcopy(SUBJ)
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            build(reg, manual=man, subjects=subs, dettaglio=True)
        self.assertEqual((man, subs), (MAN, SUBJ))
        reg.close()

    def test_no_writes_and_m1_m14_unchanged(self):
        reg = make()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            snap = lambda: [pretty(C3.build(reg, "matematica", SUBJ, NOW, manual=MAN)), pretty(C3.overview(reg, SUBJ, NOW, manual=MAN)),  # noqa: E731
                            pretty(L.build(reg, SUBJ, NOW, manual=MAN)), pretty(M.build(reg, SUBJ, NOW, manual=MAN)),
                            pretty(C4.build(reg, "inglese", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(ST.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(PR.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(AC.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(SX.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(SA.build(reg, "matematica", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(SS.build(reg, "matematica", "spiega", "testo", NOW, manual=MAN, subjects=SUBJ)),
                            pretty(RC.build(reg, NOW, manual=MAN, subjects=SUBJ))]
            before, c0 = snap(), counts(reg)
            build(reg, dettaglio=True, limite=200)
            self.assertEqual((before, c0), (snap(), counts(reg)))
        reg.close()

    def test_build_from_path_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.sqlite3")
        make(path=p).close()
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        s0, f0, h0 = dry._fs_state(p), sorted(os.listdir(d)), h(p)
        out = MC.build_from_path(p, NOW, subjects=SUBJ)
        self.assertEqual(out["schema"], MC.SCHEMA)
        self.assertEqual((s0, f0, h0), (dry._fs_state(p), sorted(os.listdir(d)), h(p)))


class ServerTool(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.d = tempfile.mkdtemp()
        os.chmod(self.d, 0o700)
        self.db = os.path.join(self.d, "registry.sqlite3")
        self.manual = os.path.join(self.d, "manual", "school_manual.json")
        for p in (mock.patch.dict(os.environ, {"CLOUD_SCHOOL_DB": self.db}), mock.patch.object(server, "subjects", lambda: SUBJ),
                  mock.patch.object(SC, "manual_default_path", lambda: self.manual)):
            p.start(); self.addCleanup(p.stop)

    def test_tool_registered(self):
        t = self.server.srv._tools["school_materials_catalog_v1"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"materia", "obiettivo", "perimetro", "limite", "pagina", "dettaglio", "soglia_ore"})
        self.assertFalse(t["inputSchema"].get("required"))
        self.assertIn("SOLA LETTURA", t["description"])

    def test_missing_registry(self):
        with self.assertRaises(ValueError):
            self.server.school_materials_catalog_v1()
        self.assertFalse(os.path.exists(self.db))

    def test_clock_once_no_writes(self):
        make(path=self.db).close()
        os.makedirs(os.path.dirname(self.manual), exist_ok=True)
        with open(self.manual, "w") as f:
            json.dump(MAN, f)
        calls = []
        fixed = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

        class FakeDT:
            class datetime:
                @staticmethod
                def now(tz=None):
                    calls.append(tz)
                    return fixed
        h0 = hashlib.sha256(open(self.db, "rb").read()).hexdigest()
        with mock.patch.object(self.server, "dt", FakeDT):
            out = self.server.school_materials_catalog_v1(materia="matematica")
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertFalse(any(out["scritture"].values()))
        self.assertEqual(h0, hashlib.sha256(open(self.db, "rb").read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
