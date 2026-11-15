"""M10 v1 - school_action/1 (cloud.school.action/1): azioni assistive sopra M9, con risoluzione delle sole evidenze M9 nel contratto M8
(uid + hash). Struttura, 5 azioni, eseguibilita' (eseguibile / parzialmente / non), gap, provenance, affidabilita' monotona,
non_determinabile, mancante/vuoto/ambiguo, collegamenti probabili e non determinabili, storico escluso, nessun argomento inventato,
M8/M9 non modificati, determinismo, ordine inverso, nessuna scrittura, clock una volta, vocabolario chiuso, M2-M9 invariati."""
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
import school_priority as PR
import school_status as ST
from links_fixtures import ann, ev
from test_school_context4 import fresh_fake, pretty, counts

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL
A = lambda c, a: "cls:%s/%s" % (c, a)            # noqa: E731
LV = {k: i for i, k in enumerate(ST.RELIABILITY)}


def build(reg, **kw):
    kw.setdefault("manual", MAN)
    return AC.build(reg, kw.pop("now", NOW), subjects=kw.pop("subjects", SUBJ), **kw)


def subj(d, k):
    return next(m for m in d["materie"] if m["materia"] == k)


def act(m, azione, target):
    return next(x for x in m["azioni"] if x["azione"] == azione and x["target"] == target)


def pair(reg, k):
    """(voce materia M9 con dettaglio, documento M8 con dettaglio) per i test puri di subject_actions."""
    m9 = PR.build(reg, NOW, manual=MAN, materia=k, dettaglio=True, subjects=SUBJ)["materie"][0]
    return m9, ST.build(reg, k, NOW, manual=MAN, dettaglio=True, subjects=SUBJ)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = F.make_registry()
        cls.p = mock.patch.object(R.Registry, "freshness", fresh_fake())
        cls.p.start()
        cls.doc = build(cls.reg)

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        cls.reg.close()


# ------------------------------------------------------------------------------------------------ struttura
class Struttura(Base):
    def test_schema_and_keys(self):
        d = self.doc
        self.assertEqual(AC.SCHEMA, "cloud.school.action/1")
        self.assertEqual(list(d), ["schema", "natura", "riferimento_temporale", "politica", "ambito", "sintesi", "materie", "provenance",
                                   "regole_agente"])
        self.assertEqual(sorted(m["materia"] for m in d["materie"]), sorted(SC.AGENT_SUBJECTS))
        self.assertTrue(AC.action_version().startswith("r1-"))
        for m in d["materie"]:
            for x in m["azioni"]:
                self.assertEqual(list(x), ["id", "azione", "target", "eseguibilita", "affidabilita", "affidabilita_m9", "priorita_m9",
                                           "voce_m9", "regola_m9", "contesto", "gaps", "provenienza"])

    def test_closed_vocabulary_and_levels(self):
        self.assertEqual(AC.ACTIONS, ("controllare_verifica", "aprire_preparazione", "controllare_compito", "leggere_comunicazione",
                                      "verificare_informazione"))
        self.assertEqual(AC.EXECUTABILITY, ("eseguibile", "parzialmente_eseguibile", "non_eseguibile"))
        names = set()
        for m in self.doc["materie"]:
            for x in m["azioni"]:
                self.assertIn(x["azione"], AC.ACTIONS)
                self.assertIn(x["eseguibilita"], AC.EXECUTABILITY)
                for g in x["gaps"]:
                    self.assertIn(g["codice"], AC.GAP_CODES)
                names.add(x["azione"])
        self.assertEqual(names, set(AC.ACTIONS))                                       # le 5 azioni compaiono nelle fixture

    def test_one_action_per_m9_action(self):
        m9 = PR.build(self.reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True)
        exp = sorted((m["materia"], a["azione"], a["uid"]) for m in m9["materie"] for v in m["voci"] for a in v["azioni"])
        got = sorted((m["materia"], x["azione"], x["target"]) for m in self.doc["materie"] for x in m["azioni"])
        self.assertEqual(got, exp)

    def test_priority_reliability_executability_distinct(self):
        x = act(subj(self.doc, "matematica"), "controllare_compito", "cvv:3000001")
        self.assertEqual((x["priorita_m9"], x["eseguibilita"]), ("attenzione", "parzialmente_eseguibile"))
        self.assertIn("affidabilita_m9", x)

    def test_no_judgment_words(self):
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in AC.FORBIDDEN_WORDS), re.I)
        for txt in list(AC.RULES.values()) + AC.RULES_FOR_AGENTS + [AC.NATURA] + list(AC.UNCERTAIN_INFO.values()):
            self.assertIsNone(rx.search(txt), txt)
        policy = pretty({k: v for k, v in self.doc.items() if k not in ("materie",)})
        self.assertIsNone(rx.search(policy))


