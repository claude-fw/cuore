"""M9 v1 - school_priority/1 (cloud.school.priority/1): decision layer prudente sopra M8. Regole P1/P1b/P2/P3a/P3b/P4/P5/P6, soglie
D-M9-1 ai confini 0/1/2/7/8, P2 senza escalation, P5 solo contesto, non_determinabile (D-M9-2), vuoto -> nessuna, affidabilita'
monotona, storico mai nelle voci, evidenze identiche a M8, vocabolario chiuso, determinismo, nessuna scrittura, clock una volta."""
import copy, datetime as dt
import hashlib, itertools, json, os, re, sys, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import context4_fixtures as F
import cvv_dryrun as dry
import registry as R
import school_context as SC
import school_context3 as C3
import school_context4 as C4
import school_priority as PR
import school_status as ST
import cvv_cls_links as L
import cvv_comms as M
from links_fixtures import ann, ev, work
from test_school_context4 import fresh_fake, pretty, counts

SUBJ, NOW, MAN = F.SUBJECTS, F.NOW, F.MANUAL
A = lambda c, a: "cls:%s/%s" % (c, a)            # noqa: E731
LV = {k: i for i, k in enumerate(ST.RELIABILITY)}


def build(reg, **kw):
    kw.setdefault("manual", MAN)
    return PR.build(reg, kw.pop("now", NOW), subjects=kw.pop("subjects", SUBJ), **kw)


def subj(d, k):
    return next(m for m in d["materie"] if m["materia"] == k)


def voce(m, oggetto):
    return next(v for v in m["voci"] if v["oggetto"] == oggetto)


def m8(reg, k, **kw):
    return ST.build(reg, k, kw.pop("now", NOW), manual=MAN, subjects=SUBJ, dettaglio=True, **kw)


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


# ------------------------------------------------------------------------------------------------ struttura e politica
class Struttura(Base):
    def test_schema_keys_and_eight_subjects(self):
        d = self.doc
        self.assertEqual(PR.SCHEMA, "cloud.school.priority/1")
        self.assertEqual(list(d), ["schema", "natura", "riferimento_temporale", "politica", "ambito", "sintesi", "materie", "ordinamento",
                                   "provenance", "regole_agente"])
        self.assertEqual(sorted(m["materia"] for m in d["materie"]), sorted(SC.AGENT_SUBJECTS))
        self.assertEqual(d["riferimento_temporale"]["anno_operativo"], "2026-27")

    def test_policy_versioned_and_approved_values(self):
        self.assertEqual(PR.POLICY["P1_soglie_giorni"], {"alta_max": 1, "attenzione_max": 7})
        self.assertEqual((PR.POLICY["P3a_massimo"], PR.POLICY["P4_massimo"], PR.POLICY["P1b_massimo"]), ("attenzione", "informativa", "informativa"))
        self.assertTrue(PR.priority_version().startswith("r1-"))
        with mock.patch.dict(PR.POLICY, {"P1_soglie_giorni": {"alta_max": 2, "attenzione_max": 7}}):
            self.assertNotEqual(PR.priority_version(), self.doc["politica"]["versione"])

    def test_levels_and_closed_action_vocabulary(self):
        self.assertEqual(PR.PRIORITY_LEVELS, ("nessuna", "informativa", "attenzione", "alta", "non_determinabile"))
        self.assertEqual(PR.ACTIONS, ("controllare_verifica", "aprire_preparazione", "controllare_compito", "leggere_comunicazione",
                                      "verificare_informazione"))
        for m in self.doc["materie"]:
            self.assertIn(m["priorita"], PR.PRIORITY_LEVELS)
            for v in m["voci"]:
                self.assertIn(v["priorita"], ("informativa", "attenzione", "alta"))
                for a in v["azioni"]:
                    self.assertIn(a["azione"], PR.ACTIONS)

    def test_no_judgment_plan_duration_or_weights(self):
        rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in PR.FORBIDDEN_WORDS), re.I)
        self.assertIsNone(rx.search(pretty(self.doc)))
        for txt in list(PR.RULES.values()) + PR.RULES_FOR_AGENTS + [PR.NATURA]:
            self.assertIsNone(rx.search(txt), txt)
        self.assertNotRegex(pretty(self.doc), r'"(punteggio|peso|score|voto)"')

    def test_invalid_inputs(self):
        for bad in ("chimica", ""):
            with self.assertRaises(ValueError):
                build(self.reg, materia=bad)
        with self.assertRaises(ValueError):
            build(self.reg, dettaglio="si")
        one = build(self.reg, materia="MATEMATICA")
        self.assertEqual([m["materia"] for m in one["materie"]], ["matematica"])


# ------------------------------------------------------------------------------------------------ regole
class Regole(Base):
    def test_p1_thresholds_unit(self):
        self.assertEqual([PR.p1_level(g) for g in (0, 1, 2, 7, 8)], ["alta", "alta", "attenzione", "attenzione", "informativa"])

    def test_p1_boundaries_end_to_end(self):
        # verifica di matematica il 2026-11-02: now tale che giorni_mancanti = 0,1,2,7,8
        for day, g, exp in (("2026-11-02", 0, "alta"), ("2026-11-01", 1, "alta"), ("2026-10-31", 2, "attenzione"),
                            ("2026-10-26", 7, "attenzione"), ("2026-10-25", 8, "informativa")):
            m = subj(build(self.reg, materia="matematica", now=day + "T08:00:00+02:00"), "matematica")
            v = voce(m, "cvv:1000101")
            self.assertEqual((v["regola"], v["priorita"]), ("P1", exp), day)
            self.assertIn("oggi" if g == 0 else ("tra %d giorn" % g), v["motivo"])
            self.assertEqual(bool(v.get("note")), g == 0)

    def test_p1_one_voce_per_future_test_and_m6_reliability(self):
        m = subj(self.doc, "inglese")
        v = voce(m, A("C-EN", "a-en-ver"))
        self.assertEqual((v["priorita"], v["affidabilita"]), ("attenzione", "media"))          # 5 giorni; M6 probabile
        self.assertEqual(v["azioni"][0]["azione"], "controllare_verifica")

    def test_p1b_undated_hint_informativa(self):
        m = subj(self.doc, "scienze")
        v = voce(m, A("C-SC", "a-sci-ver"))
        self.assertEqual((v["regola"], v["priorita"], v["affidabilita"]), ("P1b", "informativa", "bassa"))
        self.assertEqual(v["azioni"][0]["azione"], "verificare_informazione")

    def test_p2_adds_action_without_escalation(self):
        for k, uid in (("matematica", "cvv:1000101"), ("fisica", "cvv:1000102")):
            m = subj(self.doc, k)
            v = voce(m, uid)
            names = [a["azione"] for a in v["azioni"]]
            self.assertEqual(names[:2], ["controllare_verifica", "aprire_preparazione"])
            prep = v["azioni"][1]
            self.assertEqual(v["priorita"], PR.p1_level((dt.date.fromisoformat(v["data"]) - dt.date(2026, 11, 1)).days))
            self.assertEqual(v["affidabilita"], next(s for s in m8(self.reg, k)["segnali"] if s["id"] == "S1")["affidabilita"])
            self.assertIn(prep["affidabilita"], ("media", "bassa"))                          # M5 probabile: non e' un fatto
        # senza P2 (nessun collegamento) la priorita' e' la stessa: P2 non alza mai
        reg = F.make_registry(works=[w for w in F.WORKS if w["id"] != "w-sim"])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            v2 = voce(subj(build(reg, materia="matematica"), "matematica"), "cvv:1000101")
        self.assertEqual(v2["priorita"], voce(subj(self.doc, "matematica"), "cvv:1000101")["priorita"])
        self.assertNotIn("aprire_preparazione", [a["azione"] for a in v2["azioni"]])
        reg.close()

    def test_p2_non_determinabile_link_no_action(self):
        m = subj(self.doc, "italiano")                                          # Ariosto: solo collegamenti non_determinabili
        v = voce(m, "cvv:1000104")
        self.assertEqual([a["azione"] for a in v["azioni"]], ["controllare_verifica"])
        self.assertEqual(v["segnali"], ["S1"])                                          # il collegamento non_determinabile non entra

    def test_p3a_capped_attenzione_and_informativa_far(self):
        m = subj(self.doc, "matematica")
        self.assertEqual(voce(m, "cvv:3000001")["priorita"], "attenzione")              # 5 giorni
        self.assertEqual(voce(m, A("C-MF", "w-mat-open"))["priorita"], "informativa")    # 8 giorni
        reg = F.make_registry(events=F.LF.EVENTS + [ev(3000009, "Esercizi di matematica per domani", "ROSSI MARIO", "2026-11-02", code="AGHW")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            v = voce(subj(build(reg, materia="matematica"), "matematica"), "cvv:3000009")
        self.assertEqual((v["regola"], v["priorita"]), ("P3a", "attenzione"))           # 1 giorno: MAI alta
        reg.close()

    def test_p3b_overdue_with_caveat(self):
        v = voce(subj(self.doc, "inglese"), A("C-EN", "w-en-late"))
        self.assertEqual((v["regola"], v["priorita"]), ("P3b", "attenzione"))
        self.assertIn("secondo Classroom", v["motivo"])
        self.assertTrue(any("richiesta" in n for n in v["note"]))

    def test_p4_recent_operational_only(self):
        m = subj(self.doc, "scienze")
        p4 = {v["oggetto"]: v for v in m["voci"] if v["regola"] == "P4"}
        self.assertIn(A("C-SC", "a-sci-ass"), p4)                                        # assenza docente (organizzativa probabile)
        self.assertIn("assenza docente", p4[A("C-SC", "a-sci-ass")]["motivo"])
        self.assertNotIn(A("C-SC", "a-sci1"), p4)                                        # materiale/proposto: generico
        self.assertNotIn(A("C-SC", "a-two"), p4)                                         # pari livello: nessuna classe principale
        for v in p4.values():
            self.assertEqual((v["priorita"], v["azioni"][0]["azione"]), ("informativa", "leggere_comunicazione"))
            self.assertGreaterEqual(v["data"], "2026-10-18")

    def test_p4_not_duplicated_with_s1(self):
        m = subj(self.doc, "inglese")
        objs = [v["oggetto"] for v in m["voci"]]
        self.assertEqual(len(objs), len(set(objs)))
        self.assertEqual(voce(m, A("C-EN", "a-en-ver"))["regola"], "P1")

    def test_p5_never_priority(self):
        for m in self.doc["materie"]:
            self.assertFalse([v for v in m["voci"] if v["regola"] == "P5" or set(v["segnali"]) & {"S4", "S5", "S8", "S9"}])
            self.assertIn("S4_continuita_lezioni", m["contesto"])
        reg = F.make_registry(lessons=[])                                                # nessuna lezione: nessuna priorita' da P5
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            m = subj(build(reg, materia="latino"), "latino")
        self.assertEqual(m["priorita"], "nessuna")
        reg.close()

    def test_p6_adds_verification_when_unreliable(self):
        doc = m8(self.reg, "inglese")
        s3 = next(s for s in doc["segnali"] if s["id"] == "S3")
        s3["affidabilita"] = "bassa"
        m = PR.subject_priority(doc)
        v = voce(m, A("C-EN", "w-en-late"))
        self.assertEqual(v["priorita"], "attenzione")                                   # P6 non cambia la priorita'
        self.assertEqual([a["azione"] for a in v["azioni"]], ["controllare_compito", "verificare_informazione"])
        self.assertTrue(m["da_verificare"])
        self.assertEqual(m["stato_decisione"], "da_verificare")


# ------------------------------------------------------------------------------------------------ mancante, vuoto, affidabilita'
class MancanteEAffidabilita(Base):
    def test_unread_source_is_non_determinabile_never_nessuna(self):
        with mock.patch.object(R.Registry, "freshness", fresh_fake(none_kinds=("classeviva/agenda_event", "classroom/course_work",
                                                                                "classroom/announcement"))):
            d = build(self.reg)
        for k in ("latino", "storia", "filosofia"):
            m = subj(d, k)
            self.assertEqual((m["priorita"], m["affidabilita"], m["stato_decisione"]), ("non_determinabile", "non_determinabile", "parziale"))
            self.assertEqual({r["regola"] for r in m["regole_non_valutate"]}, {"P1", "P3a", "P3b", "P4"})     # S2/S3 null insieme in M8
        self.assertIn("latino", d["sintesi"]["non_determinabile"])

    def test_unread_source_caps_even_with_items(self):
        doc = m8(self.reg, "matematica")
        s7 = next(s for s in doc["segnali"] if s["id"] == "S7")
        s7["valore"], s7["affidabilita"] = None, "non_determinabile"
        m = PR.subject_priority(doc)
        self.assertEqual((m["priorita"], m["affidabilita"]), ("alta", "non_determinabile"))   # la voce resta, l'affidabilita' no

    def test_empty_read_source_is_nessuna(self):
        reg = F.make_registry(events=[], works=[], announcements=[], notices=[], submissions=[])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg)
        for m in d["materie"]:
            self.assertEqual((m["priorita"], m["affidabilita"], m["regole_non_valutate"]), ("nessuna", "alta", []))
        reg.close()

    def test_monotone_under_m8_degradation(self):
        a = self.doc
        for fk in ({"stale_kinds": ("classeviva/agenda_event",)}, {"stale_kinds": ("classroom/announcement", "classroom/course_work")},
                   {"none_kinds": ("classeviva/agenda_event",)}, {"none_kinds": ("classroom/announcement",)}):
            with mock.patch.object(R.Registry, "freshness", fresh_fake(**fk)):
                b = build(self.reg)
            for k in SC.AGENT_SUBJECTS:
                ma, mb = subj(a, k), subj(b, k)
                self.assertGreaterEqual(LV[mb["affidabilita"]], LV[ma["affidabilita"]], (k, fk))
                # invariante per voce: stessa (oggetto, regola) -> affidabilita' mai piu' alta. Una voce di regola DIVERSA sullo stesso
                # oggetto (es. P4 che resta quando P1b non e' valutabile) e' un'altra affermazione: la materia e' coperta dal tetto R2.
                old = {(v["oggetto"], v["regola"]): v for v in ma["voci"]}
                for v in mb["voci"]:
                    if (v["oggetto"], v["regola"]) in old:
                        o = old[(v["oggetto"], v["regola"])]
                        self.assertGreaterEqual(LV[v["affidabilita"]], LV[o["affidabilita"]])
                        if "stale_kinds" in fk:
                            self.assertEqual(v["priorita"], o["priorita"])                       # la freschezza non cambia la priorita'
                if "none_kinds" in fk and ma["voci"] != mb["voci"]:
                    self.assertEqual(mb["affidabilita"], "non_determinabile")

    def test_monotone_property_synthetic(self):
        base = {k: m8(self.reg, k) for k in ("matematica", "inglese", "scienze", "fisica")}
        inputs = ("S1", "S2", "S3", "S6", "S7")
        for k, doc in base.items():
            ref = PR.subject_priority(doc)
            for sids in itertools.chain.from_iterable(itertools.combinations(inputs, n) for n in (1, 2)):
                for mode in ("media", "bassa", "non_determinabile", "null"):
                    d = copy.deepcopy(doc)
                    for s in d["segnali"]:
                        if s["id"] in sids:
                            if mode == "null":
                                s["valore"], s["affidabilita"] = None, "non_determinabile"
                            else:
                                s["affidabilita"] = ST.RELIABILITY[max(LV[s["affidabilita"]], LV[mode])]
                    m = PR.subject_priority(d)
                    self.assertGreaterEqual(LV[m["affidabilita"]], LV[ref["affidabilita"]], (k, sids, mode))
                    if mode != "null":
                        self.assertEqual([(v["oggetto"], v["priorita"]) for v in m["voci"]],
                                         [(v["oggetto"], v["priorita"]) for v in ref["voci"]])

    def test_legacy_no_effect(self):
        def legacy(self_, now, stale_after_hours=None):
            out = fresh_fake()(self_, now, stale_after_hours)
            for e in out["per_kind"].values():
                e["ultimo_run"]["legacy"] = True
            return out
        with mock.patch.object(R.Registry, "freshness", legacy):
            d = build(self.reg)
        strip = lambda x: [(m["materia"], m["priorita"], m["affidabilita"], [(v["oggetto"], v["affidabilita"]) for v in m["voci"]])  # noqa: E731
                           for m in x["materie"]]
        self.assertEqual(strip(d), strip(self.doc))


# ------------------------------------------------------------------------------------------------ evidenze e storico
class EvidenzeEStorico(Base):
    def test_evidence_identical_to_m8(self):
        for m in self.doc["materie"]:
            doc = m8(self.reg, m["materia"])
            m8ev = {(s["id"], e["uid"]): e["hash"] for s in doc["segnali"] for e in s["evidenze"] if "uid" in e}
            for v in m["voci"]:
                self.assertTrue(v["evidenze"])
                for e in v["evidenze"]:
                    self.assertEqual(m8ev[(e["segnale"], e["uid"])], e["hash"])
                for a in v["azioni"]:
                    for e in a.get("evidenze", []):
                        self.assertEqual(m8ev[(e["segnale"], e["uid"])], e["hash"])

    def test_history_never_in_items(self):
        reg = F.make_registry(announcements=F.ANNOUNCEMENTS + [ann("C-SC", "a-old-ver", "Verifica del 6 novembre 2025", "2025-10-01")])
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            d = build(reg)
            old = set()
            for k in SC.AGENT_SUBJECTS:
                c = C4.collect(reg, k, NOW, manual=MAN, subjects=SUBJ)
                old |= {r["uid"] for s in C4.SECTIONS for r in c["candidati"][s] if ST._year(r, s) not in (None, "2026-27")}
        self.assertIn(A("C-SC", "a-old-ver"), old)
        for m in d["materie"]:
            used = {v["oggetto"] for v in m["voci"]} | {e["uid"] for v in m["voci"] for e in v["evidenze"]}
            self.assertFalse(used & old, m["materia"])
        self.assertIn("2025-26", subj(d, "scienze")["storico"]["anni"])
        reg.close()

    def test_ordering_by_level_then_date(self):
        order = [m["priorita"] for m in self.doc["materie"]]
        self.assertEqual(order, sorted(order, key=PR.ORDER_SUBJECTS.index))
        self.assertEqual(self.doc["materie"][0]["materia"], "matematica")              # alta (verifica tra 1 giorno)


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
        src = open(PR.__file__, encoding="utf-8").read()
        for bad in ("datetime.now", "time.time", "date.today", "utcnow"):
            self.assertNotIn(bad, src)

    def test_no_writes_and_m2_m8_unchanged(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            snap = lambda: [pretty(C3.build(reg, "matematica", SUBJ, NOW, manual=MAN)), pretty(C3.overview(reg, SUBJ, NOW, manual=MAN)),  # noqa: E731
                            pretty(L.build(reg, SUBJ, NOW, manual=MAN)), pretty(M.build(reg, SUBJ, NOW, manual=MAN)),
                            pretty(C4.build(reg, "inglese", NOW, manual=MAN, subjects=SUBJ, dettaglio=True)),
                            pretty(ST.build(reg, "scienze", NOW, manual=MAN, subjects=SUBJ, dettaglio=True))]
            before, c0 = snap(), counts(reg)
            build(reg, dettaglio=True)
            self.assertEqual((before, c0), (snap(), counts(reg)))
        reg.close()

    def test_build_from_path_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.sqlite3")
        F.make_registry(path=p).close()
        h = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()                 # noqa: E731
        s0, f0, h0 = dry._fs_state(p), sorted(os.listdir(d)), h(p)
        out = PR.build_from_path(p, NOW, subjects=SUBJ, materia="fisica")
        self.assertEqual(out["schema"], PR.SCHEMA)
        self.assertEqual((s0, f0, h0), (dry._fs_state(p), sorted(os.listdir(d)), h(p)))

    def test_detail_adds_used_m8_signals(self):
        reg = F.make_registry()
        with mock.patch.object(R.Registry, "freshness", fresh_fake()):
            m = subj(build(reg, materia="matematica", dettaglio=True), "matematica")
        used = sorted({s for v in m["voci"] for s in v["segnali"]})
        self.assertEqual([s["id"] for s in m["segnali_m8"]], used)
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
        t = self.server.srv._tools["school_priority_v1"]["def"]
        self.assertEqual(set(t["inputSchema"]["properties"]), {"materia", "dettaglio", "soglia_ore"})
        self.assertFalse(t["inputSchema"].get("required"))
        self.assertIn("SOLA LETTURA", t["description"])

    def test_missing_registry(self):
        with self.assertRaises(ValueError):
            self.server.school_priority_v1()
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
            out = self.server.school_priority_v1()
        self.assertEqual(len(calls), 1)                                                 # anche per 8 materie
        self.assertEqual(out["riferimento_temporale"]["now"], "2026-11-01T12:00:00+02:00")
        self.assertFalse(out["verifica_nessuna_scrittura"]["file_modificati"])
        self.assertEqual(h0, hashlib.sha256(open(self.db, "rb").read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