# ------------------------------------------------------------------------------------------------ le 5 azioni
class Azioni(Base):
    def test_controllare_verifica_context_without_invented_topics(self):
        x = act(subj(self.doc, "matematica"), "controllare_verifica", "cvv:1000101")
        c = x["contesto"]
        self.assertEqual((c["data"], c["testo_verifica"], c["fonte"]["origine"]), ("2026-11-02", "Verifica Matematica", "agenda_classeviva"))
        self.assertNotIn("argomenti", c)                                               # nessun argomento estratto o dedotto
        self.assertTrue(all(l["stato"] in ("probabile", "proposto", "conflitto", "non_determinabile") for l in c["collegamenti_m5"]))
        lesson_args = {"Equazioni e disequazioni irrazionali", "Funzione irrazionale", "Integrali indefiniti", "Integrali definiti"}
        self.assertFalse(lesson_args & set(re.findall(r"[^\"]+", pretty(c))))           # le lezioni recenti (S5) non entrano
        self.assertEqual(x["eseguibilita"], "eseguibile")

    def test_coverage_not_exposed_ariosto(self):
        x = act(subj(self.doc, "italiano"), "controllare_verifica", "cvv:1000104")       # "Verifica su Ariosto"
        c = x["contesto"]
        self.assertEqual((c["collegamenti_m5"], c["collegamenti_m5_copertura"]), ([], "non_esposti_da_m9"))
        self.assertIn("NON significa che non esistano", c["nota_copertura"])
        self.assertIn("comunicazioni_collegate", c["nota_copertura"])                   # anche [] di comunicazioni non e' assenza certa
        self.assertEqual(c["comunicazioni_collegate"], [])
        # M5 ha davvero collegamenti (non_determinabili) per questa verifica: la lista vuota non e' "nessun collegamento"
        res = L.compute(L.collect(self.reg, SUBJ, MAN))
        self.assertTrue([y for y in res["non_determinabili"] if y["a"]["uid"] == "cvv:1000104"])
        # nessun gap nuovo, eseguibilita' e affidabilita' invariate (= M9)
        self.assertEqual((x["eseguibilita"], x["gaps"], x["affidabilita"]), ("eseguibile", [], x["affidabilita_m9"]))

    def test_coverage_exposed_matematica(self):
        c = act(subj(self.doc, "matematica"), "controllare_verifica", "cvv:1000101")["contesto"]
        self.assertEqual(c["collegamenti_m5_copertura"], "esposti_da_m9")
        self.assertTrue(c["collegamenti_m5"])
        self.assertNotIn("nota_copertura", c)
        self.assertEqual(AC.COVERAGE, ("esposti_da_m9", "non_esposti_da_m9"))
        self.assertIn("non_esposti_da_m9", " ".join(AC.RULES_FOR_AGENTS))

    def test_controllare_verifica_m6_origin(self):
        x = act(subj(self.doc, "inglese"), "controllare_verifica", A("C-EN", "a-en-ver"))
        self.assertEqual((x["contesto"]["fonte"]["origine"], x["contesto"]["fonte"]["stato_m6"]), ("comunicazione_m6", "probabile"))
        self.assertEqual(x["affidabilita"], "media")                                   # = M9, mai sopra

    def test_aprire_preparazione_keeps_probable_state(self):
        m = subj(self.doc, "matematica")
        x = next(x for x in m["azioni"] if x["azione"] == "aprire_preparazione")
        c = x["contesto"]
        self.assertEqual((c["relazione"], c["stato_collegamento"]), ("prepara", "probabile"))
        self.assertIn("non_confermato", c["certezza"])
        self.assertEqual(c["materiale"]["uid"], A("C-MF", "w-sim"))
        self.assertEqual(c["verifica"]["uid"], "cvv:1000101")
        self.assertNotEqual(x["affidabilita"], "alta")

    def test_controllare_compito_classroom_and_agenda(self):
        m = subj(self.doc, "matematica")
        cr = act(m, "controllare_compito", A("C-MF", "w-mat-open"))
        self.assertEqual((cr["eseguibilita"], cr["contesto"]["stato_operativo"], cr["contesto"]["scadenza"]), ("eseguibile", "aperto", "2026-11-09"))
        ag = act(m, "controllare_compito", "cvv:3000001")
        self.assertEqual(ag["eseguibilita"], "parzialmente_eseguibile")
        self.assertEqual([g["codice"] for g in ag["gaps"]], ["stato_consegna_non_disponibile"])

    def test_controllare_compito_overdue_prudent_wording(self):
        x = act(subj(self.doc, "inglese"), "controllare_compito", A("C-EN", "w-en-late"))
        self.assertIn("non consegnato secondo Classroom", x["contesto"]["formulazione"])
        self.assertEqual(x["contesto"]["stato_operativo"], "scaduto_non_consegnato")
        self.assertNotRegex(pretty(x), r"(?i)non hai fatto")

    def test_leggere_comunicazione_no_reinterpretation(self):
        x = act(subj(self.doc, "scienze"), "leggere_comunicazione", A("C-SC", "a-sci-ass"))
        c = x["contesto"]
        self.assertEqual(c["caso_speciale"], "ASSENZA_DOCENTE")
        self.assertEqual(c["classe_principale"], {"valore": "organizzativa", "stato": "probabile", "secondarie": ["compito"]})
        m6 = {y["uid"]: y for y in M.compute(self.reg, SUBJ, C3.D.parse_now(NOW), MAN)[0]}
        self.assertEqual(c["stato_m6"], m6[A("C-SC", "a-sci-ass")]["stato"])

    def test_leggere_comunicazione_truncated_is_executable_with_gap(self):
        long = "Per domani leggete " + "pagina " * 40 + "fine"
        reg = F.make_registry(announcements=F.ANNOUNCEMENTS + [ann("C-SC", "a-long", long, "2026-10-31")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            x = act(subj(build(reg, materia="scienze"), "scienze"), "leggere_comunicazione", A("C-SC", "a-long"))
        self.assertEqual(x["eseguibilita"], "eseguibile")
        self.assertEqual([g["codice"] for g in x["gaps"]], ["contenuto_parziale"])
        self.assertTrue(x["contesto"]["testo_troncato"])
        reg.close()

    def test_verificare_informazione_p1b(self):
        x = act(subj(self.doc, "scienze"), "verificare_informazione", A("C-SC", "a-sci-ver"))
        c = x["contesto"]
        self.assertEqual(c["origine_regola_m9"], "P1b")
        self.assertIn("data della verifica", c["informazione_non_determinabile"])
        self.assertTrue(c["dati_mancanti_o_incerti"][0]["motivi_affidabilita_m8"])
        self.assertEqual(x["affidabilita"], "bassa")                                   # l'incertezza non viene risolta

    def test_verificare_informazione_conflict_link(self):
        m9, d8 = pair(self.reg, "matematica")
        m9 = copy.deepcopy(m9)
        v = next(v for v in m9["voci"] if v["oggetto"] == "cvv:1000101")
        for a in v["azioni"]:
            if a["azione"] == "aprire_preparazione":
                a["azione"] = "verificare_informazione"                                # stesso collegamento, come per un conflitto M5
        x = next(x for x in AC.subject_actions(m9, d8)["azioni"] if x["azione"] == "verificare_informazione")
        self.assertEqual(x["contesto"]["origine_regola_m9"], "P2")
        self.assertIn("collegamento_in_conflitto", [g["codice"] for g in x["gaps"]])

    def test_verificare_informazione_p6(self):
        m9, d8 = pair(self.reg, "inglese")
        m9 = copy.deepcopy(m9)
        v = next(v for v in m9["voci"] if v["oggetto"] == A("C-EN", "w-en-late"))
        v["azioni"].append({"azione": "verificare_informazione", "uid": v["oggetto"], "affidabilita": "bassa"})
        x = next(x for x in AC.subject_actions(m9, d8)["azioni"] if x["azione"] == "verificare_informazione" and x["target"] == v["oggetto"])
        self.assertEqual(x["contesto"]["origine_regola_m9"], "P6")


# ------------------------------------------------------------------------------------------------ eseguibilita', gap, affidabilita'
class EseguibilitaEAffidabilita(Base):
    def test_unresolvable_hash_is_non_eseguibile(self):
        m9, d8 = pair(self.reg, "matematica")
        m9 = copy.deepcopy(m9)
        v = next(v for v in m9["voci"] if v["oggetto"] == "cvv:1000101")
        v["evidenze"][0]["hash"] = "000000000000"
        x = act(AC.subject_actions(m9, d8), "controllare_verifica", "cvv:1000101")
        self.assertEqual((x["eseguibilita"], x["affidabilita"]), ("non_eseguibile", "non_determinabile"))
        self.assertEqual([g["codice"] for g in x["gaps"]], ["evidenza_non_risolvibile"])
        self.assertEqual(x["contesto"], {})
        self.assertFalse(x["provenienza"]["m8"][0]["risolta"])

    def test_missing_m8_element_is_non_eseguibile(self):
        m9, d8 = pair(self.reg, "inglese")
        d8 = copy.deepcopy(d8)
        for s in d8["segnali"]:
            if s["id"] == "S3":
                s["evidenze_dettaglio"] = []
        x = act(AC.subject_actions(m9, d8), "controllare_compito", A("C-EN", "w-en-late"))
        self.assertEqual(x["eseguibilita"], "non_eseguibile")

    def test_topics_missing_is_partial(self):
        m9, d8 = pair(self.reg, "italiano")
        d8 = copy.deepcopy(d8)
        for s in d8["segnali"]:
            for e in s.get("evidenze_dettaglio") or []:
                if e["uid"] == "cvv:1000104":
                    e["testo"] = ""
        x = act(AC.subject_actions(m9, d8), "controllare_verifica", "cvv:1000104")
        self.assertEqual(x["eseguibilita"], "parzialmente_eseguibile")
        self.assertIn("argomenti_non_disponibili", [g["codice"] for g in x["gaps"]])
        self.assertEqual(x["affidabilita"], "media")                                   # tetto del parziale (M9 era media)

    def test_non_determinabile_link_visible_never_preparation(self):
        m9, d8 = pair(self.reg, "matematica")
        m9 = copy.deepcopy(m9)
        s6 = next(s for s in m9["segnali_m8"] if s["id"] == "S6")
        s6["valore"][0]["collegamenti"].append({"uid": "nd:xyz", "relazione": "stesso_argomento", "stato": "non_determinabile",
                                                "tipo": "non_determinabile"})
        out = AC.subject_actions(m9, d8)
        x = act(out, "controllare_verifica", "cvv:1000101")
        self.assertIn("collegamento_non_determinabile", [g["codice"] for g in x["gaps"]])
        self.assertEqual(x["eseguibilita"], "eseguibile")
        self.assertFalse([y for y in out["azioni"] if y["azione"] == "aprire_preparazione" and y["target"] == "nd:xyz"])

    def test_m9_non_determinabile_stays(self):
        m9, d8 = pair(self.reg, "matematica")
        m9 = copy.deepcopy(m9)
        for v in m9["voci"]:
            for a in v["azioni"]:
                a["affidabilita"] = "non_determinabile"
        for x in AC.subject_actions(m9, d8)["azioni"]:
            self.assertEqual(x["affidabilita"], "non_determinabile")
            self.assertIn("affidabilita_non_determinabile", [g["codice"] for g in x["gaps"]])
            self.assertNotEqual(x["eseguibilita"], "eseguibile")

    def test_monotone_never_above_m9_property(self):
        for k in ("matematica", "inglese", "scienze"):
            m9, d8 = pair(self.reg, k)
            for lvl in ST.RELIABILITY:
                mm = copy.deepcopy(m9)
                for v in mm["voci"]:
                    for a in v["azioni"]:
                        a["affidabilita"] = ST.RELIABILITY[max(LV[a["affidabilita"]], LV[lvl])]
                base = {(x["azione"], x["target"]): x for x in AC.subject_actions(m9, d8)["azioni"]}
                for x in AC.subject_actions(mm, d8)["azioni"]:
                    self.assertGreaterEqual(LV[x["affidabilita"]], LV[x["affidabilita_m9"]])
                    self.assertGreaterEqual(LV[x["affidabilita"]], LV[base[(x["azione"], x["target"])]["affidabilita"]])

    def test_monotone_end_to_end_with_stale_sources(self):
        with mock.patch.object(R.Registry, "freshness", fresh_fake(stale_kinds=("classeviva/agenda_event", "classroom/announcement"))):
            b = build(self.reg)
        old = {(m["materia"], x["azione"], x["target"]): x for m in self.doc["materie"] for x in m["azioni"]}
        for m in b["materie"]:
            for x in m["azioni"]:
                o = old.get((m["materia"], x["azione"], x["target"]))
                if o:
                    self.assertGreaterEqual(LV[x["affidabilita"]], LV[o["affidabilita"]])

    def test_unread_source_no_actions_and_rules_listed(self):
        with mock.patch.object(R.Registry, "freshness", fresh_fake(none_kinds=("classeviva/agenda_event", "classroom/course_work",
                                                                                "classroom/announcement"))):
            m = subj(build(self.reg, materia="storia"), "storia")
        self.assertEqual(m["priorita_m9"], "non_determinabile")
        self.assertEqual(m["azioni"], [])
        self.assertTrue(m["regole_non_valutate_m9"])

    def test_empty_sources_no_actions(self):
        reg = F.make_registry(events=[], works=[], announcements=[], notices=[], submissions=[])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg)
        for m in d["materie"]:
            self.assertEqual((m["priorita_m9"], m["azioni"]), ("nessuna", []))
        reg.close()


# ------------------------------------------------------------------------------------------------ provenance e storico
class ProvenanceEStorico(Base):
    def test_provenance_uid_hash_chain(self):
        for m in self.doc["materie"]:
            m9, d8 = pair(self.reg, m["materia"])
            voci = {v["id"]: v for v in m9["voci"]}
            m8ev = {(s["id"], e["uid"]): e["hash"] for s in d8["segnali"] for e in s["evidenze"] if "uid" in e}
            for x in m["azioni"]:
                self.assertIn(x["voce_m9"], voci)
                v = voci[x["voce_m9"]]
                cited = {(e["segnale"], e["uid"], e["hash"]) for e in v["evidenze"]} | {
                    (e["segnale"], e["uid"], e["hash"]) for a in v["azioni"] for e in a.get("evidenze", [])}
                for e in x["provenienza"]["m8"]:
                    self.assertIn((e["segnale"], e["uid"], e["hash"]), cited)              # nessuna evidenza nuova
                    self.assertEqual(m8ev[(e["segnale"], e["uid"])], e["hash"])
                    self.assertTrue(e["risolta"])
                self.assertEqual(x["provenienza"]["m7"]["hash"], x["provenienza"]["m8"][0]["hash"])

    def test_history_excluded(self):
        reg = F.make_registry(announcements=F.ANNOUNCEMENTS + [ann("C-SC", "a-old-ver", "Verifica del 6 novembre 2025", "2025-10-01")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg)
        targets = {x["target"] for m in d["materie"] for x in m["azioni"]}
        self.assertNotIn(A("C-SC", "a-old-ver"), targets)
        self.assertNotIn(A("C-SC", "a-old"), targets)
        reg.close()

    def test_same_now_as_m9(self):
        m9 = PR.build(self.reg, NOW, manual=MAN, subjects=SUBJ)
        self.assertEqual(self.doc["riferimento_temporale"]["now"], m9["riferimento_temporale"]["now"])
        self.assertEqual([m["materia"] for m in self.doc["materie"]], [m["materia"] for m in m9["materie"]])


# ------------------------------------------------------------------------------------------------ determinismo, scritture, tool
class DeterminismoESicurezza(unittest.TestCase):
    def test_byte_identical_and_order_independent(self):
        a, b = F.make_registry(), F.make_registry(reverse=True)
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            for kw in ({}, {"dettaglio": True}, {"materia": "inglese"}):
                x1, x2, y = build(a, **kw), build(a, **kw), build(b, **kw)
                self.assertEqual(pretty(x1), pretty(x2))
                self.assertEqual(pretty(x1), pretty(y))
        a.close(); b.close()

    def test_builder_never_reads_clock(self):
        src = open(AC.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "time.time", "date.today", "utcnow"):
            self.assertNotIn(bad, src)

    def test_no_writes_and_m2_m9_unchanged(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            snap = lambda: [pretty(C3.build(reg, "matematica", SUBJ, NOW, manual=MAN)), pretty(C3.overview(reg, SUBJ, NOW, manual=MAN)),  # noqa: E731
                            pretty(L.build(reg, SUBJ, NOW, manual=MAN)), pretty(M.build(reg, SUBJ, NOW, manual=MAN)),
                            pretty(C4.build(reg, "inglese", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(ST.build(reg, "scienze", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(PR.build(reg, NOW, manual=MAN, subjects=SUBJ, dettaglio=True))]
            before, c0 = snap(), counts(reg)
            build(reg, dettaglio=True)
            self.assertEqual((before, c0), (snap(), counts(reg)))
        reg.close()

    def test_inputs_not_mutated(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            m9, d8 = pair(reg, "matematica")
            s9, s8 = pretty(m9), pretty(d8)
            AC.subject_actions(m9, d8, dettaglio=True)
        self.assertEqual((s9, s8), (pretty(m9), pretty(d8)))
        reg.close()

    def test_build_from_path_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.sqlite3")
        F.make_registry(path=p).close()
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        s0, f0, h0 = dry._fs_state(p), sorted(os.listdir(d)), h(p)
        out = AC.build_from_path(p, NOW, subjects=SUBJ, materia="fisica")
        self.assertEqual(out["schema"], AC.SCHEMA)
        self.assertEqual((s0, f0, h0), (dry._fs_state(p), sorted(os.listdir(d)), h(p)))

    def test_detail_adds_resolved_elements(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            m = subj(build(reg, materia="matematica", dettaglio=True), "matematica")
        for x in m["azioni"]:
            self.assertEqual([e["uid"] for e in x["elementi_m8"]], [e["uid"] for e in x["provenienza"]["m8"] if e["risolta"]])
        reg.close()

    def test_invalid_inputs(self):
        reg = F.make_registry()
        with self.assertRaises(ValueError):
            build(reg, dettaglio="si")
        with self.assertRaises(ValueError):
            build(reg, materia="chimica")
        reg.close()


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
        t = self.server.srv._tools["school_action_v1"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"materia", "dettaglio", "soglia_ore"})
        self.assertFalse(t["inputSchema"].get("required"))
        self.assertIn("SOLA LETTURA", t["description"])

    def test_missing_registry(self):
        with self.assertRaises(ValueError):
            self.server.school_action_v1()
        self.assertFalse(os.path.exists(self.db))

    def test_clock_once_no_writes(self):
        F.make_registry(path=self.db).close()
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
            out = self.server.school_action_v1()
        self.assertEqual(len(calls), 1)                                                 # un solo now per M9 e M8, 8 materie
        self.assertEqual(out["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertEqual(h0, hashlib.sha256(open(self.db, "rb").read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
